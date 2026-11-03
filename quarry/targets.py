#!/usr/bin/python3
# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Write a release's unsigned TUF targets with hardhat. `quarry.release` runs this over a release folder.

The targets describe every published file with quarry's `custom.quarry` marks. Whoever publishes the
release imports them into a repository and signs that with its own keys. The marks are the contract a
build pipeline reads a release by, so they are spelled here and nowhere else. What a file is comes from
the release's description (`artifacts_<arch>.json`), never from its name: the kind and subkind, the
image and the verity root hash.

The sysupdate definitions are handed to hardhat as component dirs (`--quarry-transfer-dir`). hardhat
owns their layout in the repository (`.quarry/<component dir>/`). The definitions are not versioned.
Their component and feature files decide on which machines they are enabled.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict

# These are the supplemented-by media types of the SBOM formats by subkind. Both have IANA
# registrations.
_SBOM_TYPES = {
    "spdx": "application/spdx+json",
    "cyclonedx": "application/vnd.cyclonedx+json",
}


class Entry(TypedDict):
    """One file of the release's description, see docs/user/images.md."""

    kind: str
    subkind: NotRequired[str]
    image: str
    root_hash: NotRequired[str]
    version: NotRequired[str]


def _partition(entry: Entry) -> str | None:
    """Return the partition type of a partition, and None for any other file."""
    return entry.get("subkind") if entry["kind"] == "partition" else None


def _is_usr(entry: Entry) -> bool:
    """Return whether the file is a usr partition or one of its verity partitions."""
    partition = _partition(entry) or ""
    return partition == "usr" or partition.startswith("usr-")


def _is_ddi(entry: Entry) -> bool:
    """Return whether the file is an extension or a portable image, of any class."""
    return entry["kind"] in ("extension", "portable")


def is_update(entry: Entry) -> bool:
    """Return whether systemd-sysupdate installs the file.

    It installs UKIs, usr partitions, extensions and portable images.
    """
    return entry["kind"] == "uki" or _is_ddi(entry) or _is_usr(entry)


def selected(entry: Entry, selectors: list[str]) -> bool:
    """Return whether one of `selectors` picks the file.

    A selector is a kind, which picks every subkind of it, or `<kind>/<subkind glob>`.
    """
    kind = entry["kind"]
    subkind = entry.get("subkind")
    for selector in selectors:
        wanted, sep, pattern = selector.partition("/")
        if wanted == kind and (not sep or (subkind is not None and fnmatch.fnmatchcase(subkind, pattern))):
            return True
    return False


def supplement_type(entry: Entry) -> dict[str, str]:
    subkind = entry.get("subkind")
    if entry["kind"] == "sbom" and subkind in _SBOM_TYPES:
        return {"type": _SBOM_TYPES[subkind]}
    if entry["kind"] == "pkgdb":
        return {"type": "application/vnd.amutable.pkgdb"}
    return {}


def _of_image(files: dict[str, Entry], image: str, wanted: Callable[[Entry], bool]) -> list[str]:
    return sorted(n for n, e in files.items() if e["image"] == image and wanted(e))


def _scans(files: dict[str, Entry], image: str) -> list[str]:
    """Return the SBOMs and package database that describe `image`."""
    return _of_image(files, image, lambda e: e["kind"] in ("sbom", "pkgdb"))


def supplements(name: str, files: dict[str, Entry]) -> list[str]:
    """Return what is fetched alongside `name`, based on the description's roles.

    A UKI brings its usr partition and the initrd's listings. The usr partition brings its verity
    partitions and its own listings. An extension or a portable image brings its listings.
    """
    entry = files[name]
    image = entry["image"]
    if entry["kind"] == "uki":
        return _of_image(files, image, lambda e: _partition(e) == "usr") + _scans(files, image + ".initrd")
    if _partition(entry) == "usr":
        verity = _of_image(files, image, lambda e: (_partition(e) or "").startswith("usr-"))
        return verity + _scans(files, image)
    if _is_ddi(entry):
        return _scans(files, image)
    return []


def custom_marks(files: dict[str, Entry]) -> dict[str, Any]:
    """Return the `custom` object of every published target.

    The version mark is hardhat's (--custom-version).
    """
    custom: dict[str, Any] = {}
    for name, entry in sorted(files.items()):
        quarry: dict[str, Any] = {}
        if is_update(entry):
            quarry["update"] = True
        supp = supplements(name, files)
        if supp:
            quarry["supplemented-by"] = {n: supplement_type(files[n]) for n in supp}
        if "root_hash" in entry:
            quarry["verity-root-hash"] = entry["root_hash"]
        if quarry:
            custom[name] = {"quarry": quarry}
    return custom


def published(files: dict[str, Entry], exclude: list[str]) -> dict[str, Entry]:
    """Return the described files that become targets. These are all but the ones `exclude` selects."""
    kept = {n: e for n, e in files.items() if not selected(e, exclude)}
    for name in kept:
        if "/" in name or name.startswith("."):
            raise SystemExit(f"targets: published name {name!r} must be a plain file name")
    return kept


def release_version(files: dict[str, Entry]) -> str:
    """Return the version of the release, which all its versioned files share.

    A build pipeline expects one version per release, which every published name contains.
    """
    versions = sorted({e["version"] for e in files.values() if "version" in e})
    if len(versions) != 1:
        raise SystemExit(f"targets: a release has exactly one version, its files have {versions}")
    return versions[0]


def hardhat_transfer_args(dirs: list[str]) -> list[str]:
    """Return hardhat's --quarry-transfer-dir options for the transfer directories, as given to us.

    The directories are made absolute because hardhat runs from the stage, not from here.
    """
    argv: list[str] = []
    for spec in dirs:
        directory, sep, source = spec.partition("=")
        if not sep or not directory or not source:
            raise SystemExit(f"targets: --transfer-dir {spec!r} must be <component dir>=<source directory>")
        if not source.startswith("confext:"):
            source = os.path.abspath(source)
        argv += ["--quarry-transfer-dir", f"{directory}={source}"]
    return argv


def write_targets(args: argparse.Namespace) -> None:
    description = json.loads(Path(args.artifacts).read_text(encoding="utf-8"))
    files: dict[str, Entry] = published(description["files"], args.exclude)
    with tempfile.TemporaryDirectory(prefix="targets-stage.") as tmp:
        # The published files are staged as symlinks. hardhat follows them for files named on its
        # command line.
        stage = Path(tmp) / "files"
        stage.mkdir()
        for name in files:
            (stage / name).symlink_to(os.path.abspath(os.path.join(args.files, name)))

        # The custom marks go beside the stage, not in it, so no published name can collide with them.
        custom_path = Path(tmp) / "custom.json"
        custom_path.write_text(
            json.dumps(custom_marks(files), indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # All paths are absolute because hardhat runs from the stage, not the action's directory.
        argv = [
            os.path.abspath(args.hardhat),
            # hardhat ignores SOURCE_DATE_EPOCH and versions the targets by the wall-clock time.
            # Importing them into a repository keeps only their entries, so any fixed time will do.
            # TODO: Drop this once the pinned quarry has https://github.com/amutable/quarry/pull/97
            "--ref-time",
            datetime.fromtimestamp(int(os.environ["SOURCE_DATE_EPOCH"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "targets",
            # The file has no signatures and the zero time as its expiry, which nothing checks.
            "--unsigned",
            "--custom-from",
            os.fspath(custom_path),
            "--custom-version",
            release_version(files),
            "-o",
            os.fspath(Path(args.output).absolute()),
        ]
        argv += hardhat_transfer_args(args.transfer_dir)
        argv += sorted(files)
        # Run from the stage so the target paths come out relative to it.
        subprocess.run(argv, check=True, cwd=stage, stdout=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="targets", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--artifacts", required=True, help="This is the release's description (artifacts_<arch>.json)."
    )
    parser.add_argument("--files", required=True, help="This holds the published files under their names.")
    parser.add_argument("--output", required=True, help="The targets file goes here.")
    parser.add_argument(
        "--hardhat", required=True, help="This is quarry's hardhat, which writes the targets."
    )
    parser.add_argument(
        "--exclude", action="append", default=[], help="Files this selects (kind[/subkind]) are left out."
    )
    parser.add_argument(
        "--transfer-dir",
        action="append",
        default=[],
        help="This takes <component dir>=<source directory>, e.g. sysupdate.d=transfers/sysupdate.d.",
    )
    write_targets(parser.parse_args(argv))


if __name__ == "__main__":
    main()
