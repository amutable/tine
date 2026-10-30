# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for image layer filesystem operations.

buck test tine//image:test
"""

import os
import struct
import tempfile
import unittest
from pathlib import Path

import layer


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
        with tempfile.TemporaryDirectory(prefix="layer-test.", dir="/var/tmp") as scratch:
            tree = Path(scratch)
            tree.chmod(0o700)
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

            layer._normalize_modes(tree)

            self.assertEqual(tree.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/file").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/secret").stat().st_mode & 0o7777, 0o644)
            self.assertEqual((tree / "sub/script").stat().st_mode & 0o7777, 0o755)
            self.assertEqual((tree / "sub/mount").stat().st_mode & 0o7777, 0o755)
            self.assertTrue((tree / "link").is_symlink())
            # not content: whatever it is, it is not ours to rewrite
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
