# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for reading the payload of an rpm.

buck test tine//package_system/rpm:rpmfile-test
"""

import bz2
import compression.zstd
import gzip
import io
import lzma
import struct
import tempfile
import unittest
from pathlib import Path
from typing import override

import rpmfile

# An rpm header with no tags and no data.
_EMPTY_HEADER = rpmfile.MAGIC + bytes(4) + struct.pack(">II", 0, 0)


def _header(tags: dict[int, tuple[int, list[int] | list[str]]]) -> bytes:
    """Return an rpm header with the type and the values of each tag in `tags`."""
    index, store = b"", b""
    for tag, (kind, values) in tags.items():
        if kind == 8:
            data = b"".join(str(value).encode() + b"\0" for value in values)
        else:
            data = b"".join(struct.pack({3: ">H", 4: ">I", 5: ">Q"}[kind], value) for value in values)
        index += struct.pack(">IIII", tag, kind, len(store), len(values))
        store += data
    return rpmfile.MAGIC + bytes(4) + struct.pack(">II", len(tags), len(store)) + index + store


class WritePayload(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpm-rpmfile-test.", dir="/var/tmp"))
        )
        self.rpm = self.scratch / "hello.rpm"

    def write_payload(self) -> bytes:
        output = io.BytesIO()
        rpmfile.write_payload(self.rpm, output)
        return output.getvalue()

    def test_the_payload_is_decompressed(self) -> None:
        # An rpm with a lead, two empty headers and a gzip payload. write_payload() writes the
        # uncompressed cpio archive.
        self.rpm.write_bytes(bytes(96) + _EMPTY_HEADER + _EMPTY_HEADER + gzip.compress(b"070701cpio"))
        self.assertEqual(self.write_payload(), b"070701cpio")

    def test_an_empty_file_fails(self) -> None:
        # mmap can't map an empty file. write_payload() must fail with a message instead.
        self.rpm.write_bytes(b"")
        with self.assertRaisesRegex(SystemExit, "is empty"):
            self.write_payload()

    def test_a_truncated_header_fails(self) -> None:
        # The lead is followed by the magic of the signature header, and then the file ends.
        self.rpm.write_bytes(bytes(96) + rpmfile.MAGIC)
        with self.assertRaisesRegex(SystemExit, "truncated rpm header"):
            self.write_payload()

    def test_a_payload_that_ends_early_fails(self) -> None:
        # A gzip payload cut in half fails with a message instead of an EOFError.
        payload = gzip.compress(b"070701" + bytes(1000))
        self.rpm.write_bytes(bytes(96) + _EMPTY_HEADER + _EMPTY_HEADER + payload[: len(payload) // 2])
        with self.assertRaisesRegex(SystemExit, "cannot decompress the payload"):
            self.write_payload()

    def test_a_corrupt_payload_fails(self) -> None:
        # Each compressor raises an exception type of its own on corrupt data. Overwrite part of a
        # payload of each kind with zeros, and check that every one fails with a message.
        for compress in (gzip.compress, bz2.compress, lzma.compress, compression.zstd.compress):
            with self.subTest(compress.__module__):
                payload = bytearray(compress(b"070701" + bytes(100000)))
                payload[20:60] = bytes(40)
                self.rpm.write_bytes(bytes(96) + _EMPTY_HEADER + _EMPTY_HEADER + payload)
                with self.assertRaisesRegex(SystemExit, "cannot decompress the payload"):
                    self.write_payload()


class Files(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpm-rpmfile-test.", dir="/var/tmp"))
        )
        self.rpm = self.scratch / "hello.src.rpm"

    def files(self, tags: dict[int, tuple[int, list[int] | list[str]]]) -> tuple[list[rpmfile.File], int]:
        main = _header(tags)
        self.rpm.write_bytes(bytes(96) + _EMPTY_HEADER + main)
        return rpmfile.files(self.rpm)

    def test_the_files_of_a_format_6_header_are_listed(self) -> None:
        # A format 6 rpm lists its file sizes in LONGFILESIZES and its payload alignment in
        # PAYLOADALIGNMENT. Two of the three files share an inode. files() lists each file with its
        # directory, mode, size and number of links.
        listed, align = self.files(
            {
                1117: (8, ["hello.spec", "hello.tar.gz", "copy"]),
                1118: (8, ["", "sub/"]),
                1116: (4, [0, 1, 1]),
                1030: (3, [0o100644, 0o100755, 0o100755]),
                5008: (5, [5, 2**33, 2**33]),
                1095: (4, [1, 1, 1]),
                1096: (4, [10, 11, 11]),
                5125: (4, [4096]),
            }
        )
        self.assertEqual(
            listed,
            [
                rpmfile.File(name="hello.spec", mode=0o100644, size=5, links=1),
                rpmfile.File(name="sub/hello.tar.gz", mode=0o100755, size=2**33, links=2),
                rpmfile.File(name="sub/copy", mode=0o100755, size=2**33, links=2),
            ],
        )
        self.assertEqual(align, 4096)

    def test_file_tags_of_different_lengths_fail(self) -> None:
        # The header lists two names but one mode. files() must fail instead of pairing them wrongly.
        with self.assertRaisesRegex(SystemExit, "different lengths"):
            self.files(
                {
                    1117: (8, ["hello.spec", "other"]),
                    1118: (8, [""]),
                    1116: (4, [0, 0]),
                    1030: (3, [0o100644]),
                    1028: (4, [5, 5]),
                    1095: (4, [1, 1]),
                    1096: (4, [10, 11]),
                }
            )

    def test_a_string_array_past_the_header_fails(self) -> None:
        # The last name of the header has no NUL, so the string array runs past the end of the store.
        main = bytearray(_header({1117: (8, ["hello.spec"])}))
        main[-1:] = b"x"
        self.rpm.write_bytes(bytes(96) + _EMPTY_HEADER + main)
        with self.assertRaisesRegex(SystemExit, "ends after the header"):
            rpmfile.files(self.rpm)


if __name__ == "__main__":
    unittest.main()
