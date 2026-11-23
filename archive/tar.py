# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Deterministic tar archives.

Entries are written in manifest order with ownership normalized to uid/gid 0, so the same tree packs to
the same bytes on any machine. pack_manifest() keeps the modification times of the objects, which the
listing of the tree or the capture of the snapshot clamped to the build's epoch.
"""

import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import uapi16


def pack_tree(tree: Path, out: Path, epoch: int) -> None:
    """Pack a whole tree into `out`, with modification times clamped to `epoch`."""
    objects = uapi16.tree_objects(tree, list(uapi16.entries(tree)), epoch=epoch)[1:]
    pack_manifest(objects, lambda obj: tree / obj["name"], out)


def pack_manifest(
    objects: Sequence[uapi16.Object],
    content: Callable[[uapi16.Object], Path],
    out: Path,
) -> None:
    kinds = {
        "reg": tarfile.REGTYPE,
        "dir": tarfile.DIRTYPE,
        "lnk": tarfile.SYMTYPE,
        "chr": tarfile.CHRTYPE,
        "blk": tarfile.BLKTYPE,
        "fifo": tarfile.FIFOTYPE,
    }
    groups = uapi16.hardlink_groups(objects)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for obj in objects:
            kind = obj["type"]
            info = tarfile.TarInfo("./" + obj["name"] if "name" in obj else ".")
            info.type = kinds[kind]
            info.mode = 0o777 if kind == "lnk" else obj["mode"]
            headers: dict[str, str] = {}
            ns = obj["mTimeNSec"]
            info.mtime = ns // 10**9
            if ns % 10**9:
                headers["mtime"] = f"{ns // 10**9}.{ns % 10**9:09}"
            info.devmajor, info.devminor = obj.get("major", 0), obj.get("minor", 0)
            for name, value in obj.get("extendedAttributes", {}).get("user", {}).items():
                data = uapi16.xattr_value(value)
                headers["SCHILY.xattr.user." + name] = data.decode("utf-8", "surrogateescape")
            info.pax_headers = headers
            token = obj.get("inodeToken")
            if token and groups[token][0] != obj["name"]:
                info.type, info.linkname = tarfile.LNKTYPE, "./" + groups[token][0]
            elif kind == "lnk":
                info.linkname = uapi16.symlink_target(obj)
            elif kind == "reg":
                info.size = obj["size"]
            if info.isreg():
                with content(obj).open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
