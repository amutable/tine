#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Unpack the spec and the Source and Patch files of a source rpm into a directory.

A source rpm comes from outside the project. The driver writes only regular files with plain names, and
fails on any other entry of the payload. A symlink or a path in the payload therefore can't place a file
outside the directory.
"""

import mmap
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, TypedDict

import specs
import util

import cpio
import rpmfile


class Spec(TypedDict):
    srpm: str
    out: str


class _Entry(NamedTuple):
    name: str
    mode: int
    nlink: int
    data_off: int
    size: int


# rpm writes a payload with stripped cpio headers when the header lists the file sizes in LONGFILESIZES. A
# stripped header contains the index of the file in the header and no name, mode or size.
_STRIPPED_MAGIC = b"07070X"
_STRIPPED_HEADER = len(_STRIPPED_MAGIC) + 8


def _roundup(value: int, align: int) -> int:
    return value + (-value % align)


def _newc_entries(archive: mmap.mmap, what: str) -> list[_Entry]:
    try:
        entries = list(cpio.read(archive))
    except ValueError as error:
        util.fail(f"srpm: {what}: {error}")
    return [_Entry(entry.name, entry.mode, entry.nlink, entry.data_off, entry.size) for entry in entries]


def _stripped_entries(archive: mmap.mmap, files: list[rpmfile.File], align: int, what: str) -> list[_Entry]:
    entries: list[_Entry] = []
    off = 0
    while archive[off : off + len(_STRIPPED_MAGIC)] == _STRIPPED_MAGIC:
        index = archive[off + len(_STRIPPED_MAGIC) : off + _STRIPPED_HEADER]
        if len(index) != 8 or index.strip(b"0123456789abcdefABCDEF"):
            util.fail(f"srpm: {what}: bad stripped cpio header at {off}: {index!r}")
        fx = int(index, 16)
        if fx >= len(files):
            util.fail(f"srpm: {what}: the payload contains file {fx}, which the header doesn't list")
        file = files[fx]
        # rpm aligns the data of a regular file to the payload alignment of the header if the file is at
        # least that large, and to 4 bytes otherwise.
        data_align = align if align and stat.S_ISREG(file.mode) and file.size >= align else 4
        data_off = _roundup(off + _STRIPPED_HEADER, data_align)
        if data_off + file.size > len(archive):
            util.fail(f"srpm: {what}: the data of {file.name} ends after the payload")
        entries.append(_Entry(file.name, file.mode, file.links, data_off, file.size))
        off = _roundup(data_off + file.size, 4)
    # The trailer of a stripped payload is a newc trailer.
    try:
        rest = list(cpio.read(archive, off))
    except ValueError as error:
        util.fail(f"srpm: {what}: {error}")
    if rest:
        util.fail(f"srpm: {what}: the payload mixes stripped and newc cpio headers")
    return entries


def unpack_payload(payload: int, out: Path, what: str, files: list[rpmfile.File], align: int) -> list[str]:
    """Write the files of the cpio archive in the fd `payload` into `out`, and return their names.

    `files` and `align` are the files and the payload alignment that the header of the rpm lists. `what`
    names the source rpm in a failure message.
    """
    # mmap can't map an empty file.
    if not os.fstat(payload).st_size:
        util.fail(f"srpm: {what} has an empty payload")
    names: list[str] = []
    with mmap.mmap(payload, 0, access=mmap.ACCESS_READ) as archive:
        if archive[: len(_STRIPPED_MAGIC)] == _STRIPPED_MAGIC:
            entries = _stripped_entries(archive, files, align, what)
        else:
            entries = _newc_entries(archive, what)
        for entry in entries:
            name = entry.name.removeprefix("./")
            if not name or "/" in name or name in (".", ".."):
                util.fail(f"srpm: {what} contains {entry.name!r}, which is not a file name")
            if not stat.S_ISREG(entry.mode):
                util.fail(f"srpm: {what} contains {name}, which is not a regular file")
            # The payload stores the data of a hard-link set with one of its files only.
            if entry.nlink != 1:
                util.fail(f"srpm: {what} contains {name} with {entry.nlink} links instead of one")
            try:
                fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                util.fail(f"srpm: {what} contains {name} more than once")
            with os.fdopen(fd, "wb") as output:
                util.copy_range(output.fileno(), 0, payload, entry.data_off, entry.size)
                os.fchmod(output.fileno(), 0o644 | (entry.mode & 0o111))
            names.append(name)
    return names


def unpack(srpm: Path, out: Path) -> list[str]:
    """Write the files of the source rpm `srpm` into `out`, and return their names."""
    out.mkdir(parents=True, exist_ok=True)
    files, align = rpmfile.files(srpm)
    with tempfile.TemporaryFile() as payload:
        rpmfile.write_payload(srpm, payload)
        payload.flush()
        return unpack_payload(payload.fileno(), out, str(srpm), files, align)


def main(argv: list[str] | None = None) -> None:
    """Parse an invocation and unpack its source rpm."""
    spec = specs.parse(Spec, "srpm", argv)
    names = unpack(Path(spec["srpm"]), Path(spec["out"]))
    print(f"srpm: unpacked {len(names)} files of {spec['srpm']}", file=sys.stderr)


if __name__ == "__main__":
    main()
