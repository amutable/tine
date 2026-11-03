# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Pin one repository: its build metadata, and every package it currently serves.

Refresh runs a host-side fetch-and-parse step per repository, and whichever package system owns
it, that step answers the same request: the metadata a build materializes to make the repository
local again, and an inventory keyed by the content checksum every package is fetched by. Only how
a repository describes those is its own, so the fetching, the merge rule, the JSON and the
invocation happen here and a driver is left with its own metadata format.
"""

import argparse
import hashlib
import http.client
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any, TypedDict

import specs
from util import fail, text_destination, urlopen, with_retries

AGENT = "tine-snapshot"


class PackageEntry(TypedDict):
    location: str
    size: int


class MetadataFile(TypedDict):
    """One pinned file, named by where it lands in the materialized repository directory."""

    out: str
    url: str
    sha256: str
    size: int


class RepositoryMetadata(TypedDict):
    """What a build materializes to make a pinned repository local again.

    A repository describes itself with files it serves and, where the pin has to edit or narrow
    what it found, files the snapshot carries verbatim. Both are placed by path, so what belongs
    in a `repodata/` subdirectory and what sits at the root are the same kind of thing here.
    """

    files: list[MetadataFile]
    inline: dict[str, str]


class Spec(TypedDict):
    id: str
    baseurl: str
    # The package system's own name for the architecture this snapshot is of. A mirror that names
    # it in the URL has it in `baseurl` already; one that names it in an index path needs it here.
    arch: str


# What a materialized repository whose metadata vouches for its packages carries beyond the
# snapshot's files: the manifest of those files, which a resolve records in the lock it writes, and
# the generations committed locks retain, one subdirectory each with the time it was pinned at.
# Keep in sync with METADATA_MANIFEST, RETAINED_DIRECTORY and PINNED_AT_FILE in
# package/repository.bzl.
MANIFEST = "metadata.json"
RETAINED = "retained"
PINNED_AT = "pinned_at"


def generations(repository: Path) -> list[Path]:
    """The metadata generations a materialized repository carries: the pinned one, then the retained.

    A lock's packages were vouched for by the metadata it was resolved against, so a package the
    pinned metadata no longer describes is still verifiable through the generation a lock retains.
    """
    retained = repository / RETAINED
    return [repository, *(sorted(retained.iterdir()) if retained.is_dir() else [])]


def pinned_at(generation: Path) -> str | None:
    """When a retained generation was pinned, ISO 8601 in UTC, for a verifier to judge it as of then.

    The pinned generation itself carries no time: the keyring built for the repository does.
    """
    marker = generation / PINNED_AT
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


class Vouching[T]:
    """What the repository's metadata states per package checksum: the pinned generation, then the retained.

    A retained generation is read only once a package the pinned one does not describe asks for it,
    so what a lock retained is a concern of the packages that need it and of no other.
    """

    def __init__(self, repository: Path, read: Callable[[Path], dict[str, T]]) -> None:
        self._read = read
        pinned, *self._retained = generations(repository)
        self._known = read(pinned)

    def get(self, checksum: str) -> T | None:
        while checksum not in self._known and self._retained:
            for digest, stated in self._read(self._retained.pop(0)).items():
                self._known.setdefault(digest, stated)
        return self._known.get(checksum)


def pool_checksum(prog: str, package: str) -> str:
    """The checksum a pool artifact is named by, ahead of its package system's suffix."""
    checksum, suffix, _ = Path(package).name.partition(".")
    if not suffix or len(checksum) != 64:
        fail(f"{prog}: {package} is not named by its checksum")
    return checksum.lower()


def checksum(rid: str, what: str, value: str | None) -> str:
    """Normalize and validate a sha256 a repository states about its own contents."""
    digest = "" if value is None else value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        fail(f"{rid}: {what} has invalid sha256 {digest!r}")
    return digest


# A server that stops sending would otherwise block a download forever. The timeout applies to each read,
# so a slow but steady download finishes.
_TIMEOUT = 60


def download(
    rid: str,
    what: str,
    url: str,
    output: IO[bytes],
    *,
    size: int | None = None,
    sha256: str | None = None,
) -> tuple[str, int]:
    """Stream one file into `output`, returning its digest and size.

    Where the repository has already stated both, they are checked, and an oversized response is
    cut off as it arrives rather than after an unbounded endpoint has filled the disk.
    """

    def fetch() -> tuple[str, int]:
        # A retried attempt restarts the stream from scratch.
        output.seek(0)
        output.truncate()
        digest = hashlib.sha256()
        total = 0
        with urlopen(url, agent=AGENT, timeout=_TIMEOUT) as response:
            try:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if size is not None and total > size:
                        fail(f"{rid}: {what} is larger than the stated {size} bytes")
                    digest.update(chunk)
                    output.write(chunk)
            except http.client.IncompleteRead as error:
                raise ConnectionError(f"the transfer ended early: {error}") from error
            # When the server closes the connection before the end of the Content-Length, http.client
            # returns the short body without an error. `length` then contains the number of missing bytes.
            if getattr(response, "length", None):
                raise ConnectionError(f"the transfer ended {response.length} bytes early")
        if size is not None and total != size:
            fail(f"{rid}: {what} is {total} bytes, not the stated {size}")
        if sha256 is not None and digest.hexdigest() != sha256:
            fail(f"{rid}: {what} does not match the checksum stated for it")
        return digest.hexdigest(), total

    return with_retries(f"{rid}: {url}", fetch)


def add_package(packages: dict[str, PackageEntry], rid: str, digest: str, entry: PackageEntry) -> None:
    """Record one package, settling a checksum the repository serves under several names."""
    previous = packages.get(digest)
    if previous is None:
        packages[digest] = entry
        return
    if previous["size"] != entry["size"]:
        fail(f"{rid}: duplicate checksum {digest} has conflicting sizes")

    # One package can be served under more than one name; pick a stable one.
    previous["location"] = min(previous["location"], entry["location"])


def _write_packages(output: IO[str], packages: Mapping[str, PackageEntry]) -> None:
    # One package per line keeps this large generated file reviewable.
    output.write("{\n")
    entries = sorted(packages.items())
    for index, (digest, package) in enumerate(entries):
        comma = "," if index + 1 < len(entries) else ""
        entry = json.dumps(package, sort_keys=True, separators=(",", ":"))
        output.write(f"    {json.dumps(digest)}: {entry}{comma}\n")
    output.write("  }")


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    """Write a snapshot as deterministic, reviewable UTF-8 JSON, replacing a path atomically."""
    with text_destination(path) as output:
        output.write("{\n")
        keys = sorted(snapshot)
        for index, key in enumerate(keys):
            comma = "," if index + 1 < len(keys) else ""
            output.write(f"  {json.dumps(key)}: ")
            if key == "packages":
                _write_packages(output, snapshot[key])
            else:
                output.write(json.dumps(snapshot[key], indent=2, sort_keys=True).replace("\n", "\n  "))
            output.write(f"{comma}\n")
        output.write("}\n")


def run[T](
    prog: str,
    shape: specs.Shape[T],
    inventory: Callable[[T], Mapping[str, Any]],
    argv: list[str] | None = None,
) -> None:
    """Take the repository's inventory and write it where this invocation asks.

    `--out` stays on the command line rather than in the spec: a repository publishes its
    `[snapshot]` sub-target as something to run, and the caller chooses where the result lands.
    """
    parser = argparse.ArgumentParser(prog=prog)
    specs.add_argument(parser)
    parser.add_argument("--out", required=True, help="snapshot path to write, or `-` for stdout")
    args = parser.parse_args(argv)

    snapshot = inventory(specs.load(shape, args.spec, prog=prog))
    out = Path(args.out)
    write_snapshot(out, snapshot)
    where = "stdout" if str(out) == "-" else str(out)
    print(f"wrote {where} ({len(snapshot['packages'])} packages)", file=sys.stderr)
