#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Export image snapshots as reproducible archives or directory stores."""

import argparse
import os
import sys
import tempfile
from contextlib import chdir
from pathlib import Path

import specs
import util

import cpio
import finalize
import storage
import tar
import uapi16
from isolation import AT_FDCWD, Close, open_directory, unshare_sandbox


class Spec(finalize.ImageSpec):
    out: str
    format: str
    compression: str
    pkgdb_paths: list[str]
    manifest: str | None


def _pack(snapshot: storage.Snapshot, out: Path, fmt: str, epoch: int) -> None:
    if fmt == "tar":
        tar.pack_manifest(snapshot.objects, snapshot.content, out)
    elif fmt == "cpio":
        cpio.pack_manifest(snapshot.objects, snapshot.content, out, epoch)
    else:
        util.fail(f"unknown archive format {fmt!r}")


def _archive(snapshot: storage.Snapshot, out: Path, fmt: str, epoch: int, compression: str) -> None:
    if fmt == "directory":
        if compression != "none":
            util.fail("archive: the directory format cannot be compressed")
        storage.save(snapshot, out)
    elif compression == "none":
        _pack(snapshot, out, fmt, epoch)
    elif compression != "zstd":
        util.fail(f"archive: unknown compression {compression!r}")
    else:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            raw = Path(scratch) / out.name
            _pack(snapshot, raw, fmt, epoch)
            util.compress_zstd(raw, out)


def export(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="image-directory")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    # Restoration changes the working directory, so blob references need an absolute store.
    snapshot = storage.Snapshot([args.store.absolute()])
    with Close(open_directory(AT_FDCWD, str(args.destination.parent))) as parent:
        parent_attributes = os.fstat(parent)
        # A user namespace shows an owner that it doesn't map as the overflow uid. The caller's own uid in
        # the namespace can be the overflow uid as well. A parent owned by the overflow uid is never trusted.
        overflow_uid = int(Path("/proc/sys/kernel/overflowuid").read_text())
        if (
            parent_attributes.st_uid not in (0, os.geteuid())
            or parent_attributes.st_uid == overflow_uid
            or (parent_attributes.st_mode & 0o022 and not parent_attributes.st_mode & 0o1000)
        ):
            util.fail(f"image-directory: export needs a protected parent: {args.destination.parent}")
        unshare_sandbox(become_root=True)
        name = args.destination.name or "."
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            util.fail(f"image-directory: destination already exists: {args.destination}")
        with (
            Close(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)) as directory,
            chdir("."),
        ):
            attributes = os.fstat(directory)
            if attributes.st_uid != os.geteuid() or attributes.st_mode & 0o077 or os.listdir(directory):
                util.fail(f"image-directory: destination changed before export: {args.destination}")
            os.fchdir(directory)
            storage.restore(snapshot, Path("."))


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["export"]:
        export(arguments[1:])
        return
    spec = specs.parse(Spec, "archive", arguments)
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    with finalize.snapshot(
        spec, program="archive", epoch=epoch, pkgdb_paths=spec["pkgdb_paths"]
    ) as snapshot:
        if spec["format"] == "cpio":
            for obj in snapshot.objects:
                obj["mTimeNSec"] = obj["mTimeNSec"] // 10**9 * 10**9
                obj.pop("extendedAttributes", None)
        _archive(snapshot, out, spec["format"], epoch, spec["compression"])
        if spec["manifest"]:
            uapi16.write_listing(snapshot.objects, Path(spec["manifest"]))
    print(f"archive: wrote {spec['format']} (epoch={epoch}) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
