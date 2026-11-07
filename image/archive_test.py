# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Terminal metadata and archive identity regressions."""

import os
import shutil
import stat
import tarfile
import tempfile
import unittest
from contextlib import chdir
from decimal import Decimal
from pathlib import Path
from typing import override
from unittest import mock

import specs

import archive
import cpio
import finalize
import storage
import uapi16

EPOCH = 1000000000


# Captures in these tests keep every timestamp, so they clamp to an epoch after all of them.
CAPTURE_EPOCH = 2**31


class TestArchive(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp")))
        source = self.scratch / "source"
        (source / "empty-dir").mkdir(parents=True)
        for name, mode, ns in (("a", 0o640, 123), ("b", 0o755, 456), ("empty", 0o600, 789)):
            path = source / name
            path.write_text("contents" if name != "empty" else "")
            path.chmod(mode)
            os.utime(path, ns=(ns, ns))
        os.link(source / "a", source / "linked")
        os.link(source / "empty", source / "empty-linked")
        (source / "link").symlink_to("/a")
        for name in ("back\\slash", ".wh.literal", ".esc.literal", "TRAILER!!!"):
            (source / name).write_text(name)
        os.mkfifo(source / "fifo", 0o600)
        os.setxattr(source / "a", "user.example", b"value")
        (source / "var/lib/database").mkdir(parents=True)
        (source / "var/lib/database/contents").write_text("database")
        self.store = self.scratch / "store"
        storage.capture(source, self.store, epoch=CAPTURE_EPOCH)
        self.spec = finalize.ImageSpec(lower=[str(self.store)], tmpfiles=[])

    def test_archive_writers_read_blobs_without_reconstruction(self) -> None:
        # A terminal without tmpfiles can package its snapshot directly. Tar must preserve
        # declared links, independent equal files, original nanoseconds and user attributes.
        out = self.scratch / "image.tar"
        with mock.patch.object(storage, "root", side_effect=AssertionError("unexpected reconstruction")):
            with finalize.snapshot(self.spec, program="test", epoch=EPOCH) as snapshot:
                archive._archive(snapshot, out, "tar", EPOCH, "none")
        with tarfile.open(out) as packed:
            objects = {entry.name: entry for entry in packed.getmembers()}
            self.assertTrue(objects["./linked"].islnk())
            self.assertEqual(objects["./linked"].linkname, "./a")
            self.assertTrue(objects["./b"].isreg())
            self.assertEqual(objects["./a"].mode, 0o644)
            self.assertEqual(objects["./a"].pax_headers["mtime"], "0.000000123")
            self.assertEqual(objects["./a"].pax_headers["SCHILY.xattr.user.example"], "value")
            self.assertEqual(objects["./link"].linkname, "/a")
            self.assertTrue(objects["./fifo"].isfifo())
            self.assertIn("./back\\slash", objects)

    def test_cpio_preserves_nonempty_and_empty_hardlinks(self) -> None:
        # Newc needs one payload and a shared inode per declared group, including empty files.
        # Equal contents alone must not give unrelated regular files the same inode.
        out = self.scratch / "image.cpio"
        with finalize.snapshot(self.spec, program="test", epoch=EPOCH) as snapshot:
            archive._archive(snapshot, out, "cpio", EPOCH, "none")
        objects = {obj.name: obj for obj in cpio.read(out.read_bytes())}
        self.assertGreater(objects["./TRAILER!!!"].size, 0)
        self.assertEqual(objects["a"].ino, objects["linked"].ino)
        self.assertEqual(objects["a"].nlink, 2)
        self.assertEqual(objects["empty"].ino, objects["empty-linked"].ino)
        self.assertNotEqual(objects["a"].ino, objects["b"].ino)
        dest = self.scratch / "unpacked"
        with out.open("rb") as stream:
            cpio.unpack(stream.fileno(), dest)
        self.assertTrue((dest / "a").samefile(dest / "linked"))
        self.assertTrue((dest / "empty").samefile(dest / "empty-linked"))
        self.assertFalse((dest / "a").samefile(dest / "b"))

    def test_tmpfiles_and_database_removal_match_exported_manifest(self) -> None:
        # Apply terminal tmpfiles and database removal before writing both the archive and its
        # listing. The manifest must describe the modes and timestamps that tar actually encodes.
        out, listing = self.scratch / "image.tar", self.scratch / "listing"
        spec = specs.write(
            self.scratch / "spec.json",
            dict(self.spec)
            | {
                "out": str(out),
                "format": "tar",
                "compression": "none",
                "manifest": str(listing),
                "pkgdb_paths": ["var/lib/database"],
                "tmpfiles": ["z /a 0600 - - -", "f /created 0640 - - - authored"],
            },
        )
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": str(EPOCH)}):
            archive.main(["--spec", str(spec)])
        objects = {obj.get("name", "."): obj for obj in uapi16.read(listing)}
        with tarfile.open(out) as packed:
            for entry in packed.getmembers():
                name = entry.name.removeprefix("./")
                obj = objects[name]
                if not entry.issym():
                    self.assertEqual(entry.mode, obj["mode"])
                ns = int(Decimal(entry.pax_headers.get("mtime", str(entry.mtime))) * 10**9)
                self.assertEqual(ns, obj["mTimeNSec"])
            stream = packed.extractfile("./created")
            assert stream is not None
            with stream:
                self.assertEqual(stream.read(), b"authored")
        self.assertEqual(objects["a"]["mode"], 0o600)
        self.assertEqual(objects["linked"]["mode"], 0o600)
        self.assertNotIn("var/lib/database", objects)
        self.assertNotIn("var", objects)

    def test_directory_export_restores_after_materialization(self) -> None:
        # The directory artifact contains only safe blob names and a uapi16. Copying that
        # artifact before export must not lose modes, times, hardlinks or logical filenames.
        out, copied = self.scratch / "directory", self.scratch / "copied"
        with finalize.snapshot(self.spec, program="test", epoch=EPOCH) as snapshot:
            archive._archive(snapshot, out, "directory", EPOCH, "none")
        shutil.copytree(out, copied, copy_function=shutil.copyfile)
        with chdir(self.scratch):
            archive.export(["--store", "copied", "exported"])
        exported = self.scratch / "exported"
        self.assertEqual((exported / "a").stat().st_mtime_ns, 123)
        self.assertEqual(stat.S_IMODE((exported / "a").stat().st_mode), 0o644)
        self.assertTrue((exported / "a").samefile(exported / "linked"))
        self.assertFalse((exported / "a").samefile(exported / "b"))
        self.assertTrue((exported / "back\\slash").exists())
        self.assertEqual({p.name for p in out.iterdir()}, {"blobs", uapi16.MANIFEST})

    def test_export_refuses_a_destination_created_during_validation(self) -> None:
        # Another process inserts a destination and a symlink while the store is validated.
        # Export must fail without following that symlink or changing the unrelated file.
        destination, victim = self.scratch / "exported", self.scratch / "victim"
        victim.write_text("unrelated")
        read = storage.Snapshot

        def raced(stores: list[Path]) -> storage.Snapshot:
            snapshot = read(stores)
            destination.mkdir()
            (destination / "a").symlink_to(victim)
            return snapshot

        with mock.patch.object(storage, "Snapshot", side_effect=raced), self.assertRaises(SystemExit):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertEqual(victim.read_text(), "unrelated")

    def test_export_keeps_the_open_directory_after_rename(self) -> None:
        # Another process renames the private destination after it is opened and inserts a symlink.
        # Restoration must stay in the opened directory and leave the symlink's target unchanged.
        destination, saved = self.scratch / "exported", self.scratch / "renamed"
        victim = self.scratch / "victim"
        victim.mkdir()
        (victim / "a").write_text("unrelated")
        restore = storage.restore

        def raced(snapshot: storage.Snapshot, tree: Path) -> None:
            destination.rename(saved)
            destination.symlink_to(victim, target_is_directory=True)
            restore(snapshot, tree)

        with mock.patch.object(storage, "restore", side_effect=raced):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertEqual((victim / "a").read_text(), "unrelated")
        self.assertEqual((saved / "a").read_text(), "contents")

    def test_export_rejects_a_replacement_directory(self) -> None:
        # Another process replaces the destination before open with a private directory containing
        # a symlink. Export must reject the nonempty replacement without following the symlink.
        destination, saved = self.scratch / "exported", self.scratch / "renamed"
        replacement, victim = self.scratch / "replacement", self.scratch / "victim"
        replacement.mkdir(mode=0o700)
        victim.write_text("unrelated")
        (replacement / "a").symlink_to(victim)
        open_directory = os.open

        def raced(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
            if path == destination.name:
                destination.rename(saved)
                replacement.rename(destination)
            return open_directory(path, flags, dir_fd=dir_fd)

        with mock.patch.object(archive.os, "open", side_effect=raced), self.assertRaises(SystemExit):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertEqual(victim.read_text(), "unrelated")

    def test_export_keeps_the_parent_directory_after_rename(self) -> None:
        # Another user replaces an ancestor after mkdir. The destination then resolves to an existing
        # private empty directory. Opening through the original parent must retain the created root.
        parent, saved = self.scratch / "parent", self.scratch / "renamed"
        parent.mkdir()
        destination = parent / "out"
        victim = self.scratch / "victim"
        (victim / "out").mkdir(parents=True, mode=0o700)
        os.utime(victim / "out", ns=(111, 111))
        open_directory = os.open

        def raced(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
            if path == destination.name:
                parent.rename(saved)
                parent.symlink_to(victim, target_is_directory=True)
            return open_directory(path, flags, dir_fd=dir_fd)

        with mock.patch.object(archive.os, "open", side_effect=raced):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertEqual(list((victim / "out").iterdir()), [])
        self.assertEqual((victim / "out").stat().st_mtime_ns, 111)
        self.assertEqual((saved / "out/a").read_text(), "contents")

    def test_export_requires_a_protected_parent(self) -> None:
        # A writable nonsticky parent permits another user to replace the new root with an existing
        # private empty directory. Reject that parent before creating or changing either directory.
        parent = self.scratch / "shared"
        parent.mkdir()
        parent.chmod(0o777)
        private = parent / "private"
        private.mkdir(mode=0o700)
        destination = parent / "out"
        with self.assertRaises(SystemExit):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertFalse(destination.exists())
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)

        # A trusted sticky parent prevents other users from renaming the exporter's private root.
        parent.chmod(0o1777)
        archive.export(["--store", str(self.store), str(destination)])
        self.assertEqual((destination / "a").read_text(), "contents")

    def test_export_creates_a_protected_parent_with_a_shared_umask(self) -> None:
        # Direct execution can inherit a shared umask. A missing parent must be created with
        # protected permissions so the exporter can restore the image beneath it.
        destination = self.scratch / "missing" / "out"
        previous = os.umask(0o002)
        try:
            archive.export(["--store", str(self.store), str(destination)])
        finally:
            os.umask(previous)
        self.assertEqual((destination / "a").read_text(), "contents")

    def test_export_rejects_an_unmapped_owner_matching_the_caller(self) -> None:
        # A user namespace shows an unmapped parent owner as the overflow uid, which can equal the
        # caller's uid. Report the overflow uid as both the caller's uid and the owner of the parent, and
        # check that the export fails without creating the destination.
        destination = self.scratch / "out"
        parent = self.scratch.stat()
        overflow = int(Path("/proc/sys/kernel/overflowuid").read_text())
        fstat = os.fstat

        def attributes(fd: int) -> os.stat_result:
            result = fstat(fd)
            if result.st_ino == parent.st_ino and result.st_dev == parent.st_dev:
                values = list(result)
                values[4] = overflow
                return os.stat_result(values)
            return result

        with (
            mock.patch.object(archive.os, "geteuid", return_value=overflow),
            mock.patch.object(archive.os, "fstat", side_effect=attributes),
            self.assertRaises(SystemExit),
        ):
            archive.export(["--store", str(self.store), str(destination)])
        self.assertFalse(destination.exists())

    def test_database_removal_resolves_intermediate_symlinks(self) -> None:
        # A database can be reached through a relative or absolute directory symlink. Direct
        # terminal export and tmpfiles finalization must remove it inside the image in either case.
        source = self.scratch / "source"
        (source / "var/lib").rename(source / "relocated")
        for index, target in enumerate(("../relocated", "/relocated")):
            (source / "var/lib").unlink(missing_ok=True)
            (source / "var/lib").symlink_to(target)
            store = self.scratch / f"store-{index}"
            storage.capture(source, store, epoch=CAPTURE_EPOCH)
            for rules in ([], ["d /empty-dir - - -"]):
                with self.subTest(target=target, rules=rules):
                    spec = finalize.ImageSpec(lower=[str(store)], tmpfiles=rules)
                    with finalize.snapshot(
                        spec, program="test", epoch=EPOCH, pkgdb_paths=["var/lib/database"]
                    ) as snapshot:
                        names = {obj["name"] for obj in snapshot.objects[1:]}
                        self.assertNotIn("relocated/database", names)
                        self.assertNotIn("relocated/database/contents", names)
                        self.assertIn("var/lib", names)

    def test_database_removal_updates_the_root_timestamp(self) -> None:
        # Removing the last var subtree changes the root mtime. The direct snapshot path must
        # match filesystem removal when an existing directory has a no-op tmpfiles rule.
        source = self.scratch / "source"
        for present in (True, False):
            if not present:
                shutil.rmtree(source / "var/lib/database")
            os.utime(source, ns=(111, 111))
            storage.capture(source, self.store, epoch=CAPTURE_EPOCH)
            for rules in ([], ["d /empty-dir - - -"]):
                with self.subTest(present=present, rules=rules):
                    spec = finalize.ImageSpec(lower=self.spec["lower"], tmpfiles=rules)
                    with finalize.snapshot(
                        spec, program="test", epoch=EPOCH, pkgdb_paths=["var/lib/database"]
                    ) as snapshot:
                        self.assertEqual(snapshot.objects[0]["mTimeNSec"], EPOCH * 10**9)
                        self.assertNotIn("var", {obj["name"] for obj in snapshot.objects[1:]})

    def test_missing_database_does_not_require_reconstruction(self) -> None:
        # A cached snapshot can contain a device even when the current worker cannot create it.
        # An absent database without symlink parents must still be archivable directly from blobs.
        objects = uapi16.read(self.store / uapi16.MANIFEST)
        objects.append(
            {"name": "device", "type": "chr", "mode": 0o600, "mTimeNSec": 111, "major": 1, "minor": 3}
        )
        uapi16.write_objects(objects, self.store / uapi16.MANIFEST)
        out = self.scratch / "image.tar"
        with mock.patch.object(storage, "root", side_effect=AssertionError("unexpected reconstruction")):
            with finalize.snapshot(
                self.spec, program="test", epoch=EPOCH, pkgdb_paths=["absent/database"]
            ) as snapshot:
                archive._archive(snapshot, out, "tar", EPOCH, "none")
        with tarfile.open(out) as packed:
            self.assertTrue(packed.getmember("./device").ischr())

    def test_missing_database_leaves_nonempty_parent_times_unchanged(self) -> None:
        # A missing database under a nonempty directory removes nothing. Its parent timestamps
        # must remain unchanged rather than being set to the terminal epoch.
        source = self.scratch / "source"
        shutil.rmtree(source / "var/lib/database")
        (source / "var/lib/keep").touch()
        os.utime(source / "var/lib", ns=(333, 333))
        os.utime(source, ns=(111, 111))
        storage.capture(source, self.store, epoch=CAPTURE_EPOCH)
        with finalize.snapshot(
            self.spec, program="test", epoch=EPOCH, pkgdb_paths=["var/lib/database"]
        ) as snapshot:
            objects = {obj.get("name", "."): obj for obj in snapshot.objects}
            self.assertEqual(objects["."]["mTimeNSec"], 111)
            self.assertEqual(objects["var/lib"]["mTimeNSec"], 333)

    def test_cpio_listing_uses_terminal_timestamp_precision(self) -> None:
        # Cpio finalization must round the listing to whole seconds and omit user attributes,
        # while keeping the explicit hardlink group used by the archive.
        out, listing = self.scratch / "image.cpio", self.scratch / "listing"
        spec = specs.write(
            self.scratch / "spec.json",
            dict(self.spec)
            | {
                "out": str(out),
                "manifest": str(listing),
                "format": "cpio",
                "compression": "none",
                "pkgdb_paths": [],
            },
        )
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": str(EPOCH)}):
            archive.main(["--spec", str(spec)])
        objects = {obj.get("name", "."): obj for obj in uapi16.read(listing)}
        self.assertEqual(objects["a"]["mTimeNSec"], 0)
        self.assertNotIn("extendedAttributes", objects["a"])
        self.assertEqual(objects["a"]["inodeToken"], objects["linked"]["inodeToken"])


if __name__ == "__main__":
    unittest.main()
