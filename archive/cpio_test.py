# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the newc reader.

buck test tine//archive:test
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

import cpio


def entry(name: str, mode: int, size: int, *, ino: int = 0, nlink: int = 1) -> bytes:
    """One newc header, which `Writer` cannot emit with a chosen inode or link count."""
    named = name.encode() + b"\0"
    fields = (ino, mode, 0, 0, nlink, 0, size, 0, 0, 0, 0, len(named), 0)
    header = b"070701" + b"".join(b"%08X" % field for field in fields) + named
    return header + b"\0" * (-len(header) % 4)


def payload(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


class TestUnpack(unittest.TestCase):
    def test_extracts_an_ordinary_hardlink_set(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # The newc convention rpms use: empty stubs first, content on the last of the set.
            archive = root / "links.cpio"
            archive.write_bytes(
                entry("a", stat.S_IFREG | 0o644, 0, ino=11, nlink=3)
                + entry("b", stat.S_IFREG | 0o644, 0, ino=11, nlink=3)
                + entry("c", stat.S_IFREG | 0o644, 8, ino=11, nlink=3)
                + payload(b"content\n")
                + entry("TRAILER!!!", 0, 0)
            )
            dest = root / "dest"
            with open(archive, "rb") as raw:
                cpio.unpack(raw.fileno(), dest)

            inodes = {os.stat(dest / name).st_ino for name in ("a", "b", "c")}
            self.assertEqual(len(inodes), 1)
            self.assertEqual(os.stat(dest / "a").st_nlink, 3)
            self.assertEqual((dest / "a").read_text(), "content\n")

    def test_extracts_a_hardlink_set_with_no_content_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            archive = root / "empty.cpio"
            archive.write_bytes(
                entry("a", stat.S_IFREG | 0o600, 0, ino=12, nlink=2)
                + entry("b", stat.S_IFREG | 0o600, 0, ino=12, nlink=2)
                + entry("TRAILER!!!", 0, 0)
            )
            dest = root / "dest"
            with open(archive, "rb") as raw:
                cpio.unpack(raw.fileno(), dest)

            for name in ("a", "b"):
                self.assertEqual((dest / name).read_bytes(), b"")
                self.assertEqual((dest / name).stat().st_mode & 0o7777, 0o600)

    def test_reports_a_malformed_archive_as_the_input_error_it_is(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # No trailer, so the reader runs off the end of the archive.
            archive = root / "truncated.cpio"
            archive.write_bytes(entry("demo", stat.S_IFREG | 0o644, 0))

            with open(archive, "rb") as raw, self.assertRaises(SystemExit) as raised:
                cpio.unpack(raw.fileno(), root / "dest")
            self.assertIn("magic", str(raised.exception))

    def test_a_negative_size_is_a_malformed_archive(self) -> None:
        # The data of "demo" starts at offset 116. int() parses the filesize "-0000074" as -116, which
        # would point the next header back at this one.
        before = b"".join(b"%08X" % field for field in (0, stat.S_IFREG | 0o644, 0, 0, 1, 0))
        after = b"".join(b"%08X" % field for field in (0, 0, 0, 0, 5, 0))
        header = b"070701" + before + b"-0000074" + after + b"demo\0"
        with self.assertRaisesRegex(ValueError, "bad cpio header at 0: .*-0000074"):
            list(cpio.read(header))

    def test_reports_what_actually_failed_when_an_entry_cannot_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # `a` as a file and then as a directory: writing the second raises from inside the loop.
            # An entry holding a view into the archive would make the mmap's close raise a
            # BufferError over this, which is the whole reason nothing yielded holds one.
            archive = root / "conflict.cpio"
            archive.write_bytes(
                entry("a", stat.S_IFREG | 0o644, 8)
                + payload(b"content\n")
                + entry("a/b", stat.S_IFREG | 0o644, 0)
                + entry("TRAILER!!!", 0, 0)
            )
            with open(archive, "rb") as raw, self.assertRaises(FileExistsError):
                cpio.unpack(raw.fileno(), root / "dest")

    def test_unpacks_an_ordinary_archive(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "payload").write_text("content\n")
            archive = root / "plain.cpio"
            with cpio.Writer(archive, 0) as writer:
                writer.add_dir("usr", mode=0o755, mtime=0)
                writer.add_file("usr/demo", root / "payload", mode=0o755, mtime=0)

            dest = root / "dest"
            with open(archive, "rb") as raw:
                self.assertEqual(cpio.unpack(raw.fileno(), dest), 2)
            self.assertEqual((dest / "usr/demo").read_text(), "content\n")
            self.assertEqual((dest / "usr/demo").stat().st_mode & 0o7777, 0o755)
            self.assertTrue(os.path.isdir(dest / "usr"))

    def test_an_unaligned_archive_keeps_the_stock_name_field(self) -> None:
        """The kernel's early cpio reader refuses a padded name, so nothing pads the microcode's."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "payload").write_bytes(bytes(cpio._BLOCK))
            name = "kernel/x86/microcode/GenuineIntel.bin"
            for block_align, namesize in ((True, cpio._BLOCK - cpio._HEADER), (False, len(name) + 1)):
                with self.subTest(block_align=block_align):
                    archive = root / "ucode.cpio"
                    with cpio.Writer(archive, 0, block_align=block_align) as writer:
                        writer.add_file(name, root / "payload", mode=0o644, mtime=0)
                    header = archive.read_bytes()[: cpio._HEADER]
                    self.assertEqual(int(header[6 + 11 * 8 : 6 + 12 * 8], 16), namesize)


if __name__ == "__main__":
    unittest.main()
