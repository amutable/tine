# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for image layer filesystem operations.

buck test tine//image:test
"""

import errno
import os
import shutil
import stat
import struct
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from typing import override
from unittest import mock

import specs

import finalize
import layer
import rootfs
import storage
import uapi16
from isolation import umount2

# Captures in these tests keep every timestamp, so they clamp to an epoch after all of them.
CAPTURE_EPOCH = 2**31


class TestImageWrites(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp"))
        )
        self.project = self.scratch / "project"
        self.project.mkdir()
        self.out = self.scratch / "out"
        self.out.mkdir()
        (self.project / "source").write_text("copied", encoding="utf-8")
        # The sandbox sets SOURCE_DATE_EPOCH for every action, and layer.main() clamps times to it.
        self.enterContext(mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": str(2**31)}))

    def _build(self, operations: list[object], lower: Path | None = None) -> None:
        spec = specs.write(
            self.scratch / "spec.json",
            {
                "out": str(self.out),
                "lower": [str(lower)] if lower is not None else [],
                "install": None,
                "operations": operations,
            },
        )
        with chdir(self.project):
            layer.main(["--spec", str(spec)])

    def test_absolute_symlinks_resolve_inside_the_image(self) -> None:
        outside = self.scratch / "outside"
        outside.mkdir()
        inside = self.out / outside.relative_to("/")
        inside.mkdir(parents=True)
        (self.out / "alias").symlink_to(outside)
        (self.out / "usr").mkdir()
        (self.out / "usr/lib").symlink_to(outside)

        with (
            chdir(self.project),
            rootfs.rootfs(self.scratch / "target", bind=self.out) as target,
        ):
            layer._apply(["copy", "source", "/alias/file"], target)
            layer._apply(["os_release", {"ID": "test"}], target)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((inside / "file").read_text(encoding="utf-8"), "copied")
        self.assertEqual((inside / "os-release").read_text(encoding="utf-8"), 'ID="test"\n')

    def test_a_copy_source_must_stay_relative_to_the_project(self) -> None:
        for source in ("/absolute", "../outside", "source/../../outside"):
            with self.subTest(source=source), self.assertRaisesRegex(SystemExit, "relative project path"):
                layer._apply(["copy", source, "/destination"], self.out)

    def test_a_derived_layer_exposes_a_readonly_project_only_to_copy_and_run(self) -> None:
        # Copy and run must see a read-only project, while removals affect only the image snapshot.
        lower = self.scratch / "lower"
        lower.mkdir()
        (lower / "cache.pyc").write_text("image cache", encoding="utf-8")
        (self.project / "cache.pyc").write_text("cache", encoding="utf-8")
        stored = self.scratch / "stored"
        storage.capture(storage.Root(lower), stored, epoch=CAPTURE_EPOCH)

        def run(tag: str, cmd: object, env: object, cwd: str | None = None) -> None:
            self.assertEqual(cwd, layer.PROJECT)
            source = Path(layer.PROJECT) / "source"
            self.assertEqual(source.read_text(encoding="utf-8"), "copied")
            with self.assertRaises(OSError) as raised:
                source.write_text("changed", encoding="utf-8")
            self.assertEqual(raised.exception.errno, errno.EROFS)

        with mock.patch.object(layer, "_run", side_effect=run) as command:
            self._build(
                [
                    ["copy", "source", "/copied"],
                    ["run", ["command"], {}, True],
                    ["remove", "/**/*.pyc"],
                ],
                stored,
            )

        command.assert_called_once()
        with storage.root(storage.Snapshot([stored, self.out])) as restored:
            tree = restored.path
            self.assertEqual((tree / "copied").read_text(encoding="utf-8"), "copied")
            self.assertFalse((tree / "cache.pyc").exists())
        self.assertTrue((self.project / "cache.pyc").exists())

    def test_commands_see_inherited_metadata_and_can_override_it(self) -> None:
        # Capture before artifact copying, then check a command sees the original metadata and
        # explicit hardlinks. Its metadata changes must become authoritative in the child snapshot.
        source = self.scratch / "source-root"
        source.mkdir()
        (source / "file").write_text("contents")
        (source / "file").chmod(0o640)
        os.utime(source / "file", ns=(123, 123))
        os.link(source / "file", source / "linked")
        parent = self.scratch / "parent"
        storage.capture(storage.Root(source), parent, epoch=CAPTURE_EPOCH)
        copied = self.scratch / "copied-parent"
        shutil.copytree(parent, copied, copy_function=shutil.copyfile)

        def command(*args: object) -> None:
            path = Path("/buildroot/file")
            self.assertEqual(path.stat().st_mtime_ns, 123)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertTrue(path.samefile("/buildroot/linked"))
            path.chmod(0o600)
            os.utime(path, ns=(456, 456))

        with mock.patch.object(layer, "_run", side_effect=command):
            self._build([["run", ["command"], {}, False]], copied)
        with storage.root(storage.Snapshot([copied, self.out])) as restored:
            tree = restored.path
            self.assertEqual(stat.S_IMODE((tree / "file").stat().st_mode), 0o600)
            self.assertEqual((tree / "linked").stat().st_mtime_ns, 456)
        self.assertEqual(list((self.out / "blobs").iterdir()), [])

    def test_a_live_writer_prevents_capture(self) -> None:
        # A command retains an open image file after it returns. Strict unmount must fail before
        # capture can publish contents that the retained descriptor could still change.
        descriptor = -1

        def command(*args: object) -> None:
            nonlocal descriptor
            descriptor = os.open("/buildroot/file", os.O_WRONLY | os.O_CREAT, 0o644)

        try:
            with mock.patch.object(layer, "_run", side_effect=command), self.assertRaises(OSError) as error:
                self._build([["run", ["command"], {}, False]])
            self.assertEqual(error.exception.errno, errno.EBUSY)
            self.assertFalse((self.out / uapi16.MANIFEST).exists())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
                if Path("/buildroot").is_mount():
                    umount2("/buildroot", 0)

    def test_commands_create_hardlinks_to_inherited_files(self) -> None:
        # A command links an inherited independent file and changes its data and mode. Capture
        # must use upper inode identity so the next image keeps the new aliases hardlinked.
        source = self.scratch / "source-root"
        source.mkdir()
        (source / "file").write_text("contents")
        parent = self.scratch / "parent"
        storage.capture(storage.Root(source), parent, epoch=CAPTURE_EPOCH)

        def command(*args: object) -> None:
            os.link("/buildroot/file", "/buildroot/linked")
            Path("/buildroot/file").write_text("changed")
            Path("/buildroot/linked").chmod(0o600)

        with mock.patch.object(layer, "_run", side_effect=command):
            self._build([["run", ["command"], {}, False]], parent)
        with storage.root(storage.Snapshot([parent, self.out])) as restored:
            tree = restored.path
            self.assertTrue((tree / "file").samefile(tree / "linked"))
            self.assertEqual((tree / "linked").read_text(), "changed")
            self.assertEqual(stat.S_IMODE((tree / "file").stat().st_mode), 0o600)


class TestRemove(unittest.TestCase):
    def test_plain_path_is_also_a_glob(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            removed = tree / "removed"
            removed.write_text("removed")

            layer._remove_glob(tree, "/removed")

            self.assertFalse(removed.exists())

    def test_recursive_glob_includes_hidden_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            (tree / "root.txt").write_text("root")
            (tree / ".hidden.txt").write_text("hidden")
            (tree / "sub").mkdir()
            (tree / "sub/nested.txt").write_text("nested")
            (tree / "sub/kept").write_text("kept")

            layer._remove_glob(tree, "/**/*.txt")

            self.assertEqual(
                {path.relative_to(tree) for path in tree.rglob("*")}, {Path("sub"), Path("sub/kept")}
            )

    def test_glob_removes_matching_directory_trees(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            (tree / "var/cache/one/nested").mkdir(parents=True)
            (tree / "var/cache/two").write_text("two")
            (tree / "var/cache-kept").mkdir()

            layer._remove_glob(tree, "/var/cache/*")

            self.assertEqual(list((tree / "var/cache").iterdir()), [])
            self.assertTrue((tree / "var/cache-kept").is_dir())

    def test_unmatched_glob_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            kept = tree / "kept"
            kept.write_text("kept")

            layer._remove_glob(tree, "/missing-*")

            self.assertTrue(kept.is_file())

    def test_pattern_must_be_an_absolute_image_path(self) -> None:
        for pattern in ("relative/*", "/safe/../outside"):
            with self.subTest(pattern=pattern), self.assertRaises(SystemExit):
                layer._remove_glob(Path("/"), pattern)


class TestNormalization(unittest.TestCase):
    def test_modes(self) -> None:
        # Terminal policy normalizes regular files and directories without following links or
        # changing special-file modes. Authored tmpfiles rules run after this normalization.
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch) / "source"
            tree.mkdir(mode=0o700)
            (tree / "sub").mkdir()
            (tree / "sub").chmod(0o1777)
            (tree / "sub/file").write_text("file")
            (tree / "sub/file").chmod(0o664)
            (tree / "sub/secret").write_text("secret")
            (tree / "sub/secret").chmod(0o600)
            (tree / "sub/script").write_text("script")
            (tree / "sub/script").chmod(0o777)
            (tree / "sub/mount").write_text("mount")
            (tree / "sub/mount").chmod(0o4755)
            (tree / "link").symlink_to("sub/secret")
            os.mkfifo(tree / "fifo", 0o600)

            store = Path(scratch) / "store"
            storage.capture(storage.Root(tree), store, epoch=CAPTURE_EPOCH)
            snapshot = storage.Snapshot([store])
            finalize.normalize_snapshot(snapshot)
            tree = Path(scratch) / "normalized"
            storage.restore(snapshot, tree)

            self.assertEqual(tree.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/file").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/secret").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/script").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/mount").stat().st_mode & 0o7777, 0o755)
            self.assertTrue((tree / "link").is_symlink())
            self.assertEqual((tree / "fifo").lstat().st_mode & 0o7777, 0o600)


def _elf(
    *, bits: int = 64, order: str = "<", e_type: int = 3, machine: int = 62, stack: int | None = 0o6
) -> bytes:
    """An ELF header and its program header table: a PT_LOAD, and a PT_GNU_STACK with `stack` flags."""
    ident = b"\x7fELF" + bytes([bits // 32, 1 if order == "<" else 2, 1]) + bytes(9)
    headers = [(1, 0o5)] + ([] if stack is None else [(0x6474E551, stack)])
    if bits == 64:
        ehdr = struct.Struct(order + "16sHHIQQQIHHHHHH")
        phdr = struct.Struct(order + "IIQQQQQQ")
        table = b"".join(phdr.pack(p_type, flags, 0, 0, 0, 0, 0, 0) for p_type, flags in headers)
    else:
        ehdr = struct.Struct(order + "16sHHIIIIIHHHHHH")
        phdr = struct.Struct(order + "IIIIIIII")
        table = b"".join(phdr.pack(p_type, 0, 0, 0, 0, 0, flags, 0) for p_type, flags in headers)
    header = ehdr.pack(
        ident, e_type, machine, 1, 0, ehdr.size, 0, 0, ehdr.size, phdr.size, len(headers), 0, 0, 0
    )
    return header + table


class TestExecutableStacks(unittest.TestCase):
    def test_reports_executable_and_missing_stacks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            (tree / "bin").mkdir()
            (tree / "lib").mkdir()
            (tree / "bin/rw").write_bytes(_elf())
            (tree / "bin/rwe").write_bytes(_elf(stack=0o7))
            (tree / "bin/missing").write_bytes(_elf(stack=None))
            (tree / "bin/link").symlink_to("rwe")
            (tree / "lib/i386-missing.so").write_bytes(_elf(bits=32, machine=3, stack=None))
            # EM_AARCH64: glibc's default stack there is not executable, so no PT_GNU_STACK is fine
            (tree / "lib/aarch64-missing.so").write_bytes(_elf(machine=183, stack=None))
            # EM_PPC, for the byte order
            (tree / "lib/be32-rw.so").write_bytes(_elf(bits=32, order=">", machine=20))
            (tree / "lib/be32-rwe.so").write_bytes(_elf(bits=32, order=">", machine=20, stack=0o7))
            # relocatable, as a kernel module or BPF object: no program headers, nothing to load
            (tree / "lib/module.ko").write_bytes(_elf(e_type=1, stack=None))
            (tree / "lib/truncated").write_bytes(_elf(stack=0o7)[:70])
            (tree / "lib/text").write_text("\x7fELF but not really")
            (tree / "lib/empty").write_bytes(b"")

            self.assertEqual(
                layer._executable_stacks(tree),
                [
                    "/bin/missing: no PT_GNU_STACK",
                    "/bin/rwe: executable stack",
                    "/lib/be32-rwe.so: executable stack",
                    "/lib/i386-missing.so: no PT_GNU_STACK",
                ],
            )
