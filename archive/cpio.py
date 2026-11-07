# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read and write root-owned uncompressed newc archives with aligned file payloads.

Newc has no general extended-attribute representation; callers needing xattrs must use a
different terminal format.
"""

import mmap
import os
import stat
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path, PurePath, PurePosixPath
from typing import NamedTuple, Self

import util

import uapi16

# What an archive is read from, spelled concretely because the reader slices it: an mmap and a
# `bytes` both hand back `bytes`, where the `Buffer` protocol promises no indexing at all.
type Source = mmap.mmap | bytes

MAGIC = b"070701"
TRAILER = "TRAILER!!!"
_HEADER = 110  # 6-byte magic + 13 * 8-hex fields
_HEX_DIGITS = b"0123456789abcdefABCDEF"
# Fixed alignment keeps bytes reproducible across filesystems.
_BLOCK = 4096
# A newc name is a full path, so the kernel's initramfs unpacker bounds the namesize field by
# its PATH_MAX and silently skips larger entries (init/initramfs.c, do_header). That is the
# consuming kernel's fixed ABI constant (include/uapi/linux/limits.h), not a property of the
# build host, so it must not be queried via pathconf here.
_PATH_MAX = 4096


def _roundup(n: int, align: int) -> int:
    return n + (-n) % align


def _pwrite_all(fd: int, data: memoryview | bytes, off: int) -> None:
    mv = memoryview(data)
    while mv:
        n = os.pwrite(fd, mv, off)
        mv = mv[n:]
        off += n


# Reader.


class Entry(NamedTuple):
    """A decoded record, which names where its payload is rather than holding it."""

    ino: int
    mode: int
    nlink: int
    devmajor: int
    devminor: int
    name: str
    size: int
    data_off: int
    # A symlink's target, and empty for everything else: it is the one payload a caller needs the
    # bytes of, and small enough to copy. A regular file's is written from the source fd instead.
    target: bytes


def _align(start: int, pos: int) -> int:
    """Newc alignment is relative to the archive, not its containing file."""
    return start + _roundup(pos - start, 4)


def read(source: Source, start: int = 0) -> Iterator[Entry]:
    """Yield records from a newc archive embedded at `start` in `source`.

    Nothing yielded holds a view into `source`, which is what lets a caller abandon this loop on
    an error: an exported view outliving the frame that raised would make an mmap's close fail with
    a BufferError instead of the error that ended the extraction.
    """
    off = start
    while True:
        if source[off : off + 6] != MAGIC:
            raise ValueError(f"bad cpio magic at {off}: {source[off : off + 6]!r}")
        # 13 8-hex fields: ino, mode, uid, gid, nlink, mtime, filesize, devmajor, devminor,
        # rdevmajor, rdevminor, namesize, check.
        header = source[off + 6 : off + _HEADER]
        # int() also accepts a sign, blanks and underscores. A negative filesize can point the next
        # header offset back at this header, and the reader then loops forever.
        if len(header) != _HEADER - 6 or header.strip(_HEX_DIGITS):
            raise ValueError(f"bad cpio header at {off}: {header!r}")
        f = [int(header[i * 8 : (i + 1) * 8], 16) for i in range(13)]
        ino, mode, nlink, filesize, devmajor, devminor, namesize = (
            f[0], f[1], f[4], f[6], f[7], f[8], f[11],
        )  # fmt: skip
        name_off = off + _HEADER
        # First NUL drops both the stock terminator and any of our alignment padding.
        name = source[name_off : name_off + namesize].split(b"\x00", 1)[0].decode()
        data_off = _align(start, name_off + namesize)
        if name == TRAILER:
            return
        target = source[data_off : data_off + filesize] if stat.S_ISLNK(mode) else b""
        yield Entry(ino, mode, nlink, devmajor, devminor, name, filesize, data_off, target)
        off = _align(start, data_off + filesize)


def _relpath(name: str) -> str:
    """Normalize a trusted archive entry path beneath the destination."""
    return "/".join(p for p in name.split("/") if p not in ("", ".", ".."))


def _hardlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.hardlink_to(target)


def _put_regular(target: Path, e: Entry, src_fd: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        util.copy_range(fd, 0, src_fd, e.data_off, e.size)
    finally:
        os.close(fd)
    target.chmod(e.mode & 0o7777)


def _extract(source: Source, dest: Path, src_fd: int, start: int) -> int:
    count = 0
    canonical: dict[tuple[int, int, int], Path] = {}
    pending: dict[tuple[int, int, int], list[tuple[Path, int]]] = {}
    for e in read(source, start):
        rel = _relpath(e.name)
        if not rel:
            continue
        target = dest / rel
        fmt = e.mode & 0o170000
        # Hardlinkable iff a regular file sharing an inode (nlink > 1).
        key = (e.devmajor, e.devminor, e.ino) if e.nlink > 1 and fmt == 0o100000 else None
        if key is not None and e.size == 0:
            if key in canonical:
                _hardlink(target, canonical[key])
                count += 1
            else:
                pending.setdefault(key, []).append((target, e.mode))
            continue
        if fmt == 0o040000:  # directory
            # The mode a package ships a directory with is deliberately not applied: this runs once
            # per archive into one tree, and a later archive still has to write into a directory an
            # earlier one declared unwritable. `rootfs.capture` settles directory modes once
            # everything is in place.
            target.mkdir(parents=True, exist_ok=True)
        elif fmt == 0o120000:  # symlink (last-writer-wins across multi-archive extracts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            target.symlink_to(os.fsdecode(e.target))
        elif fmt == 0o100000:  # regular file
            _put_regular(target, e, src_fd)
        else:  # device/fifo/socket
            continue
        count += 1
        if key is not None:  # this entry carried the content; flush deferred links
            canonical[key] = target
            for link, _ in pending.pop(key, []):
                _hardlink(link, target)
                count += 1
    for links in pending.values():  # a fully-empty hardlink set has no content carrier
        first, mode = links[0]
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        first.chmod(mode & 0o7777)
        count += 1
        for target, _ in links[1:]:
            _hardlink(target, first)
            count += 1
    return count


def unpack(fd: int, dest: Path, *, offset: int = 0) -> int:
    """Extract a newc archive from `fd` at `offset`, returning the entry count."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if os.fstat(fd).st_size == 0:
        return 0
    with mmap.mmap(fd, 0, prot=mmap.PROT_READ) as mm:
        try:
            return _extract(mm, dest, fd, offset)
        except ValueError as error:
            # What the reader raises for an archive that is not the format it claims, which is an
            # input being wrong rather than a bug worth a traceback.
            util.fail(f"cpio: {error}")


# Writer.


class Writer:
    """Stream a reproducible archive with fixed ownership and clamped mtimes."""

    def __init__(self, path: Path, epoch: int, *, block_align: bool = True) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        self._epoch = epoch
        self._pos = 0
        self._ino = 0
        self._links: dict[int, tuple[int, int]] = {}
        # Alignment pads the name field, and the kernel's early cpio reader (lib/earlycpio.c) refuses
        # an entry whose name field exceeds its 18-byte name buffer, so an archive it reads before
        # unpacking anything, microcode, is written without.
        self._block_align = block_align

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is not None:  # error mid-archive: release the fd but don't finalize a partial archive
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            return
        self.close()

    def _write(self, data: bytes) -> None:
        _pwrite_all(self._fd, data, self._pos)
        self._pos += len(data)

    def _pad(self, align: int) -> None:
        if pad := (-self._pos) % align:
            self._write(b"\x00" * pad)

    def _header(
        self,
        name: str,
        mode: int,
        filesize: int,
        *,
        mtime: int,
        block_align: bool,
        ino: int | None = None,
        nlink: int = 1,
        major: int = 0,
        minor: int = 0,
    ) -> None:
        self._ino += 1
        if name == TRAILER:
            name = "./" + name
        raw = name.encode()
        namesize = len(raw) + 1  # name + its NUL terminator
        if block_align:
            # Newc namesize permits padding file data to the reflink boundary — unless the
            # padding would push namesize past PATH_MAX; then the entry stays unaligned.
            padded = _roundup(self._pos + _HEADER + namesize, _BLOCK) - (self._pos + _HEADER)
            if padded <= _PATH_MAX:
                namesize = padded
        fields = (
            self._ino if ino is None else ino,
            mode,
            0,
            0,
            nlink,
            min(mtime, self._epoch),
            filesize,
            0,
            0,
            major,
            minor,
            namesize,
            0,
        )
        self._write(MAGIC + b"".join(b"%08x" % v for v in fields))
        self._write(raw + b"\x00" * (namesize - len(raw)))
        self._pad(4)

    def _link(self, link: tuple[int, int] | None) -> tuple[int | None, int, int]:
        if link is None:
            return None, 1, 1
        token, nlink = link
        ino, remaining = self._links.get(token, (self._ino + 1, nlink))
        self._links[token] = (ino, remaining - 1)
        return ino, nlink, remaining

    def add_dir(self, name: str, *, mode: int, mtime: int) -> None:
        self._header(name, stat.S_IFDIR | (mode & 0o7777), 0, mtime=mtime, block_align=False)

    def add_symlink(
        self, name: str, target: str, *, mtime: int, link: tuple[int, int] | None = None
    ) -> None:
        ino, nlink, _ = self._link(link)
        data = os.fsencode(target)
        self._header(
            name, stat.S_IFLNK | 0o777, len(data), mtime=mtime, block_align=False, ino=ino, nlink=nlink
        )
        self._write(data)
        self._pad(4)

    def add_node(
        self,
        name: str,
        *,
        mode: int,
        mtime: int,
        major: int = 0,
        minor: int = 0,
        link: tuple[int, int] | None = None,
    ) -> None:
        ino, nlink, _ = self._link(link)
        self._header(
            name, mode, 0, mtime=mtime, block_align=False, major=major, minor=minor, ino=ino, nlink=nlink
        )

    def add_file(
        self,
        name: str,
        src: Path,
        *,
        mode: int,
        mtime: int,
        link: tuple[int, int] | None = None,
    ) -> None:
        ino, nlink, remaining = self._link(link)
        if remaining > 1:
            self._header(name, stat.S_IFREG | mode, 0, mtime=mtime, block_align=False, ino=ino, nlink=nlink)
            return
        fd = os.open(src, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            self._header(
                name,
                stat.S_IFREG | (mode & 0o7777),
                size,
                mtime=mtime,
                block_align=self._block_align and size >= _BLOCK,
                ino=ino,
                nlink=nlink,
            )
            util.copy_range(self._fd, self._pos, fd, 0, size)
            self._pos += size
        finally:
            os.close(fd)
        self._pad(4)

    def close(self) -> None:
        if self._fd < 0:
            return
        # TRAILER!!!: nlink=1, ino 0, everything else zero (the stock convention).
        fields = (0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(TRAILER) + 1, 0)
        self._write(MAGIC + b"".join(b"%08x" % v for v in fields))
        self._write(TRAILER.encode() + b"\x00")
        self._pad(4)
        os.close(self._fd)
        self._fd = -1


def _add(w: Writer, tree: Path, path: Path, link: tuple[int, int] | None = None) -> bool:
    """Add one path under `tree`; false for a type an initramfs has no use for."""
    st = path.lstat()
    name = str(path.relative_to(tree))
    mode = stat.S_IMODE(st.st_mode)
    mtime = st.st_mtime_ns // 10**9
    if stat.S_ISDIR(st.st_mode):
        w.add_dir(name, mode=mode, mtime=mtime)
    elif stat.S_ISLNK(st.st_mode):
        w.add_symlink(name, os.readlink(path), mtime=mtime)
    elif stat.S_ISREG(st.st_mode):
        w.add_file(name, path, mode=mode, mtime=mtime, link=link)
    else:
        return False  # devices/fifos/sockets: unneeded in an initramfs (systemd mounts /dev)
    return True


def pack_tree(tree: Path, out: Path, epoch: int) -> int:
    """Pack a whole tree into `out` without block alignment, which the kernel's early cpio reader needs."""
    tree = Path(tree)
    return _pack_paths(tree, out, epoch, sorted(tree.rglob("*")), block_align=False)


def pack_paths(tree: Path, out: Path, epoch: int, paths: Iterable[str | PurePath]) -> int:
    """Pack exactly the given tree-relative paths into `out`.

    Sorting is what puts a directory before its contents, which the kernel's unpacker needs.
    """
    tree = Path(tree)
    return _pack_paths(
        tree, out, epoch, [tree / rel for rel in sorted({PurePosixPath(path) for path in paths})]
    )


def _pack_paths(
    tree: Path, out: Path, epoch: int, paths: Sequence[Path], *, block_align: bool = True
) -> int:
    inodes = {path: path.lstat() for path in paths}
    counts = Counter((st.st_dev, st.st_ino) for st in inodes.values() if stat.S_ISREG(st.st_mode))
    tokens: dict[tuple[int, int], int] = {}
    count = 0
    with Writer(out, epoch, block_align=block_align) as w:
        for path, st in inodes.items():
            inode = st.st_dev, st.st_ino
            link = None
            if stat.S_ISREG(st.st_mode) and counts[inode] > 1:
                link = (tokens.setdefault(inode, len(tokens) + 1), counts[inode])
            count += _add(w, tree, path, link)
    return count


def pack_manifest(
    objects: Sequence[uapi16.Object],
    content: Callable[[uapi16.Object], Path],
    out: Path,
    epoch: int,
) -> None:
    counts = Counter(obj.get("inodeToken") for obj in objects if obj.get("inodeToken"))
    kinds = {"fifo": stat.S_IFIFO, "chr": stat.S_IFCHR, "blk": stat.S_IFBLK, "sock": stat.S_IFSOCK}
    with Writer(out, epoch) as w:
        for obj in objects:
            kind = obj["type"]
            name, mode, mtime = obj.get("name", "."), obj.get("mode", 0o755), obj["mTimeNSec"] // 10**9
            token = obj.get("inodeToken")
            link = (token, counts[token]) if token else None
            if kind == "reg":
                w.add_file(name, content(obj), mode=mode, mtime=mtime, link=link)
            elif kind == "dir":
                w.add_dir(name, mode=mode, mtime=mtime)
            elif kind == "lnk":
                w.add_symlink(name, uapi16.symlink_target(obj), mtime=mtime, link=link)
            else:
                w.add_node(
                    name,
                    mode=kinds[kind] | mode,
                    mtime=mtime,
                    major=obj.get("major", 0),
                    minor=obj.get("minor", 0),
                    link=link,
                )
