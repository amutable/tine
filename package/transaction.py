# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The transaction a planner writes and a package closure is selected from.

One schema, shared by every package system's planner, because the same Starlark reads all of
them: `select_package_artifacts` looks each entry up in its repository's pool by checksum, and a
committed box lock is one of these files kept verbatim. What differs between package systems
is how the closure is arrived at, not how it is described.
"""

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict

from util import fail, text_destination

import rootfs
import snapshotter
import storage
import uapi16
from snapshotter import MetadataFile


class Repository(NamedTuple):
    id: str
    path: Path
    priority: int
    baseurl: str | None


class RepositorySpec(TypedDict):
    id: str
    directory: str
    priority: int
    baseurl: str | None


class Spec(TypedDict):
    arch: str
    repositories: list[RepositorySpec]


class TransactionPackage(TypedDict):
    package_id: str
    repo: str
    pkg_checksum: str
    source: Literal["local", "repo"]
    # A local package names an input directory and file; a remote one names its transport.
    location: NotRequired[str]
    size: NotRequired[int]
    url: NotRequired[str]


class TransactionMetadata(TypedDict):
    """The metadata one remote repository was resolved against.

    Where the metadata rather than the package carries the proof, a committed lock keeps this so
    that its packages stay verifiable after the repository's pin moves on, the way it keeps their
    transports so that they stay fetchable.
    """

    repo: str
    source: Literal["metadata"]
    files: list[MetadataFile]
    # When the repository was pinned, for a verifier to judge the generation as of then.
    pinned_at: str | None


type TransactionEntry = TransactionPackage | TransactionMetadata


def load_repositories(spec: Spec, *, absolute: bool = False) -> list[Repository]:
    """Read the configured repositories a solve runs against, in declaration order."""
    return [
        Repository(
            repository["id"],
            Path(repository["directory"]).absolute() if absolute else Path(repository["directory"]),
            repository["priority"],
            repository["baseurl"],
        )
        for repository in spec["repositories"]
    ]


def checksum(what: str, value: str) -> str:
    """Check that a package's digest is one a pool can be keyed by."""
    digest = value.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        fail(f"{what} has invalid sha256 {value!r}")
    return digest


def entry(
    package_id: str,
    repository: Repository,
    digest: str,
    location: str,
    size: int = 0,
) -> TransactionPackage:
    """Describe one package the closure must materialize.

    A repository with no base URL is one this build produced, so its packages are projected from
    the directory that produced them rather than fetched.
    """
    package = TransactionPackage(
        package_id=package_id,
        repo=repository.id,
        pkg_checksum=checksum(package_id, digest),
        source="local" if repository.baseurl is None else "repo",
    )
    if repository.baseurl is None:
        package["location"] = location
    else:
        if size <= 0:
            fail(f"{package_id} has invalid download size {size}")
        package["size"] = size
        package["url"] = repository.baseurl.rstrip("/") + "/" + location.lstrip("/")
    return package


def resolved_against(repositories: list[Repository]) -> list[TransactionMetadata]:
    """What vouched for each remote repository's packages, from the manifest its materialization wrote.

    A repository whose packages vouch for themselves has no manifest, and a lock records nothing
    for it. Nor for a rolling repository: its metadata is fetched from where the mirror serves it
    now, which the mirror's next advance replaces, so there is nothing durable to record.
    """
    entries = []
    for repository in sorted(repositories, key=lambda repository: repository.id):
        manifest = repository.path / snapshotter.MANIFEST
        if repository.baseurl is None or not manifest.is_file():
            continue
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_data["pinned_at"] is None:
            continue
        entries.append(
            TransactionMetadata(
                repo=repository.id,
                source="metadata",
                files=manifest_data["files"],
                pinned_at=manifest_data["pinned_at"],
            )
        )
    return entries


def install_specs(install: list[str], install_files: list[str]) -> list[str]:
    """Return `install` followed by the install specs of the JSON lists `install_files`, without duplicates.

    An empty result is a solve with nothing to install. A solve that names neither is an error.
    """
    if not install and not install_files:
        fail("plan: a solve needs at least one install spec")
    found = dict.fromkeys(install)
    for path in install_files:
        found |= dict.fromkeys(json.loads(Path(path).read_text()))
    return list(found)


def write(path: Path, packages: list[TransactionPackage], repositories: list[Repository]) -> None:
    """Order a transaction before writing it, so identical solves give identical bytes."""
    packages.sort(
        key=lambda package: (
            package["repo"],
            package["package_id"],
            package["pkg_checksum"],
            package.get("location", ""),
            package.get("url", ""),
        )
    )
    entries: list[TransactionEntry] = [*packages, *resolved_against(repositories)]
    with text_destination(path) as output:
        json.dump(entries, output, indent=2)
        output.write("\n")


@contextmanager
def installed_root(lowers: Sequence[str]) -> Generator[Path]:
    # A snapshot store contains `uapi16.MANIFEST` at its top level. A box layer is a plain directory
    # tree, and no package installs a file of that name into the root.
    if (Path(lowers[-1]) / uapi16.MANIFEST).exists():
        with storage.root(lowers) as restored:
            yield restored.path
    else:
        with rootfs.rootfs("/installroot", lowers=lowers) as tree:
            yield tree
