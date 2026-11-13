# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Deterministic tar archives.

Entries are written in sorted order with ownership normalized to uid/gid 0 and mtimes clamped to
the build's epoch, so the same tree packs to the same bytes on any machine.
"""

import os
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import uapi16


def _xattrs(path: Path) -> dict[str, str]:
    """Encode Linux xattrs using the convention understood by GNU tar and star."""
    headers = {}
    for name in sorted(os.listxattr(path, follow_symlinks=False)):
        if name.startswith(("user.overlay.", "trusted.overlay.")):
            continue
        value = os.getxattr(path, name, follow_symlinks=False)
        headers["SCHILY.xattr." + name] = value.decode("utf-8", "surrogateescape")
    return headers


def _reproducible(path: Path, epoch: int) -> Callable[[tarfile.TarInfo], tarfile.TarInfo]:
    """Return a tar filter that normalizes ownership, mtimes, and extended attributes."""

    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = min(int(info.mtime), epoch)
        info.pax_headers = dict(info.pax_headers) | _xattrs(path)
        return info

    return reset


def pack_tree(tree: Path, out: Path, epoch: int) -> int:
    """Pack a whole tree into `out`."""
    tree = Path(tree)
    count = 0
    # Explicit sorted entries keep archive order stable.
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(tree.rglob("*")):
            archive.add(
                path,
                arcname="./" + str(path.relative_to(tree)),
                recursive=False,
                filter=_reproducible(path, epoch),
            )
            count += 1
    return count


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
    links: dict[int, str] = {}
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for obj in objects:
            kind = obj["type"]
            if kind not in kinds:
                raise ValueError(f"tar cannot represent inode type {kind!r} at {obj.get('name', '.')!r}")
            info = tarfile.TarInfo("./" + obj["name"] if "name" in obj else ".")
            info.type = kinds[kind]
            info.mode = obj.get("mode", 0o777 if kind == "lnk" else 0o755)
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
            if token and token in links:
                info.type, info.linkname = tarfile.LNKTYPE, links[token]
            elif kind == "lnk":
                info.linkname = uapi16.symlink_target(obj)
            elif kind == "reg":
                info.size = obj["size"]
            if token:
                links.setdefault(token, info.name)
            if info.isreg():
                with content(obj).open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
