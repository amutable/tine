#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Write archives and snapshot stores of images, and export snapshot stores as directories."""

import argparse
import os
import shutil
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
    manifest: str | None


def _pack(snapshot: uapi16.Snapshot, out: Path, fmt: str, epoch: int) -> None:
    if fmt == "tar":
        tar.pack_manifest(snapshot.objects, snapshot.content, out)
    elif fmt == "cpio":
        cpio.pack_manifest(snapshot.objects, snapshot.content, out, epoch)
    else:
        util.fail(f"unknown archive format {fmt!r}")


def _archive(snapshot: uapi16.Snapshot, out: Path, fmt: str, epoch: int, compression: str) -> None:
    if fmt == "snapshot":
        if compression != "none":
            util.fail("archive: the snapshot format cannot be compressed")
        uapi16.save(snapshot, out)
    elif compression == "none":
        _pack(snapshot, out, fmt, epoch)
    elif compression != "zstd":
        util.fail(f"archive: unknown compression {compression!r}")
    else:
        # zstd needs the finished archive. In an action, /var/tmp is the scratch directory of Buck, which
        # is on the same filesystem as the store. The cpio packer can therefore clone blobs into the
        # uncompressed archive.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            raw = Path(scratch) / out.name
            _pack(snapshot, raw, fmt, epoch)
            util.compress_zstd(raw, out)


def export(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="image-export")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    # Restoration changes the working directory, so blob references need an absolute store.
    store = args.store.absolute()
    # A user can export a store that Buck did not write, such as a copy.
    try:
        snapshot = uapi16.Snapshot([store])
    except (OSError, ValueError) as error:
        util.fail(f"image-export: cannot read the store {store}: {error}")
    uapi16.check_blobs(snapshot)
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
            util.fail(f"image-export: export needs a protected parent: {args.destination.parent}")
        unshare_sandbox(become_root=True)
        name = args.destination.name or "."
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            util.fail(f"image-export: destination already exists: {args.destination}")
        with (
            Close(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)) as directory,
            chdir("."),
        ):
            attributes = os.fstat(directory)
            if attributes.st_uid != os.geteuid() or attributes.st_mode & 0o077 or os.listdir(directory):
                util.fail(f"image-export: destination changed before export: {args.destination}")
            os.fchdir(directory)
            try:
                storage.restore(snapshot, Path("."))
            except BaseException:
                # The checks above made sure that only we can change names in the parent, so the
                # name still refers to the directory we created.
                shutil.rmtree(name, dir_fd=parent)
                raise


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["export"]:
        export(arguments[1:])
        return
    spec = specs.parse(Spec, "archive", arguments)
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    snapshot = uapi16.Snapshot(spec["lower"])
    if spec["format"] == "tar":
        # tar has no socket type, so neither the archive nor its listing contains sockets.
        snapshot.objects = [obj for obj in snapshot.objects if obj["type"] != "sock"]
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
