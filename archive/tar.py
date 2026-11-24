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
from typing import BinaryIO

import util

import uapi16

# Regular files of at least one filesystem block start their data on a block boundary, so that the archive
# can share their data blocks on btrfs or XFS.
_BLOCK = 4096


def _aligned_header(archive: tarfile.TarFile, info: tarfile.TarInfo) -> bytes:
    """Return the headers of `info` padded so that its data starts on a filesystem block boundary."""

    def header() -> bytes:
        return info.tobuf(tarfile.PAX_FORMAT, archive.encoding, archive.errors)

    plain = header()
    if (archive.offset + len(plain)) % _BLOCK == 0:
        return plain
    # Tar readers ignore a pax record with the "comment" keyword, so a comment record can pad the extended
    # header. The extended header grows in steps of one tar block.
    headers = dict(info.pax_headers)
    info.pax_headers = headers | {"comment": ""}
    shortest = len(header())
    target = shortest + (-(archive.offset + shortest)) % _BLOCK
    # The length field of a pax record counts its own digits. One more byte of padding can therefore grow
    # the record by two bytes.
    padding = target - shortest
    while True:
        info.pax_headers = headers | {"comment": "0" * padding}
        buf = header()
        if len(buf) == target:
            info.pax_headers = headers
            return buf
        padding += -1 if len(buf) > target else 1


def _add_aligned(archive: tarfile.TarFile, fileobj: BinaryIO, info: tarfile.TarInfo, source: Path) -> None:
    """Add a regular file and copy its data with copy_file_range(), which reflinks it on btrfs or XFS.

    `fileobj` is the file that `archive` writes to.
    """
    buf = _aligned_header(archive, info)
    fileobj.write(buf)
    fileobj.flush()
    archive.offset += len(buf)
    with source.open("rb") as stream:
        util.copy_range(fileobj.fileno(), archive.offset, stream.fileno(), 0, info.size)
    archive.offset += info.size
    fileobj.seek(archive.offset)
    if remainder := info.size % tarfile.BLOCKSIZE:
        fileobj.write(tarfile.NUL * (tarfile.BLOCKSIZE - remainder))
        archive.offset += tarfile.BLOCKSIZE - remainder


def pack_tree(tree: Path, out: Path, epoch: int) -> None:
    """Pack a whole tree into `out`, with modification times clamped to `epoch`."""
    objects = uapi16.tree_objects(tree, list(uapi16.entries(tree)), epoch=epoch)[1:]
    # The deb and pacman package databases compress the archive that pack_tree() writes. A compressed
    # archive shares no data blocks.
    pack_manifest(objects, lambda obj: tree / obj["name"], out, align=False)


def pack_manifest(
    objects: Sequence[uapi16.Object],
    content: Callable[[uapi16.Object], Path],
    out: Path,
    *,
    align: bool,
) -> None:
    """Pack `objects` into `out`, reading the data of a regular file from `content`.

    With `align`, the data of each regular file of at least one filesystem block starts on a block
    boundary.
    """
    kinds = {
        "reg": tarfile.REGTYPE,
        "dir": tarfile.DIRTYPE,
        "lnk": tarfile.SYMTYPE,
        "chr": tarfile.CHRTYPE,
        "blk": tarfile.BLKTYPE,
        "fifo": tarfile.FIFOTYPE,
    }
    groups = uapi16.hardlink_groups(objects)
    with (
        out.open("wb") as fileobj,
        tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
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
            if align and info.isreg() and info.size >= _BLOCK:
                _add_aligned(archive, fileobj, info, content(obj))
            elif info.isreg():
                with content(obj).open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
