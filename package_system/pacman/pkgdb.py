#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Copy the alpm database out of a logical image as a separate, trimmed artifact.

Useful as a basis for SBOM creation and security scanners. The database is not shipped in
the image, so capture it as a separate artifact. alpm keeps a directory of per-package
entries rather than a database file, so the output is a reproducible archive of it.

Each entry's `mtree` is dropped: it is a manifest of the files the package shipped, which
pacman alone reads to verify an installed tree, and it is the largest part of an entry after
the file list. What identifies the package (`desc`) and what it owns (`files`) both stay.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import specs
import util

import alpm
import finalize
import tar


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "pkgdb", argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    with finalize.image(spec) as tree:
        source = tree / alpm.LOCAL_DB
        if not source.is_dir():
            util.fail(f"no alpm database at {source}; the image has no installed packages")
        entries = sorted(source.glob("*/desc"))
        if not entries:
            util.fail(f"no alpm database entries under {source}")
        # Beside the output rather than in TMPDIR, so reflink cloning keeps working on the way
        # through and Buck only ever sees the finished file.
        with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
            trimmed = Path(scratch) / "local"
            shutil.copytree(source, trimmed, ignore=shutil.ignore_patterns("mtree"))
            packed = Path(scratch) / out.name
            tar.pack_tree(trimmed, packed, epoch)
            util.compress_zstd(packed, out)
    print(f"pkgdb: captured {len(entries)} package entries -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
