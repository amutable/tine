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
import uapi16

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


class TestPackManifest(unittest.TestCase):
    def test_aligns_the_data_of_large_files_to_filesystem_blocks(self) -> None:
        # Regular files of at least 4096 bytes must start their data on a 4096-byte boundary of the archive,
        # whatever the length of the headers before them. Pack files of several sizes with names of several
        # lengths, and check the data offsets and that tarfile reads back every file unchanged.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            sizes = {"a": 4096, "b" * 120: 5000, "c": 100, "d" * 300: 70000, "e": 12288}
            objects: list[uapi16.Object] = [
                {"mediaType": uapi16.MEDIA_TYPE, "type": "dir", "mode": 0o755, "mTimeNSec": 0}
            ]
            blobs = {name: root / f"{index}.blob" for index, name in enumerate(sizes)}
            for name, size in sizes.items():
                blobs[name].write_bytes(os.urandom(size))
                objects.append({"name": name, "type": "reg", "mode": 0o644, "mTimeNSec": 0, "size": size})
            archive = root / "image.tar"
            tar.pack_manifest(objects, lambda obj: blobs[obj["name"]], archive, align=True)

            with tarfile.open(archive) as packed:
                for member in packed.getmembers():
                    name = member.name.removeprefix("./")
                    if name not in sizes:
                        continue
                    if sizes[name] >= 4096:
                        self.assertEqual(member.offset_data % 4096, 0, name)
                    extracted = packed.extractfile(member)
                    assert extracted is not None
                    self.assertEqual(extracted.read(), blobs[name].read_bytes(), name)

    def test_leaves_the_data_unaligned_without_align(self) -> None:
        # Without align, the writer adds no pax comment record to pad the headers of a large file.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            blob = root / "blob"
            blob.write_bytes(os.urandom(5000))
            objects: list[uapi16.Object] = [
                {"name": "large", "type": "reg", "mode": 0o644, "mTimeNSec": 0, "size": 5000}
            ]
            archive = root / "image.tar"
            tar.pack_manifest(objects, lambda obj: blob, archive, align=False)
            with tarfile.open(archive) as packed:
                (member,) = packed.getmembers()
            self.assertNotIn("comment", member.pax_headers)
