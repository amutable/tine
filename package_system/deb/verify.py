#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Verify upstream debs against the signed Release of a repository.

Debian signs the Release and nothing else. The Release lists the checksum of every index, and an
index lists the checksum of every package. The verifier follows this chain. sqv checks the
signature of the pinned Release against the declared keys. The Release must name the suite of the
directory that it is in, and it must list the pinned index at the path of the index. The index
must list each selected package. A committed lock retains the Release and the index that it was
resolved against. The retained files verify a package that the pinned index no longer describes.

The keys of the archive sign the Release directly, so sqv reads the declared key files as they
are, and no keyring is built.
"""

import functools
import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypedDict

import deb822
import release
import specs
import util

import snapshotter


class Spec(TypedDict):
    # Maps the declared fingerprint of a key to the path of its key file.
    keys: dict[str, str]
    out: str
    # Maps the name of a verified copy in `out` (<deb name>--<sha256>.deb) to the unverified
    # <sha256>.deb.
    packages: dict[str, str]
    # The directory of the pinned repository. It holds the signed Release, the pinned index, and
    # the older pairs of Release and index that committed locks retain.
    repository: str
    # The time of the repository's snapshot as ISO 8601 in UTC. With None, the verifier uses the
    # current time.
    time: str | None


def instant(time: str) -> datetime:
    """Parse an ISO 8601 time and return it in UTC."""
    try:
        when = datetime.fromisoformat(time)
    except ValueError:
        util.fail(f"verify: {time!r} is not an ISO 8601 time")
    return (when if when.tzinfo is not None else when.replace(tzinfo=UTC)).astimezone(UTC)


def authenticated(keys: dict[str, str], signed: Path, message: Path, at: datetime | None) -> bytes:
    """Verify the signature of a Release with sqv and return the signed text.

    One valid signature from a declared key is enough, as it is for APT. The archive signs with its
    current key and with the previous key, so a repository can declare either. sqv prints the
    fingerprint of each key with a valid signature. A fingerprint only counts if the repository
    declared it, so a key file that holds a different key than declared verifies nothing.

    sqv checks a key at the time of the signature, so a key that has expired since then still
    verifies. With `at`, sqv refuses a signature that was made after `at`.
    """
    if not signed.is_file():
        util.fail(f"verify: {signed.parent} carries no {signed.name}; run refresh-catalog")
    command = ["sqv", *(argument for file in keys.values() for argument in ("--keyring", file))]
    if at is not None:
        command += ["--time", at.strftime("%Y-%m-%dT%H:%M:%SZ")]
    command += ["--cleartext", "--output", str(message), str(signed)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        util.fail(
            f"verify: {signed.name}: no valid signature from the declared keys: {result.stderr.strip()}"
        )
    declared = {fingerprint.upper() for fingerprint in keys}
    signers = set(result.stdout.split())
    if not signers & declared:
        util.fail(
            f"verify: {signed.name}: no valid signature from the declared keys: signed by"
            f" {' '.join(sorted(signers))}, declared under another fingerprint"
        )
    print(f"verify: {signed.name} signed by {' '.join(sorted(signers & declared))}", file=sys.stderr)
    return message.read_bytes()


def _digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as raw:
        return hashlib.file_digest(raw, "sha256").hexdigest(), path.stat().st_size


def vouched(stated: dict[str, tuple[str, int]], suite: Path, index: Path) -> None:
    """Fail unless the Release lists the pinned index at its path, with the same checksum and size.

    The lookup uses the full path. The Release lists one index per component and architecture, and
    all of them have the same file name.
    """
    path = index.relative_to(suite).as_posix()
    digest, size = _digest(index)
    if stated.get(path) != (digest, size):
        util.fail(f"verify: the signed Release states no {path} of checksum {digest} and {size} bytes")


def _stated_at(what: str, value: str) -> datetime:
    """Parse a date field of a Release, which is in RFC 2822 format."""
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        util.fail(f"verify: {what} is not an RFC 2822 date: {value!r}")
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def unexpired(what: str, stanza: dict[str, str], at: datetime | None) -> None:
    """Fail if the `Valid-Until` date of the Release is before `at`.

    The signature of an old Release stays valid, so a mirror can serve an outdated Release that
    still verifies. `Valid-Until` limits how long clients accept a Release. For a pinned repository,
    `at` is the time of the pin, so the build fails if the mirror answered a refresh with a Release
    that had expired by then. For an unpinned repository, `at` is the current time, as it is for APT.
    """
    until = stanza.get("valid-until")
    if until is None:
        return
    at = at or datetime.now(UTC)
    if _stated_at(f"{what} Valid-Until", until) < at:
        util.fail(f"verify: {what} expired at {until}, before the {at.isoformat()} it is judged as of")


def vouched_packages(
    keys: dict[str, str], time: str | None, scratch: Path, generation: Path
) -> dict[str, int]:
    """Verify one generation and return the size of every package in its index, keyed by checksum.

    A generation is a signed Release with its index. The verifier checks a retained generation at
    the time that the lock recorded for it, and the pinned generation at the time of the repository.
    """
    time = snapshotter.pinned_at(generation) or time
    at = None if time is None else instant(time)
    index = deb822.package_index(generation, release.INRELEASE)
    suite = deb822.suite_directory(index)
    # sqv refuses to overwrite a file, so each generation gets its own output file.
    message = authenticated(keys, suite / release.INRELEASE, scratch / generation.name, at)
    stanza = release.stanza(release.INRELEASE, message)
    if suite.parents[0].name == deb822.SUITES:
        release.named(release.INRELEASE, stanza, suite.name)
    unexpired(release.INRELEASE, stanza, at)
    vouched(release.stated(release.INRELEASE, stanza), suite, index)
    return {package.sha256: package.size for package in deb822.packages(index, release.INRELEASE)}


def verify(spec: Spec) -> None:
    """Fail unless a signed Release covers every package, then copy the packages to `out`."""
    out = Path(spec["out"])
    out.mkdir(parents=True)
    rejected = []
    with tempfile.TemporaryDirectory(prefix="verify.") as scratch:
        sizes = snapshotter.Vouching(
            Path(spec["repository"]),
            functools.partial(vouched_packages, spec["keys"], spec["time"], Path(scratch)),
        )
        for name, package in spec["packages"].items():
            digest, size = _digest(Path(package))
            if digest != snapshotter.pool_checksum("verify", package):
                rejected.append(f"{name}: its contents are not the checksum it is named by")
                continue
            stated = sizes.get(digest)
            if stated is None:
                rejected.append(
                    f"{name}: no signed Release the repository carries vouches for checksum {digest}; a"
                    " lock that selected it before its metadata was retained needs re-resolving"
                )
            elif stated != size:
                rejected.append(f"{name}: is {size} bytes, not the {stated} the vouched-for index states")
    if rejected:
        util.fail("verify: not vouched for by the signed Release\n  " + "\n  ".join(rejected))
    for name, package in spec["packages"].items():
        util.clone_file(Path(package), out / name)
    print(f"verify: {len(spec['packages'])} package(s) verified", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    verify(specs.parse(Spec, "verify", argv))


if __name__ == "__main__":
    main()
