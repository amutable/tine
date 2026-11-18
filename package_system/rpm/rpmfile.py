# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Frame RPM headers and stream payload decompression.

This stdlib-only path lets the bootstrap extractor read RPMs without the RPM stack.
"""

import lzma
import mmap
import os
import shutil
import struct
import zlib
from collections import Counter
from collections.abc import Buffer
from pathlib import Path
from typing import BinaryIO, NamedTuple

import util

MAGIC = b"\x8e\xad\xe8\x01"
LEAD = 96
CPIO_MAGIC = b"070701"  # newc — an already-uncompressed payload passes through


def _header_end(view: memoryview, off: int) -> int:
    if bytes(view[off : off + 4]) != MAGIC:
        util.fail(f"bad rpm header magic at {off}: {bytes(view[off : off + 4]).hex()}")
    if len(view) < off + 16:
        util.fail(f"truncated rpm header at {off}")
    nindex, nbytes = struct.unpack(">II", view[off + 8 : off + 16])
    return off + 16 + 16 * nindex + nbytes


def payload_offset(data: Buffer) -> int:
    """Return where the payload starts in a buffer-like RPM, past the lead and both headers."""
    # A view that outlives the call, for example in the traceback of a failure, keeps an mmap from closing.
    with memoryview(data) as view:
        signature = _header_end(view, LEAD)
        return _header_end(view, signature + (-signature % 8))  # the signature header is padded to 8


class File(NamedTuple):
    """A file that the header of an rpm lists."""

    name: str
    mode: int
    size: int
    # `links` counts the files of the header with the device and inode of this one.
    links: int


_FILESIZES = 1028
_FILEMODES = 1030
_FILEDEVICES = 1095
_FILEINODES = 1096
_DIRINDEXES = 1116
_BASENAMES = 1117
_DIRNAMES = 1118
_LONGFILESIZES = 5008
_PAYLOADALIGNMENT = 5125

# The struct format of each integer type of a header tag: int16, int32 and int64.
_INTEGERS = {3: ">H", 4: ">I", 5: ">Q"}
_STRING_ARRAY = 8


def _tags(view: memoryview, off: int) -> tuple[dict[int, list[int]], dict[int, list[str]]]:
    """Return the values of the integer tags and of the string array tags of the header at `off`."""
    nindex, nbytes = struct.unpack(">II", view[off + 8 : off + 16])
    store = off + 16 + 16 * nindex
    if len(view) < store + nbytes:
        util.fail(f"truncated rpm header at {off}")
    integers: dict[int, list[int]] = {}
    strings: dict[int, list[str]] = {}
    for entry in range(off + 16, store, 16):
        tag, kind, start, count = struct.unpack(">IIII", view[entry : entry + 16])
        if kind in _INTEGERS:
            end = start + struct.calcsize(_INTEGERS[kind]) * count
            if end > nbytes:
                util.fail(f"the rpm header tag {tag} ends after the header")
            data = bytes(view[store + start : store + end])
            integers[tag] = [value for (value,) in struct.iter_unpack(_INTEGERS[kind], data)]
        elif kind == _STRING_ARRAY:
            # Each string ends with a NUL. split() returns one more element than the store has NULs.
            values = bytes(view[store + start : store + nbytes]).split(b"\0", count)
            if len(values) <= count:
                util.fail(f"the rpm header tag {tag} ends after the header")
            strings[tag] = [value.decode() for value in values[:count]]
    return integers, strings


def files(rpm_path: Path) -> tuple[list[File], int]:
    """Return the files that the header of the rpm at `rpm_path` lists, and the alignment of their data."""
    with rpm_path.open("rb") as rpm:
        # mmap can't map an empty file.
        if not os.fstat(rpm.fileno()).st_size:
            util.fail(f"{rpm_path} is empty")
        with mmap.mmap(rpm.fileno(), 0, access=mmap.ACCESS_READ) as data, memoryview(data) as view:
            signature = _header_end(view, LEAD)
            header = signature + (-signature % 8)
            _header_end(view, header)
            try:
                integers, strings = _tags(view, header)
            except UnicodeDecodeError as error:
                util.fail(f"{rpm_path}: the rpm header contains a name that isn't UTF-8: {error}")
    basenames = strings.get(_BASENAMES, [])
    dirnames = strings.get(_DIRNAMES, [])
    indexes = integers.get(_DIRINDEXES, [])
    modes = integers.get(_FILEMODES, [])
    # rpm lists the sizes in LONGFILESIZES instead of FILESIZES when the rpm uses format 6, or when a file
    # has 4 GiB or more.
    sizes = integers.get(_LONGFILESIZES) or integers.get(_FILESIZES, [])
    inodes = list(zip(integers.get(_FILEDEVICES, []), integers.get(_FILEINODES, []), strict=False))
    links = Counter(inodes)
    if any(len(values) != len(basenames) for values in (indexes, modes, sizes, inodes)):
        util.fail(f"{rpm_path}: the file tags of the rpm header have different lengths")
    if any(index >= len(dirnames) for index in indexes):
        util.fail(f"{rpm_path}: the rpm header names a directory that it doesn't list")
    listed = [
        File(name=dirnames[index] + basename, mode=mode, size=size, links=links[inode])
        for basename, index, mode, size, inode in zip(basenames, indexes, modes, sizes, inodes, strict=True)
    ]
    return listed, integers.get(_PAYLOADALIGNMENT, [0])[0]


def write_payload(rpm_path: Path, output: BinaryIO) -> None:
    """Write the uncompressed payload of the rpm at `rpm_path` into `output`."""
    with rpm_path.open("rb") as rpm:
        # mmap can't map an empty file.
        if not os.fstat(rpm.fileno()).st_size:
            util.fail(f"{rpm_path} is empty")
        with mmap.mmap(rpm.fileno(), 0, access=mmap.ACCESS_READ) as data:
            offset = payload_offset(data)
        rpm.seek(offset)
        decompress_stream(rpm, output)


def decompress_stream(source: BinaryIO, output: BinaryIO) -> None:
    """Stream one payload from `source`'s current offset into an uncompressed cpio."""
    start = source.tell()
    magic = source.read(util.MAGIC)
    source.seek(start)
    open_compressed = util.decompressor(magic)
    if open_compressed is None:
        if not magic.startswith(CPIO_MAGIC):
            util.fail(f"unknown payload compressor (magic {magic.hex()})")
        shutil.copyfileobj(source, output, length=1024 * 1024)
        return
    # A payload that ends early raises EOFError. Corrupt data raises OSError from bz2 and from a gzip header,
    # zlib.error from the rest of a gzip stream, and an exception type of its own from xz and zstd. Only the
    # reads are inside the try, so a failed write to `output`, such as on a full disk, keeps its traceback.
    with open_compressed(source) as reader:
        while True:
            try:
                chunk = reader.read(1024 * 1024)
            except (EOFError, OSError, lzma.LZMAError, zlib.error, *_zstd_errors()) as error:
                util.fail(f"cannot decompress the payload: {error}")
            if not chunk:
                break
            output.write(chunk)


def _zstd_errors() -> tuple[type[Exception], ...]:
    """Return the exception type of corrupt zstd data, if the interpreter can read zstd."""
    try:
        from compression.zstd import ZstdError
    except ImportError:
        return ()
    return (ZstdError,)
