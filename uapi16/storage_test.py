# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Snapshot storage and reconstruction regressions."""

import os
import shutil
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from typing import override

import storage
import uapi16

# Captures in these tests keep every timestamp, so they clamp to an epoch after all of them.
CAPTURE_EPOCH = 2**31


class TestStorage(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp")))
        self.tree = self.scratch / "source"
        self.tree.mkdir()
        self.store = self.scratch / "store"

    def files(self) -> None:
        for name, mode, time in (("a", 0o640, 111), ("b", 0o755, 1800000000000000222)):
            path = self.tree / name
            path.write_text("identical")
            path.chmod(mode)
            os.utime(path, ns=(time, time))
        os.link(self.tree / "a", self.tree / "linked")

    def test_roundtrip_after_artifact_metadata_is_lost(self) -> None:
        # Copy the store without timestamps or hardlinks. The manifest must restore inode identity,
        # original modes and times, literal marker names, symlinks, empty directories and user xattrs.
        self.files()
        for name in ("back\\slash", ".wh.literal", ".wh..wh..opq", ".esc.literal"):
            (self.tree / name).write_text(name)
        (self.tree / "empty").mkdir()
        (self.tree / "link").symlink_to("/a")
        os.utime(self.tree / "link", ns=(333, 333), follow_symlinks=False)
        os.utime(self.tree / "empty", ns=(444, 444))
        os.setxattr(self.tree / "a", "user.example", b"\x00\xff")
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        copied = self.scratch / "copied"
        shutil.copytree(self.store, copied, copy_function=shutil.copyfile)
        restored = self.scratch / "restored"
        storage.restore(storage.Snapshot([copied]), restored)
        self.assertEqual((restored / "a").stat().st_mtime_ns, 111)
        self.assertEqual(stat.S_IMODE((restored / "a").stat().st_mode), 0o640)
        self.assertEqual((restored / "b").stat().st_mtime_ns, 1800000000000000222)
        self.assertEqual(stat.S_IMODE((restored / "b").stat().st_mode), 0o755)
        self.assertTrue((restored / "a").samefile(restored / "linked"))
        self.assertFalse((restored / "a").samefile(restored / "b"))
        self.assertEqual(os.getxattr(restored / "a", "user.example"), b"\x00\xff")
        self.assertEqual(os.readlink(restored / "link"), "/a")
        self.assertEqual((restored / "link").lstat().st_mtime_ns, 333)
        self.assertEqual((restored / "empty").stat().st_mtime_ns, 444)
        self.assertEqual({p.name for p in restored.iterdir()}, {p.name for p in self.tree.iterdir()})

    def test_capture_clamps_times_to_the_epoch(self) -> None:
        # A package manager writes the time of the install into every file and directory that it
        # changes. Capture must store the epoch for a time after it and keep an earlier time, so two
        # installs at different times write the same manifest. Capture a tree with one file before and
        # one file after the epoch, and check the times in the manifest.
        self.files()
        storage.capture(self.tree, self.store, epoch=1700000000)
        objects = {obj.get("name"): obj for obj in storage.Snapshot([self.store]).objects}
        self.assertEqual(objects["a"]["mTimeNSec"], 111)
        self.assertEqual(objects["b"]["mTimeNSec"], 1700000000 * 10**9)

    def test_overlay_writes_and_metadata_leave_blobs_unchanged(self) -> None:
        # Declared links start in the upper because index=off splits lower links on writes.
        # Equal-content independent files must copy up separately without changing the blob.
        self.files()
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        blob = next((self.store / "blobs").iterdir())
        before = blob.read_bytes(), blob.stat()
        with storage.root([self.store]) as root:
            self.assertTrue((root / "a").samefile(root / "linked"))
            self.assertFalse((root / "a").samefile(root / "b"))
            (root / "a").write_text("linked write")
            (root / "linked").chmod(0o600)
            os.utime(root / "a", ns=(555, 555))
            self.assertEqual((root / "linked").read_text(), "linked write")
            self.assertEqual(stat.S_IMODE((root / "a").stat().st_mode), 0o600)
            self.assertEqual((root / "linked").stat().st_mtime_ns, 555)
            self.assertEqual((root / "b").read_text(), "identical")
            (root / "b").write_text("independent write")
            self.assertEqual((root / "a").read_text(), "linked write")
        self.assertEqual(blob.read_bytes(), before[0])
        self.assertEqual(blob.stat().st_mtime_ns, before[1].st_mtime_ns)
        self.assertEqual(blob.stat().st_mode, before[1].st_mode)
        self.assertEqual(blob.stat().st_uid, before[1].st_uid)

    def test_reuse_and_metadata_only_changes(self) -> None:
        # Changing only metadata must alter the complete manifest while reusing the parent's blob.
        self.files()
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root([self.store]) as root:
            (root / "b").chmod(0o600)
            os.utime(root / "b", ns=(999, 999))
            storage.capture(root, child, [self.store], epoch=CAPTURE_EPOCH)
        self.assertEqual(list((child / "blobs").iterdir()), [])
        self.assertNotEqual(
            (child / uapi16.MANIFEST).read_bytes(), (self.store / uapi16.MANIFEST).read_bytes()
        )
        with storage.root([self.store, child]) as root:
            self.assertEqual(stat.S_IMODE((root / "b").stat().st_mode), 0o600)
            self.assertEqual((root / "b").stat().st_mtime_ns, 999)

    def test_deletions_opacity_and_replacements(self) -> None:
        # Delete inherited paths, replace files and directories, and rename a fresh directory over
        # an inherited directory. Capture the merged view so no native overlay markers persist.
        (self.tree / "dir").mkdir()
        (self.tree / "dir/old").write_text("old")
        (self.tree / "deleted").write_text("deleted")
        (self.tree / "file").write_text("file")
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root([self.store]) as root:
            (root / "deleted").unlink()
            shutil.rmtree(root / "dir")
            (root / "fresh").mkdir()
            (root / "fresh/new").write_text("new")
            (root / "fresh").rename(root / "dir")
            (root / "file").unlink()
            (root / "file").mkdir()
            (root / "file/child").write_text("child")
            storage.capture(root, child, [self.store], epoch=CAPTURE_EPOCH)
        with storage.root([self.store, child]) as root:
            self.assertFalse((root / "deleted").exists())
            self.assertEqual([p.name for p in (root / "dir").iterdir()], ["new"])
            self.assertEqual((root / "file/child").read_text(), "child")
            shutil.rmtree(root / "file")
            (root / "file").write_text("replacement")
            grandchild = self.scratch / "grandchild"
            storage.capture(root, grandchild, [self.store, child], epoch=CAPTURE_EPOCH)
        with storage.root([self.store, child, grandchild]) as root:
            self.assertEqual((root / "file").read_text(), "replacement")
        self.assertEqual({p.name for p in child.iterdir()}, {"blobs", uapi16.MANIFEST})
        self.assertNotIn(b"overlay.opaque", (child / uapi16.MANIFEST).read_bytes())

    def test_child_stores_only_new_contents(self) -> None:
        # A child reuses every inherited content digest and writes one blob for two new equal files.
        self.files()
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root([self.store]) as root:
            (root / "new").write_text("new contents")
            (root / "new-independent").write_text("new contents")
            storage.capture(root, child, [self.store], epoch=CAPTURE_EPOCH)
        self.assertEqual(len(list((child / "blobs").iterdir())), 1)
        with storage.root([self.store, child]) as root:
            self.assertEqual((root / "a").read_text(), "identical")
            self.assertFalse((root / "new").samefile(root / "new-independent"))

    def test_device_nodes(self) -> None:
        # Preserve device numbers when the namespace permits mknod; unprivileged hosts may deny it.
        try:
            os.mknod(self.tree / "device", stat.S_IFCHR | 0o600, os.makedev(1, 3))
        except PermissionError:
            self.skipTest("the sandbox does not permit device nodes")
        storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        with storage.root([self.store]) as root:
            attributes = (root / "device").lstat()
            self.assertTrue(stat.S_ISCHR(attributes.st_mode))
            self.assertEqual(attributes.st_rdev, os.makedev(1, 3))

    def test_special_files(self) -> None:
        # FIFOs and sockets need manifest inode types because Buck only stores regular blobs.
        os.mkfifo(self.tree / "fifo", 0o600)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.tree / "sock"))
            storage.capture(self.tree, self.store, epoch=CAPTURE_EPOCH)
        with storage.root([self.store]) as root:
            self.assertTrue(stat.S_ISFIFO((root / "fifo").lstat().st_mode))
            self.assertTrue(stat.S_ISSOCK((root / "sock").lstat().st_mode))
            self.assertEqual(stat.S_IMODE((root / "fifo").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
