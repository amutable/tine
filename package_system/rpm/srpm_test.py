# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the unpacking of a source rpm.

buck test tine//package_system/rpm:srpm-test
"""

import stat
import tempfile
import unittest
from pathlib import Path
from typing import override

import cpio
import rpmfile
import srpm


def _newc_header(name: bytes, mode: int, nlink: int, size: int) -> bytes:
    """Return a newc header for `name`, with the padding after the name."""
    named = name + b"\0"
    fields = (1, mode, 0, 0, nlink, 0, size, 0, 0, 0, 0, len(named), 0)
    header = b"070701" + b"".join(b"%08X" % field for field in fields) + named
    return header + bytes(-len(header) % 4)


def _stripped(contents: list[bytes], align: int = 0) -> bytes:
    """Return a payload with stripped cpio headers that contains `contents` in the order of the header."""
    payload = b""
    for index, content in enumerate(contents):
        payload += b"07070X" + b"%08x" % index
        data_align = align if align and len(content) >= align else 4
        payload += bytes(-len(payload) % data_align) + content
        payload += bytes(-len(payload) % 4)
    return payload + _newc_header(b"TRAILER!!!", 0, 1, 0)


class UnpackPayload(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpm-srpm-test.", dir="/var/tmp"))
        )
        self.out = self.scratch / "out"
        self.out.mkdir()
        self.payload = self.scratch / "payload.cpio"
        self.file = self.scratch / "file"
        self.file.write_text("content\n")

    def unpack(self, files: list[rpmfile.File] | None = None, align: int = 0) -> list[str]:
        with self.payload.open("rb") as payload:
            return srpm.unpack_payload(payload.fileno(), self.out, "hello.src.rpm", files or [], align)

    def test_regular_files_are_unpacked(self) -> None:
        # A newc payload contains a spec and an archive, one with a leading "./". unpack_payload()
        # writes both under their plain names.
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_file("hello.spec", self.file, mode=0o644, mtime=0)
            writer.add_file("./hello-1.tar.gz", self.file, mode=0o644, mtime=0)
        self.assertEqual(self.unpack(), ["hello.spec", "hello-1.tar.gz"])
        self.assertEqual((self.out / "hello-1.tar.gz").read_text(), "content\n")

    def test_a_symlink_fails_and_writes_nothing_through_it(self) -> None:
        # A payload that contains a symlink and then a file with the same name could write through the
        # symlink. unpack_payload() must fail on the symlink, and leave its target and `out` unchanged.
        victim = self.scratch / "victim"
        victim.write_text("unchanged\n")
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_symlink("hello.spec", str(victim), mtime=0)
            writer.add_file("hello.spec", self.file, mode=0o644, mtime=0)
        with self.assertRaisesRegex(SystemExit, "hello.spec, which is not a regular file"):
            self.unpack()
        self.assertEqual(victim.read_text(), "unchanged\n")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_executable_bits_are_preserved_without_special_bits(self) -> None:
        # unpack_payload() keeps the executable bits of a file, but drops setuid, setgid and sticky, and
        # makes every file readable.
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_file("helper", self.file, mode=0o7755, mtime=0)
            writer.add_file("owner-only", self.file, mode=0o700, mtime=0)
        self.unpack()
        self.assertEqual(stat.S_IMODE((self.out / "helper").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.out / "owner-only").stat().st_mode), 0o744)

    def test_a_path_fails(self) -> None:
        # A name with a directory could place a file outside `out`. unpack_payload() must fail on it
        # and write nothing outside `out`.
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_file("../escape", self.file, mode=0o644, mtime=0)
        with self.assertRaisesRegex(SystemExit, "not a file name"):
            self.unpack()
        self.assertFalse((self.scratch / "escape").exists())

    def test_a_directory_fails(self) -> None:
        # A source rpm contains only files, so a directory in the payload fails.
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_dir("sub", mode=0o755, mtime=0)
        with self.assertRaisesRegex(SystemExit, "sub, which is not a regular file"):
            self.unpack()

    def test_a_file_twice_fails(self) -> None:
        # The second entry with one name would replace the first. unpack_payload() must fail instead.
        with cpio.Writer(self.payload, 0) as writer:
            writer.add_file("hello.spec", self.file, mode=0o644, mtime=0)
            writer.add_file("hello.spec", self.file, mode=0o644, mtime=0)
        with self.assertRaisesRegex(SystemExit, "hello.spec more than once"):
            self.unpack()

    def test_an_entry_without_one_link_fails(self) -> None:
        # newc stores the data of a hard-link set with its last entry only, so an entry with more links
        # can be empty. An entry with no links is invalid. unpack_payload() must fail on both.
        for nlink in (0, 2):
            with self.subTest(nlink=nlink):
                header = _newc_header(b"hello.spec", stat.S_IFREG | 0o644, nlink, 0)
                self.payload.write_bytes(header + _newc_header(b"TRAILER!!!", 0, 1, 0))
                with self.assertRaisesRegex(SystemExit, f"hello.spec with {nlink} links instead of one"):
                    self.unpack()

    def test_an_empty_payload_fails(self) -> None:
        # mmap can't map an empty file. unpack_payload() must fail with a message instead.
        self.payload.write_bytes(b"")
        with self.assertRaisesRegex(SystemExit, "empty payload"):
            self.unpack()

    def test_stripped_files_are_unpacked(self) -> None:
        # A format 6 rpm writes stripped cpio headers, and its header lists the name, mode and size of
        # each file. The second file is as large as the payload alignment, so its data starts at a
        # multiple of the alignment. unpack_payload() writes both files with their contents.
        files = [
            rpmfile.File(name="hello.spec", mode=stat.S_IFREG | 0o644, size=5, links=1),
            rpmfile.File(name="hello.tar.gz", mode=stat.S_IFREG | 0o755, size=16, links=1),
        ]
        self.payload.write_bytes(_stripped([b"spec\n", b"archive-content\n"], align=16))
        self.assertEqual(self.unpack(files, align=16), ["hello.spec", "hello.tar.gz"])
        self.assertEqual((self.out / "hello.spec").read_bytes(), b"spec\n")
        self.assertEqual((self.out / "hello.tar.gz").read_bytes(), b"archive-content\n")
        self.assertEqual(stat.S_IMODE((self.out / "hello.tar.gz").stat().st_mode), 0o755)

    def test_a_stripped_file_that_the_header_does_not_list_fails(self) -> None:
        # A stripped header names a file by its index in the rpm header. An index past the files of the
        # header must fail.
        self.payload.write_bytes(_stripped([b"spec\n"]))
        with self.assertRaisesRegex(SystemExit, "file 0, which the header doesn't list"):
            self.unpack([])

    def test_a_stripped_hard_link_fails(self) -> None:
        # The header lists two files with one inode. The payload stores their data once, so
        # unpack_payload() must fail as it does for a hard link in a newc payload.
        files = [rpmfile.File(name="hello.spec", mode=stat.S_IFREG | 0o644, size=5, links=2)]
        self.payload.write_bytes(_stripped([b"spec\n"]))
        with self.assertRaisesRegex(SystemExit, "hello.spec with 2 links instead of one"):
            self.unpack(files)

    def test_stripped_data_past_the_payload_fails(self) -> None:
        # The header lists a larger file than the payload contains. unpack_payload() must fail instead
        # of reading past the end of the payload.
        files = [rpmfile.File(name="hello.spec", mode=stat.S_IFREG | 0o644, size=1000, links=1)]
        self.payload.write_bytes(_stripped([b"spec\n"]))
        with self.assertRaisesRegex(SystemExit, "ends after the payload"):
            self.unpack(files)


if __name__ == "__main__":
    unittest.main()
