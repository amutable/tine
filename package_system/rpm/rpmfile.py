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
from collections.abc import Buffer
from pathlib import Path
from typing import BinaryIO

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
