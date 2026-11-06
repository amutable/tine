# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for picking the blocks of an AMI disk to upload, and for the AMI's properties.

buck test tine//image:test
"""

import argparse
import base64
import compression.zstd
import datetime
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import override

import ami_import
from ami_import import BLOCK_SIZE


class DataBlocksTest(unittest.TestCase):
    def disk(self, size: int, writes: dict[int, bytes]) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="ami-import-test.")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "disk.raw"
        with path.open("wb") as f:
            f.truncate(size)
            for offset, data in writes.items():
                f.seek(offset)
                f.write(data)
        return path

    def test_yields_only_blocks_with_data(self) -> None:
        disk = self.disk(
            8 * BLOCK_SIZE,
            {
                # allocated, but zero: nothing to upload
                0: bytes(BLOCK_SIZE),
                # stretches across blocks 2 and 3
                3 * BLOCK_SIZE - 1: b"ab",
                7 * BLOCK_SIZE + 5: b"c",
            },
        )
        self.assertEqual([index for index, _ in ami_import.data_blocks(disk)], [2, 3, 7])

    def test_blocks_are_whole_and_in_place(self) -> None:
        disk = self.disk(4 * BLOCK_SIZE, {BLOCK_SIZE + 3: b"x"})
        [(index, block)] = ami_import.data_blocks(disk)
        self.assertEqual(index, 1)
        self.assertEqual(block, bytes(3) + b"x" + bytes(BLOCK_SIZE - 4))

    def test_pads_a_short_last_block(self) -> None:
        disk = self.disk(BLOCK_SIZE + 10, {BLOCK_SIZE: b"y"})
        [(index, block)] = ami_import.data_blocks(disk)
        self.assertEqual(index, 1)
        self.assertEqual(block, b"y" + bytes(BLOCK_SIZE - 1))

    def test_empty_disk_has_no_blocks(self) -> None:
        self.assertEqual(list(ami_import.data_blocks(self.disk(3 * BLOCK_SIZE, {}))), [])

    def test_size(self) -> None:
        self.assertEqual(ami_import.disk_size(self.disk(BLOCK_SIZE + 10, {3: b"x"})), BLOCK_SIZE + 10)


class CompressedDataBlocksTest(DataBlocksTest):
    """The same disks, zstd-compressed as a release's `.raw.zst`, but still named `.raw`.

    The name must not matter, only the content.
    """

    @override
    def disk(self, size: int, writes: dict[int, bytes]) -> Path:
        disk = super().disk(size, writes)
        # as `zstd` writes it from a file, with the decompressed size in the frame header
        disk.write_bytes(compression.zstd.compress(disk.read_bytes()))
        return disk

    def test_more_than_the_header_says(self) -> None:
        disk = self.disk(BLOCK_SIZE, {0: b"x"})
        with disk.open("ab") as f:
            f.write(compression.zstd.compress(b"y"))
        with self.assertRaisesRegex(SystemExit, "decompresses to 524289 bytes, not the 524288"):
            list(ami_import.data_blocks(disk))

    def test_no_size_in_header(self) -> None:
        disk = self.disk(BLOCK_SIZE, {})
        with compression.zstd.ZstdFile(disk, "w") as f:
            f.write(b"x")
        with self.assertRaisesRegex(SystemExit, "does not record its decompressed size"):
            ami_import.disk_size(disk)


class ChecksumTest(unittest.TestCase):
    def test_is_base64_sha256(self) -> None:
        block = b"z" * BLOCK_SIZE
        self.assertEqual(base64.b64decode(ami_import.checksum(block)), hashlib.sha256(block).digest())


class DeprecationTest(unittest.TestCase):
    def test_time_is_utc(self) -> None:
        now = datetime.datetime(
            2026, 11, 3, 12, 30, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=1))
        )
        self.assertEqual(ami_import.deprecation_time(now, 2), "2026-11-05T11:30:15Z")

    def test_days_within_ec2_limits(self) -> None:
        self.assertEqual(ami_import.deprecate_days("30"), 30)
        self.assertEqual(
            ami_import.deprecate_days(str(ami_import.DEPRECATE_DAYS_MAX)), ami_import.DEPRECATE_DAYS_MAX
        )
        for days in ["0", "-1", str(ami_import.DEPRECATE_DAYS_MAX + 1)]:
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "is not between 1 and"):
                ami_import.deprecate_days(days)


class TagTest(unittest.TestCase):
    def test_key_and_value(self) -> None:
        self.assertEqual(ami_import.tag("retention=pr"), ("retention", "pr"))
        # Only the first = separates; EC2 allows = and commas in values, and empty ones.
        self.assertEqual(ami_import.tag("a=b=c,d"), ("a", "b=c,d"))
        self.assertEqual(ami_import.tag("a="), ("a", ""))

    def test_needs_key_and_separator(self) -> None:
        for value in ["a", "=b", ""]:
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "is not KEY=VALUE"):
                ami_import.tag(value)

    def test_refuses_name(self) -> None:
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "pass --name instead"):
            ami_import.tag("Name=x")


class ReusableImageTest(unittest.TestCase):
    def test_available_image(self) -> None:
        images = [{"ImageId": "ami-1", "State": "available"}]
        self.assertEqual(ami_import.reusable_image(images, "n"), "ami-1")

    def test_refuses_unavailable_image(self) -> None:
        for state in ["pending", "failed", "error"]:
            with self.assertRaisesRegex(SystemExit, f"is {state}, not available"):
                ami_import.reusable_image([{"ImageId": "ami-1", "State": state}], "n")

    def test_refuses_several_images(self) -> None:
        images = [{"ImageId": "ami-1", "State": "available"}, {"ImageId": "ami-2", "State": "available"}]
        with self.assertRaisesRegex(SystemExit, "2 images are named 'n'"):
            ami_import.reusable_image(images, "n")


if __name__ == "__main__":
    unittest.main()
