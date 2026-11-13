# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Snapshot storage and reconstruction regressions."""

import errno
import os
import shutil
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

from util import ReflinkUnavailable, clone_file

import storage
import uapi16
from isolation import Overlay

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
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        copied = self.scratch / "copied"
        shutil.copytree(self.store, copied, copy_function=shutil.copyfile)
        materialized = self.scratch / "restored"
        storage.restore(storage.Snapshot([copied]), materialized)
        with (
            storage.root(storage.Snapshot([copied]), readonly=True) as readonly,
            storage.root(storage.Snapshot([copied])) as restored,
        ):
            for tree in (materialized, readonly.path, restored.path):
                with self.subTest(tree=tree):
                    self.assertEqual((tree / "a").stat().st_mtime_ns, 111)
                    self.assertEqual(stat.S_IMODE((tree / "a").stat().st_mode), 0o640)
                    self.assertEqual((tree / "b").stat().st_mtime_ns, 1800000000000000222)
                    self.assertEqual(stat.S_IMODE((tree / "b").stat().st_mode), 0o755)
                    self.assertTrue((tree / "a").samefile(tree / "linked"))
                    self.assertFalse((tree / "a").samefile(tree / "b"))
                    self.assertEqual(os.getxattr(tree / "a", "user.example"), b"\x00\xff")
                    self.assertEqual(os.readlink(tree / "link"), "/a")
                    self.assertEqual((tree / "link").lstat().st_mtime_ns, 333)
                    self.assertEqual((tree / "empty").stat().st_mtime_ns, 444)
                    self.assertEqual({p.name for p in tree.iterdir()}, {p.name for p in self.tree.iterdir()})
            child = self.scratch / "child"
            storage.capture(restored, child, [copied], epoch=CAPTURE_EPOCH)
        self.assertEqual((child / uapi16.MANIFEST).read_bytes(), (copied / uapi16.MANIFEST).read_bytes())

    def test_capture_clamps_times_to_the_epoch(self) -> None:
        # A package manager writes the time of the install into every file and directory that it
        # changes. Capture must store the epoch for a time after it and keep an earlier time, so two
        # installs at different times write the same manifest. Capture a tree with one file before and
        # one file after the epoch, and check the times in the manifest.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=1700000000)
        objects = {obj.get("name"): obj for obj in storage.Snapshot([self.store]).objects}
        self.assertEqual(objects["a"]["mTimeNSec"], 111)
        self.assertEqual(objects["b"]["mTimeNSec"], 1700000000 * 10**9)

    def test_writes_and_metadata_leave_blobs_unchanged(self) -> None:
        # Writes and metadata changes in a restored root must preserve declared hardlinks, keep
        # equal-content independent files separate and leave the cached blob unchanged.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        blob = next((self.store / "blobs").iterdir())
        before = blob.read_bytes(), blob.stat()
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            self.assertTrue((root / "a").samefile(root / "linked"))
            self.assertFalse((root / "a").samefile(root / "b"))
            self.assertEqual((root / "a").stat().st_mtime_ns, 111)
            self.assertEqual((root / "b").stat().st_mtime_ns, 1800000000000000222)
            (root / "b").chmod(0o604)
            os.chown(root / "b", 0, 0, follow_symlinks=False)
            os.utime(root / "b", ns=(444, 444))
            self.assertEqual((root / "a").stat().st_mtime_ns, 111)
            self.assertEqual(stat.S_IMODE((root / "a").stat().st_mode), 0o640)
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

    def test_readonly_reconstruction_never_copies_blob_data(self) -> None:
        # Sparse lower metadata files restore independent files and declared hardlinks without
        # copying any data. Reads preserve metadata, while writes and chmod fail with EROFS.
        self.files()
        (self.tree / "nested").mkdir()
        (self.tree / "independent").write_text("identical")
        (self.tree / "empty").touch()
        os.link(self.tree / "a", self.tree / "nested/linked")
        os.utime(self.tree / "nested", ns=(999, 999))
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        with mock.patch.object(storage, "clone_file", wraps=storage.clone_file) as copy:
            with storage.root(storage.Snapshot([self.store]), readonly=True, strategy="overlay") as restored:
                root = restored.path
                self.assertEqual(copy.call_count, 0)
                self.assertTrue((root / "a").samefile(root / "nested/linked"))
                self.assertFalse((root / "b").samefile(root / "independent"))
                self.assertEqual((root / "independent").read_text(), "identical")
                self.assertEqual((root / "empty").read_bytes(), b"")
                self.assertEqual((root / "nested").stat().st_mtime_ns, 999)
                self.assertEqual(copy.call_count, 0)
                for operation in (
                    lambda: (root / "a").write_text("changed"),
                    lambda: (root / "independent").chmod(0o600),
                ):
                    with self.assertRaises(OSError) as error:
                        operation()
                    self.assertEqual(error.exception.errno, errno.EROFS)

    def test_commands_can_create_new_hardlinks(self) -> None:
        # A command creates a hardlink to an inherited independent file. Writes must update the
        # alias, and capturing the result must record a hardlink group without changing its blob.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root(storage.Snapshot([self.store]), strategy="overlay") as restored:
            root = restored.path
            os.link(root / "b", root / "new-link")
            (root / "b").write_text("new group")
            (root / "new-link").chmod(0o600)
            os.utime(root / "new-link", ns=(777, 777))
            self.assertEqual((root / "new-link").read_text(), "new group")
            self.assertEqual(stat.S_IMODE((root / "b").stat().st_mode), 0o600)
            entries = {entry.name: entry for entry in restored.entries()}
            self.assertEqual(entries["b"].inode, entries["new-link"].inode)
            storage.capture(restored, child, [self.store], epoch=CAPTURE_EPOCH)
        with storage.root(storage.Snapshot([self.store, child])) as restored:
            root = restored.path
            self.assertTrue((root / "b").samefile(root / "new-link"))
            self.assertEqual((root / "new-link").read_text(), "new group")
            self.assertEqual((root / "b").stat().st_mtime_ns, 777)
            self.assertEqual((root / "a").read_text(), "identical")

    def test_extension_compares_hardlink_membership(self) -> None:
        # An earlier new group changes token numbers without changing inherited files. Extension
        # selection must omit those files and include every member when a group gains an alias.
        (self.tree / "z").write_text("inherited")
        os.link(self.tree / "z", self.tree / "z-linked")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        (self.tree / "a").write_text("new group")
        os.link(self.tree / "a", self.tree / "a-linked")
        child = self.scratch / "child"
        storage.capture(storage.Root(self.tree), child, [self.store], epoch=CAPTURE_EPOCH)
        snapshot = storage.Snapshot([self.store, child])
        storage.delta(snapshot, 1)
        with storage.root(snapshot, strategy="copy") as restored:
            self.assertEqual({p.name for p in restored.path.iterdir()}, {"a", "a-linked"})
            self.assertTrue((restored.path / "a").samefile(restored.path / "a-linked"))

        # A new alias changes group membership even when the original members keep the same token.
        (self.tree / "a").unlink()
        (self.tree / "a-linked").unlink()
        os.link(self.tree / "z", self.tree / "new-alias")
        alias = self.scratch / "alias"
        storage.capture(storage.Root(self.tree), alias, [self.store], epoch=CAPTURE_EPOCH)
        snapshot = storage.Snapshot([self.store, alias])
        storage.delta(snapshot, 1)
        with storage.root(snapshot, strategy="copy") as restored:
            self.assertEqual({p.name for p in restored.path.iterdir()}, {"z", "z-linked", "new-alias"})
            self.assertTrue((restored.path / "z").samefile(restored.path / "new-alias"))
            self.assertTrue((restored.path / "z-linked").samefile(restored.path / "new-alias"))

    def test_writable_reconstruction_copies_only_declared_groups(self) -> None:
        # A writable overlay needs private data only for declared hardlink groups. Independent
        # files must read from blobs until chmod or a write copies the affected path into the upper.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        with mock.patch.object(storage, "clone_file", wraps=storage.clone_file) as copy:
            with storage.root(storage.Snapshot([self.store]), strategy="overlay") as restored:
                root = restored.path
                assert restored.upper is not None
                self.assertEqual(copy.call_count, 1)
                self.assertTrue((restored.upper / "a").samefile(restored.upper / "linked"))
                self.assertFalse((restored.upper / "b").exists())
                self.assertEqual((root / "b").read_text(), "identical")
                self.assertFalse((restored.upper / "b").exists())
                (root / "b").chmod(0o600)
                self.assertTrue((restored.upper / "b").exists())
                self.assertEqual(copy.call_count, 1)

    def test_available_reflinks_use_private_inodes_without_overlay(self) -> None:
        # Simulate successful reflinks with private files on the test filesystem. Automatic
        # reconstruction must preserve inode independence, hardlinks and metadata without OverlayFS.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        blob = next((self.store / "blobs").iterdir())
        before = blob.read_bytes(), blob.stat()

        for readonly in (False, True):
            with (
                self.subTest(readonly=readonly),
                mock.patch.object(storage, "reflink", side_effect=clone_file),
                mock.patch.object(storage, "Overlay", side_effect=AssertionError("unexpected overlay")),
                storage.root(storage.Snapshot([self.store]), readonly=readonly) as restored,
            ):
                root = restored.path
                self.assertIsNone(restored.upper)
                self.assertTrue((root / "a").samefile(root / "linked"))
                self.assertFalse((root / "a").samefile(root / "b"))
                self.assertFalse((root / "a").samefile(blob))
                self.assertEqual((root / "a").stat().st_mtime_ns, 111)
                self.assertEqual(stat.S_IMODE((root / "b").stat().st_mode), 0o755)
                if readonly:
                    with self.assertRaises(OSError) as error:
                        (root / "a").write_text("changed")
                    self.assertEqual(error.exception.errno, errno.EROFS)
                else:
                    os.link(root / "b", root / "new-link")
                    self.assertTrue((root / "b").samefile(root / "new-link"))
                    (root / "a").chmod(0o600)
                    (root / "linked").write_text("changed")
                    self.assertEqual((root / "a").read_text(), "changed")
                    self.assertEqual((root / "b").read_text(), "identical")
        self.assertEqual(blob.read_bytes(), before[0])
        self.assertEqual(blob.stat().st_mode, before[1].st_mode)
        self.assertEqual(blob.stat().st_mtime_ns, before[1].st_mtime_ns)

    def test_unavailable_reflinks_fall_back_to_overlay(self) -> None:
        # Automatic reconstruction reflinks one blob to test the filesystem. When that fails, it
        # must reconstruct an overlay with the original contents and metadata.
        self.files()
        (self.tree / "b").write_text("different contents")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)

        with mock.patch.object(
            storage, "reflink", side_effect=ReflinkUnavailable(errno.EXDEV, "different filesystems")
        ) as reflink:
            with storage.root(storage.Snapshot([self.store])) as restored:
                root = restored.path
                self.assertIsNotNone(restored.upper)
                self.assertEqual(reflink.call_count, 1)
                self.assertEqual((root / "a").read_text(), "identical")
                self.assertEqual((root / "b").read_text(), "different contents")
                self.assertTrue((root / "a").samefile(root / "linked"))
                self.assertEqual((root / "a").stat().st_mtime_ns, 111)

    def test_metadata_failure_does_not_select_another_backend(self) -> None:
        # An unsupported metadata operation is a reconstruction error rather than evidence that
        # reflinks are unavailable. Report the metadata failure without attempting an overlay.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)

        with (
            mock.patch.object(storage, "reflink", side_effect=clone_file),
            mock.patch.object(storage, "metadata", side_effect=OSError(errno.EOPNOTSUPP, "metadata")),
            mock.patch.object(storage, "Overlay", side_effect=AssertionError("unexpected overlay")),
            self.assertRaises(OSError) as error,
        ):
            with storage.root(storage.Snapshot([self.store])):
                pass
        self.assertEqual(error.exception.errno, errno.EOPNOTSUPP)

    def test_independent_hardlink_groups_can_share_one_blob(self) -> None:
        # Two declared groups have equal contents but different metadata. Each group must keep
        # its own inode when chmod and writes modify the other group in the temporary root.
        self.files()
        os.link(self.tree / "b", self.tree / "second-link")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        self.assertEqual(len(list((self.store / "blobs").iterdir())), 1)
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            self.assertTrue((root / "a").samefile(root / "linked"))
            self.assertTrue((root / "b").samefile(root / "second-link"))
            self.assertFalse((root / "a").samefile(root / "b"))
            (root / "linked").chmod(0o600)
            (root / "a").write_text("first group")
            self.assertEqual((root / "second-link").read_text(), "identical")
            self.assertEqual(stat.S_IMODE((root / "b").stat().st_mode), 0o755)
            (root / "second-link").write_text("second group")
            self.assertEqual((root / "b").read_text(), "second group")
            self.assertEqual((root / "linked").read_text(), "first group")

    def test_reuse_and_metadata_only_changes(self) -> None:
        # Changing only metadata must alter the complete manifest while reusing the parent's blob.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            (root / "b").chmod(0o600)
            os.utime(root / "b", ns=(999, 999))
            storage.capture(restored, child, [self.store], epoch=CAPTURE_EPOCH)
        self.assertEqual(list((child / "blobs").iterdir()), [])
        self.assertNotEqual(
            (child / uapi16.MANIFEST).read_bytes(), (self.store / uapi16.MANIFEST).read_bytes()
        )
        with storage.root(storage.Snapshot([self.store, child]), readonly=True) as restored:
            root = restored.path
            self.assertEqual(stat.S_IMODE((root / "b").stat().st_mode), 0o600)
            self.assertEqual((root / "b").stat().st_mtime_ns, 999)

    def test_deletions_and_replacements(self) -> None:
        # Delete inherited paths, replace files and directories, and rename a fresh directory over
        # an inherited directory. Each child snapshot must describe the complete resulting tree.
        (self.tree / "dir").mkdir()
        (self.tree / "dir/old").write_text("old")
        (self.tree / "deleted").write_text("deleted")
        (self.tree / "file").write_text("file")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            (root / "deleted").unlink()
            shutil.rmtree(root / "dir")
            (root / "fresh").mkdir()
            (root / "fresh/new").write_text("new")
            (root / "fresh").rename(root / "dir")
            (root / "file").unlink()
            (root / "file").mkdir()
            (root / "file/child").write_text("child")
            storage.capture(restored, child, [self.store], epoch=CAPTURE_EPOCH)
        with storage.root(storage.Snapshot([self.store, child])) as restored:
            root = restored.path
            self.assertFalse((root / "deleted").exists())
            self.assertEqual([p.name for p in (root / "dir").iterdir()], ["new"])
            self.assertEqual((root / "file/child").read_text(), "child")
            shutil.rmtree(root / "file")
            (root / "file").write_text("replacement")
            grandchild = self.scratch / "grandchild"
            storage.capture(restored, grandchild, [self.store, child], epoch=CAPTURE_EPOCH)
        with storage.root(storage.Snapshot([self.store, child, grandchild])) as restored:
            root = restored.path
            self.assertEqual((root / "file").read_text(), "replacement")
        self.assertEqual({p.name for p in child.iterdir()}, {"blobs", uapi16.MANIFEST})
        self.assertNotIn(b"overlay.opaque", (child / uapi16.MANIFEST).read_bytes())

    def test_capture_native_overlay_changes(self) -> None:
        # An external overlay can introduce native whiteouts and opaque directories. Capturing
        # its visible filesystem must omit deleted paths and overlay markers from the snapshot.
        (self.tree / "opaque").mkdir()
        (self.tree / "opaque/old").write_text("old")
        (self.tree / "deleted").write_text("deleted")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        lower, upper, work, merged = (self.scratch / name for name in ("lower", "upper", "work", "merged"))
        storage.restore(storage.Snapshot([self.store]), lower)
        upper.mkdir()
        work.mkdir()
        child = self.scratch / "child"
        with Overlay(lower, upper, work, merged) as overlay:
            overlay.mount()
            (merged / "deleted").unlink()
            shutil.rmtree(merged / "opaque")
            (merged / "opaque").mkdir()
            (merged / "opaque/new").write_text("new")
            self.assertEqual(os.getxattr(upper / "opaque", "user.overlay.opaque"), b"y")
            storage.capture(storage.Root(merged, upper), child, [self.store], epoch=CAPTURE_EPOCH)
        with storage.root(storage.Snapshot([self.store, child])) as restored:
            root = restored.path
            self.assertFalse((root / "deleted").exists())
            self.assertEqual([path.name for path in (root / "opaque").iterdir()], ["new"])
        self.assertNotIn(b"overlay.opaque", (child / uapi16.MANIFEST).read_bytes())
        self.assertEqual({path.name for path in child.iterdir()}, {"blobs", uapi16.MANIFEST})

    def test_failed_commands_remove_the_temporary_root(self) -> None:
        # A command failure must remove the temporary root while retaining the reusable snapshot.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        root: Path | None = None
        with self.assertRaisesRegex(RuntimeError, "command failed"):
            with storage.root(storage.Snapshot([self.store])) as restored:
                root = restored.path
                (root / "a").write_text("changed")
                raise RuntimeError("command failed")
        assert root is not None
        self.assertFalse(root.exists())
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            self.assertEqual((root / "a").read_text(), "identical")

    def test_child_stores_only_new_contents(self) -> None:
        # A child reuses every inherited content digest and writes one blob for two new equal files.
        self.files()
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        child = self.scratch / "child"
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            (root / "new").write_text("new contents")
            (root / "new-independent").write_text("new contents")
            storage.capture(restored, child, [self.store], epoch=CAPTURE_EPOCH)
        self.assertEqual(len(list((child / "blobs").iterdir())), 1)
        with storage.root(storage.Snapshot([self.store, child]), readonly=True) as restored:
            root = restored.path
            self.assertEqual((root / "a").read_text(), "identical")
            self.assertFalse((root / "new").samefile(root / "new-independent"))

    def test_readonly_empty_snapshot(self) -> None:
        # An image without parent stores still needs an empty, read-only root with its default mode.
        with storage.root(storage.Snapshot([]), readonly=True) as restored:
            root = restored.path
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            with self.assertRaises(OSError) as error:
                (root / "new").touch()
            self.assertEqual(error.exception.errno, errno.EROFS)

    def test_device_nodes(self) -> None:
        # Preserve device numbers when the namespace permits mknod; unprivileged hosts may deny it.
        try:
            os.mknod(self.tree / "device", stat.S_IFCHR | 0o600, os.makedev(1, 3))
        except PermissionError:
            self.skipTest("the sandbox does not permit device nodes")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        with storage.root(storage.Snapshot([self.store])) as restored:
            root = restored.path
            attributes = (root / "device").lstat()
            self.assertTrue(stat.S_ISCHR(attributes.st_mode))
            self.assertEqual(attributes.st_rdev, os.makedev(1, 3))

    def test_special_files(self) -> None:
        # FIFOs and sockets need manifest inode types because Buck only stores regular blobs.
        os.mkfifo(self.tree / "fifo", 0o600)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.tree / "sock"))
            storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        for readonly in (False, True):
            with (
                self.subTest(readonly=readonly),
                storage.root(storage.Snapshot([self.store]), readonly=readonly) as restored,
            ):
                root = restored.path
                self.assertTrue(stat.S_ISFIFO((root / "fifo").lstat().st_mode))
                self.assertTrue(stat.S_ISSOCK((root / "sock").lstat().st_mode))
                self.assertEqual(stat.S_IMODE((root / "fifo").stat().st_mode), 0o600)

    def test_buck_tree_contains_only_what_buck_stores(self) -> None:
        # A box layer directory must contain only regular files, directories and symlinks with modes
        # that Buck stores. Capture a tree with a hardlink, an extended attribute, a private directory
        # and a name containing a backslash. The Buck tree must contain separate files with normalized
        # modes, no extended attributes and the escaped name.
        self.files()
        os.setxattr(self.tree / "a", "user.example", b"value")
        (self.tree / "private").mkdir(mode=0o700)
        (self.tree / "back\\slash").write_text("escaped")
        (self.tree / "link").symlink_to("a")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        out = self.scratch / "out"
        storage.write_buck_tree(storage.Snapshot([self.store]), out)
        self.assertEqual(stat.S_IMODE((out / "a").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((out / "b").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((out / "private").stat().st_mode), 0o755)
        self.assertFalse((out / "a").samefile(out / "linked"))
        self.assertEqual((out / "linked").read_text(), "identical")
        self.assertEqual(os.listxattr(out / "a"), [])
        self.assertEqual((out / ".esc.back%5Cslash").read_text(), "escaped")
        self.assertEqual(os.readlink(out / "link"), "a")

    def test_buck_tree_rejects_special_files(self) -> None:
        # Buck cannot store a FIFO. Writing the Buck tree of a snapshot that contains one must fail.
        os.mkfifo(self.tree / "fifo")
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        with self.assertRaises(SystemExit):
            storage.write_buck_tree(storage.Snapshot([self.store]), self.scratch / "out")

    def test_delta_keeps_changes_and_returns_deletions(self) -> None:
        # Capture a parent with three files, then change one, add one and delete one in a child.
        # delta() must keep the changed and added files with their directory and return the deleted
        # name.
        (self.tree / "dir").mkdir()
        for name in ("dir/kept", "dir/changed", "gone"):
            (self.tree / name).write_text(name)
        storage.capture(storage.Root(self.tree), self.store, epoch=CAPTURE_EPOCH)
        (self.tree / "dir/changed").write_text("different")
        (self.tree / "dir/new").write_text("new")
        (self.tree / "gone").unlink()
        child = self.scratch / "child"
        storage.capture(storage.Root(self.tree), child, [self.store], epoch=CAPTURE_EPOCH)
        snapshot = storage.Snapshot([self.store, child])
        self.assertEqual(storage.delta(snapshot, 1), ["gone"])
        self.assertEqual({obj["name"] for obj in snapshot.objects[1:]}, {"dir", "dir/changed", "dir/new"})


if __name__ == "__main__":
    unittest.main()
