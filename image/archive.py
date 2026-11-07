#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Merge a logical image into a deterministic tar, cpio, or directory.

Archive ownership is normalized to uid/gid 0. The newc cpio format has no general
extended-attribute representation.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import specs
import util

import cpio
import finalize
import tar


class Spec(finalize.ImageSpec):
    out: str
    format: str
    compression: str
    # Image paths holding the package database, stripped from the archive.
    pkgdb_paths: list[str]


def _clamp_mtimes(tree: Path, epoch: int) -> None:
    """Clamp directory-format mtimes without an archive filter."""
    for path in [tree, *tree.rglob("*")]:
        if path.lstat().st_mtime > epoch:
            os.utime(path, (epoch, epoch), follow_symlinks=False)


def _pack(tree: Path, out: Path, fmt: str, epoch: int) -> None:
    if fmt == "tar":
        tar.pack_tree(tree, out, epoch)
    elif fmt == "cpio":
        cpio.pack_tree(tree, out, epoch)
    else:
        util.fail(f"unknown archive format {fmt!r}")


def _archive(tree: Path, out: Path, fmt: str, epoch: int, compression: str) -> None:
    if fmt == "directory":
        if compression != "none":
            util.fail("archive: the directory format cannot be compressed")

        # Reject names that would wedge Buck while storing the thawed tree.
        for path in tree.rglob("*"):
            if "\\" in path.name:
                util.fail(
                    f"archive: {path.relative_to(tree)} contains a backslash, which buck cannot "
                    "store; the directory format cannot represent this image — use tar"
                )
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-a", "--reflink=auto", f"{tree}/.", str(out)], check=True)
        _clamp_mtimes(out, epoch)
    elif compression == "none":
        _pack(tree, out, fmt, epoch)
    elif compression != "zstd":
        util.fail(f"archive: unknown compression {compression!r}")
    else:
        # zstd needs the finished archive, so pack it beside the output rather than in TMPDIR: the
        # shared filesystem keeps the packer's reflink cloning working, and Buck only ever sees the
        # compressed result.
        with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
            raw = Path(scratch) / out.name
            _pack(tree, raw, fmt, epoch)
            util.compress_zstd(raw, out)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "archive", argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    with finalize.image(spec, readonly=False) as tree:
        # The package database is a supply-chain artifact that the image's `[pkgdb]` subtarget
        # captures separately, so an archive nothing resolves packages in can drop it.
        for relative in spec["pkgdb_paths"]:
            util.remove_path(tree / relative, with_parents=True)
        _archive(tree, out, spec["format"], epoch, spec["compression"])
    print(f"archive: wrote {spec['format']} (epoch={epoch}) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
