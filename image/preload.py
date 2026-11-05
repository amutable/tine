#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Add files to the ESP of a disk, and leave every other byte of the disk alone."""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import TypedDict

import specs
from util import fail

from convert import convert


class Spec(TypedDict):
    base: str
    # each absolute path on the ESP -> the file to put there
    files: dict[str, str]
    # one of ddi.bzl's DISK_FORMATS
    format: str
    out: str
    virtual_size: str | None


def esp_offset(disk: Path) -> int:
    """Return the byte offset of the one ESP partition of `disk`."""
    with tempfile.TemporaryDirectory() as empty:
        listing = subprocess.run(
            [
                "systemd-repart",
                "--dry-run=yes",
                f"--definitions={empty}",
                "--json=short",
                "--no-pager",
                str(disk),
            ],
            check=True,
            stdout=subprocess.PIPE,
            env=os.environ | {"SYSTEMD_LOG_LEVEL": "warning"},
        )
    offsets = [partition["offset"] for partition in json.loads(listing.stdout) if partition["type"] == "esp"]
    if len(offsets) != 1:
        fail(f"preload: the base disk needs exactly one ESP partition, it has {len(offsets)}")
    return int(offsets[0])


def directories(present: Iterable[str], targets: Iterable[str]) -> list[str]:
    """Return the directories to create for `targets`, parents first, on a FAT with `present` paths.

    FAT compares names regardless of case. A target that is present already fails, as the base
    carries a file of that name.
    """
    known = {path.casefold() for path in present}
    missing = []
    for target in sorted(targets):
        if target.casefold() in known:
            fail(f"preload: the base disk's ESP already contains {target}")
        for parent in reversed(PurePosixPath(target).parents[:-1]):
            if str(parent).casefold() not in known:
                known.add(str(parent).casefold())
                missing.append(str(parent))
    return missing


def add(image: str, files: dict[str, str]) -> None:
    """Copy `files` onto the FAT that the mtools drive `image` names, creating their directories."""
    # mdir lists a directory with a trailing "/", below the drive prefix "::".
    listing = subprocess.run(
        ["mdir", "-i", image, "-/", "-b", "::/"], check=True, stdout=subprocess.PIPE, text=True
    )
    present = [line.removeprefix("::").rstrip("/") for line in listing.stdout.splitlines()]
    # mmd and mcopy refuse an existing name with exit status 1 and no message, so directories() checks first.
    if missing := directories(present, files):
        subprocess.run(["mmd", "-i", image, *(f"::{directory}" for directory in missing)], check=True)
    for target in sorted(files):
        subprocess.run(["mcopy", "-i", image, files[target], f"::{target}"], check=True)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "preload", argv)
    base = Path(spec["base"])
    with tempfile.TemporaryDirectory() as tmp:
        disk = Path(tmp) / "disk.raw"
        # Both keep the holes of a sparse disk. Without -f, zstd skips an input that is a symlink,
        # which Buck may materialize an artifact as.
        if base.name.endswith(".zst"):
            subprocess.run(["zstd", "-q", "-f", "-d", "-o", str(disk), str(base)], check=True)
        else:
            subprocess.run(["cp", "--sparse=always", str(base), str(disk)], check=True)
        add(f"{disk}@@{esp_offset(disk)}", spec["files"])
        convert(disk, Path(spec["out"]), spec["format"], spec["virtual_size"])


if __name__ == "__main__":
    main()
