# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the helpers tine's Python entry points share.

buck test tine//:test
"""

import errno
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import util


def scratch(case: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory(prefix="util-test.")
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


class TestAtomicTextWriter(unittest.TestCase):
    def test_keeps_the_mode_of_an_existing_file(self) -> None:
        path = scratch(self) / "generated"
        path.write_text("one\n")
        path.chmod(0o600)
        util.atomic_write_text(path, "two\n")
        self.assertEqual(path.read_text(), "two\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_failed_write_leaves_no_trace(self) -> None:
        root = scratch(self)
        path = root / "generated"
        path.write_text("one\n")
        with self.assertRaises(RuntimeError), util.atomic_text_writer(path) as stream:
            stream.write("two\n")
            raise RuntimeError
        self.assertEqual(path.read_text(), "one\n")
        self.assertEqual([p.name for p in root.iterdir()], ["generated"])


class TestWriteIfChanged(unittest.TestCase):
    def test_leaves_an_unchanged_file_alone(self) -> None:
        path = scratch(self) / "generated"
        util.write_if_changed(path, "one\n")
        before = path.stat().st_mtime_ns
        util.write_if_changed(path, "one\n")
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_replaces_a_changed_file(self) -> None:
        path = scratch(self) / "generated"
        util.write_if_changed(path, "one\n")
        util.write_if_changed(path, "two\n")
        self.assertEqual(path.read_text(), "two\n")

    def test_a_file_reached_through_a_symlink_is_written_through(self) -> None:
        # Buck reads that layout, and renaming over the link would leave the real file stale.
        root = scratch(self)
        shared = root / "shared.bcfg"
        shared.write_text("one\n")
        link = root / "generated"
        link.symlink_to(shared)
        util.write_if_changed(link, "two\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(shared.read_text(), "two\n")

    def test_reports_a_path_it_cannot_write(self) -> None:
        # Root, which the box runs as, writes through a read-only directory; an absent one stops it.
        with self.assertRaisesRegex(SystemExit, "tine: cannot write"):
            util.write_if_changed(scratch(self) / "absent" / "generated", "one\n")


class TestNamedFiles(unittest.TestCase):
    def test_finds_nested_files_without_following_links(self) -> None:
        source = scratch(self) / "source"
        for directory in ("", "nested"):
            (source / directory).mkdir(parents=True, exist_ok=True)
            (source / directory / "wanted").touch()
            (source / directory / "other").touch()
        (source / "cycle").symlink_to(".")
        (source / "dangling").symlink_to("missing")

        self.assertEqual(util.named_files(source, "wanted"), [Path("nested/wanted"), Path("wanted")])


class TestCloneFile(unittest.TestCase):
    def test_a_byte_copy_keeps_the_holes_of_a_sparse_file(self) -> None:
        # Without reflinks, clone_file() copies bytes. Copy a 64 MiB file that only has data at its
        # end. The copy must have the same contents and allocate less than 1 MiB.
        root = scratch(self)
        source, destination = root / "source", root / "destination"
        with source.open("wb") as stream:
            stream.seek(64 << 20)
            stream.write(b"end")
        with mock.patch.object(util.os, "copy_file_range", side_effect=OSError(errno.EXDEV, "no reflinks")):
            util.clone_file(source, destination)
        self.assertEqual(destination.stat().st_size, source.stat().st_size)
        with destination.open("rb") as stream:
            stream.seek(64 << 20)
            self.assertEqual(stream.read(), b"end")
        self.assertLess(destination.stat().st_blocks * 512, 1 << 20)


class TestReflink(unittest.TestCase):
    def test_unsupported_reflinks_raise_reflink_unavailable(self) -> None:
        # storage.root() falls back to an overlay when reflinks are unsupported, so it must tell an
        # unsupported reflink from a successful clone. Each errno of an unsupported or denied clone
        # must raise ReflinkUnavailable with that errno.
        root = scratch(self)
        source, destination = root / "source", root / "destination"
        source.write_bytes(b"cached contents")
        for number in (errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM):
            with (
                self.subTest(errno=number),
                mock.patch.object(util.fcntl, "ioctl", side_effect=OSError(number, "unavailable")),
                self.assertRaises(util.ReflinkUnavailable) as error,
            ):
                util.reflink(source, destination)
            self.assertEqual(error.exception.errno, number)
