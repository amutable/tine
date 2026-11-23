# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the deterministic tar writer.

buck test tine//archive:test
"""

import os
import tarfile
import tempfile
import unittest
from pathlib import Path

import tar

EPOCH = 1000000000


def tree(root: Path) -> Path:
    """A tree with the properties an archive has to normalize away."""
    source = root / "tree"
    (source / "dir").mkdir(parents=True)
    (source / "dir/file").write_text("content\n")
    (source / "link").symlink_to("dir/file")
    os.utime(source / "dir/file", (EPOCH + 3600, EPOCH + 3600))
    return source


class TestPackTree(unittest.TestCase):
    def test_packs_the_same_bytes_twice(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = tree(root)
            first, second = root / "first.tar", root / "second.tar"
            tar.pack_tree(source, first, EPOCH)
            # A second pass of the same tree, as a rebuild would do it.
            tar.pack_tree(source, second, EPOCH)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_normalizes_ownership_and_clamps_mtimes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            archive = root / "tree.tar"
            tar.pack_tree(tree(root), archive, EPOCH)
            with tarfile.open(archive) as packed:
                members = {member.name: member for member in packed.getmembers()}
            self.assertEqual(sorted(members), ["./dir", "./dir/file", "./link"])
            for member in members.values():
                self.assertEqual((member.uid, member.gid), (0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
                # The file was stamped an hour past the epoch; nothing may outlive it.
                self.assertLessEqual(member.mtime, EPOCH)
            self.assertEqual(members["./link"].linkname, "dir/file")
