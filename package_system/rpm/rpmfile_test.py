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


if __name__ == "__main__":
    unittest.main()
