# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for source overlays and root filesystem output capture."""

import errno
import os
import tempfile
import unittest
from contextlib import ExitStack, chdir, nullcontext
from pathlib import Path
from typing import override
from unittest import mock

import rootfs
from isolation import Bind


class TestSourceOverlay(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="source-test.", dir="/var/tmp"))
        )
        self.enterContext(mock.patch.dict(os.environ, {"TMPDIR": str(self.scratch)}))
        self.enterContext(mock.patch.object(tempfile, "tempdir", None))
        self.project = self.scratch / "project"
        self.source = self.project / "input"
        (self.source / "data").mkdir(parents=True)
        (self.source / "data/file").write_text("original", encoding="utf-8")
        (self.source / "data/file").chmod(0o444)
        os.utime(self.source / "data/file", ns=(1234567890123456789, 1234567890123456789))
        (self.source / ".hidden").touch()
        (self.source / "file-link").symlink_to("data/file")
        (self.source / "directory-link").symlink_to("data")
        (self.source / "dangling-link").symlink_to("missing")
        (self.source / "data/relative-link").symlink_to("../file-link")
        (self.source / "cycle").symlink_to("cycle")
        (self.source / "absolute-link").symlink_to(self.scratch / "outside")
        (self.scratch / "outside").write_text("outside", encoding="utf-8")
        (self.source / "escaping-dangling-link").symlink_to(self.scratch / "absent")
        (self.source / "empty").mkdir()
        (self.source / "readonly").mkdir(mode=0o555)
        for name in (".wh.original", ".wh..wh..opq", ".esc.literal"):
            (self.source / name).write_text(name, encoding="utf-8")
        # overlayfs takes the mode and the times of the mount root from the upper directory. The
        # setgid bit and the old timestamp make the test fail if the root has the mode and the
        # times of a fresh upper directory.
        self.source.chmod(0o2750)
        os.utime(self.source, ns=(1234567890123456789, 1234567890123456789))

    def test_preserves_the_tree_without_decoding_image_markers(self) -> None:
        with chdir(self.project), rootfs.source_overlay(Path("input"), self.scratch / "build/src") as tree:
            original = {path.relative_to(self.source) for path in self.source.rglob("*")}
            self.assertEqual({path.relative_to(tree) for path in tree.rglob("*")}, original)
            # rglob() does not yield the directory that it walks, so the loop adds the root.
            for path in (Path(), *original):
                source, overlaid = self.source / path, tree / path
                self.assertEqual(overlaid.lstat().st_mode, source.lstat().st_mode, path)
                self.assertEqual(overlaid.lstat().st_mtime_ns, source.lstat().st_mtime_ns, path)
                if source.is_symlink():
                    self.assertEqual(overlaid.readlink(), source.readlink())
                elif source.is_file():
                    self.assertEqual(overlaid.read_bytes(), source.read_bytes())
            self.assertFalse((tree / "dangling-link").exists())
            self.assertFalse((tree / "escaping-dangling-link").exists())
            # The test runs in the mount namespace of the setup, so the target of `absolute-link`
            # exists.
            self.assertEqual((tree / "absolute-link").read_text(encoding="utf-8"), "outside")
            self.assertEqual((tree / "directory-link/file").read_text(encoding="utf-8"), "original")
            self.assertEqual((tree / "data/relative-link").read_text(encoding="utf-8"), "original")
            upper = next(self.scratch.glob("source.*/0/upper"))
            self.assertEqual(list(upper.iterdir()), [])
        self.assertEqual(list(self.scratch.glob("source.*")), [])

    def test_shows_the_mounts_below_the_source_and_discards_writes_into_them(self) -> None:
        # `tine mount` binds a checkout over a directory of the project, and rpmbuild builds the project
        # in place.
        mounted, nested = self.scratch / "mounted", self.scratch / "nested"
        (mounted / "inner").mkdir(parents=True)
        (mounted / "file").write_text("mounted", encoding="utf-8")
        nested.mkdir()
        (nested / "file").write_text("nested", encoding="utf-8")
        (self.source / "data/covered").mkdir()
        (self.source / "data/covered/file").write_text("covered", encoding="utf-8")
        target = self.scratch / "target"
        with ExitStack() as stack:
            stack.enter_context(Bind(mounted, self.source / "data/covered")).mount()
            stack.enter_context(Bind(nested, self.source / "data/covered/inner")).mount()
            tree = stack.enter_context(rootfs.source_overlay(self.source, target))
            self.assertEqual((tree / "data/covered/file").read_text(encoding="utf-8"), "mounted")
            self.assertEqual((tree / "data/covered/inner/file").read_text(encoding="utf-8"), "nested")
            (tree / "data/covered/file").write_text("changed", encoding="utf-8")
            (tree / "data/covered/inner/file").unlink()
            (tree / "data/covered/inner/generated").touch()
        self.assertEqual((mounted / "file").read_text(encoding="utf-8"), "mounted")
        self.assertEqual(sorted(path.name for path in nested.iterdir()), ["file"])
        self.assertEqual((self.source / "data/covered/file").read_text(encoding="utf-8"), "covered")
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list(self.scratch.glob("source.*")), [])

    def test_shows_the_file_that_a_file_mount_below_the_source_covers(self) -> None:
        mounted = self.scratch / "mounted"
        mounted.write_text("mounted", encoding="utf-8")
        target = self.scratch / "target"
        with ExitStack() as stack:
            stack.enter_context(Bind(mounted, self.source / "data/file")).mount()
            tree = stack.enter_context(rootfs.source_overlay(self.source, target))
            self.assertEqual((tree / "data/file").read_text(encoding="utf-8"), "original")

    def test_renaming_a_source_directory_fails_like_a_container(self) -> None:
        # The docstring of `source_overlay()` states that rename(2) of a directory fails with EXDEV.
        with rootfs.source_overlay(self.source, self.scratch / "target") as tree:
            with self.assertRaises(OSError) as caught:
                (tree / "data").rename(tree / "moved")
            self.assertEqual(caught.exception.errno, errno.EXDEV)
            # overlayfs copies a file or a symlink up, so rename(2) of a symlink works.
            (tree / "file-link").rename(tree / "moved-link")

    def test_writes_are_discarded_after_success_and_failure(self) -> None:
        # A write or a chmod() through a symlink of the source must change a copy in the upper
        # directory and leave the target in the source unchanged.
        before = (self.source / "data/file").stat()
        target = self.scratch / "target"
        for fail in (False, True):
            with self.subTest(fail=fail):
                # The teardown must not swallow an exception that the build raises.
                raises = self.assertRaises(RuntimeError) if fail else nullcontext()
                # `self.scratch` exists. If the body never assigns `upper`, the assertFalse() below
                # fails.
                upper = self.scratch
                with raises:
                    with rootfs.source_overlay(self.source, target) as tree:
                        upper = next(self.scratch.glob("source.*/0/upper"))
                        self.assertFalse((tree / "generated").exists())
                        (tree / "data/file").chmod(0o600)
                        (tree / "data/relative-link").write_text("changed", encoding="utf-8")
                        self.assertEqual((tree / "file-link").read_text(encoding="utf-8"), "changed")
                        (tree / ".hidden").unlink()
                        (tree / "dangling-link").unlink()
                        (tree / "readonly").chmod(0o700)
                        (tree / "generated").mkdir(mode=0o500)
                        (tree / "back\\slash").touch()
                        (tree / "opaque").mkdir()
                        os.setxattr(tree / "opaque", "user.overlay.opaque", b"y")
                        if fail:
                            raise RuntimeError("build failed")
                self.assertFalse(upper.exists())
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual((self.source / "data/file").read_text(encoding="utf-8"), "original")
                self.assertEqual((self.source / "data/file").stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual((self.source / "data/file").stat().st_mode & 0o777, 0o444)
                self.assertEqual((self.source / "readonly").stat().st_mode & 0o777, 0o555)
                self.assertTrue((self.source / ".hidden").is_file())
                self.assertEqual((self.source / "dangling-link").readlink(), Path("missing"))
                self.assertFalse((self.source / "generated").exists())
                self.assertEqual(list(self.scratch.glob("source.*")), [])


class TestBuildrootScratch(unittest.TestCase):
    def _lower(self) -> tuple[Path, Path]:
        scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="scratch-test.", dir="/var/tmp"))
        )
        lower = scratch / "lower"
        for name in ("dev", "proc", "run", "tmp", "var/tmp"):
            (lower / name).mkdir(parents=True)
        return scratch, lower

    def test_is_a_tmpfs_outside_a_run_action(self) -> None:
        scratch, lower = self._lower()
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(scratch)}),
            mock.patch.object(tempfile, "tempdir", None),
            mock.patch.dict(os.environ),
        ):
            os.environ.pop("BUCK_SCRATCH_PATH", None)
            with rootfs.rootfs(scratch / "target", lowers=[lower], apivfs=True) as tree:
                (tree / "var/tmp/staged").write_text("staged", encoding="utf-8")
                self.assertTrue((tree / "var/tmp").is_mount())
                self.assertEqual(list(scratch.glob("var-tmp.*")), [])

    def test_is_backed_where_tmpdir_points(self) -> None:
        scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="scratch-test.", dir="/var/tmp"))
        )
        lower = scratch / "lower"
        for name in ("dev", "proc", "run", "tmp", "var/tmp"):
            (lower / name).mkdir(parents=True)
        # BUCK_SCRATCH_PATH is a path in the project, which `readonly_project()` can make read-only.
        # The backing directory must be in TMPDIR, so the test sets a BUCK_SCRATCH_PATH that does
        # not exist.
        environment = {"BUCK_SCRATCH_PATH": "/nonexistent/project/scratch", "TMPDIR": str(scratch)}
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(tempfile, "tempdir", None),
            rootfs.rootfs(scratch / "target", lowers=[lower], apivfs=True) as tree,
        ):
            (tree / "var/tmp/staged").write_text("staged", encoding="utf-8")
            backing = next(scratch.glob("var-tmp.*"))
            self.assertEqual((backing / "staged").read_text(encoding="utf-8"), "staged")
            self.assertEqual(backing.stat().st_mode & 0o7777, 0o1777)


class TestReadonlyProject(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="project.", dir="/var/tmp")))
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "input").write_text("input", encoding="utf-8")

    def test_only_the_outputs_stay_writable(self) -> None:
        # The build drivers pass two outputs. The path of a kept output has several components, so
        # the second output is nested.
        outputs = {Path("out"): self.root / "mounted", Path("__tine/bin"): self.root / "mounted-bin"}
        # An earlier build that kept this output on a content-based path left a symlink at the
        # output path.
        (self.root / "elsewhere").mkdir()
        (self.project / "out").symlink_to(self.root / "elsewhere")
        for fail in (False, True):
            # The test runs from a subdirectory, because the output paths are relative to `project`
            # and not to the working directory.
            with self.subTest(fail=fail), chdir(self.project / "src"):
                raises = self.assertRaises(RuntimeError) if fail else nullcontext()
                with raises:
                    with rootfs.readonly_project(self.project, outputs):
                        # The writes are checked before the working directory. Without the chdir()
                        # in `readonly_project()`, a relative path resolves through the writable
                        # mount underneath. The write checks then fail too, and not only the check
                        # of the working directory.
                        for path in (
                            Path("input"),
                            self.project / "input",
                            Path("src/probe"),
                            Path("out/inside"),
                        ):
                            with self.assertRaises(OSError) as caught:
                                path.write_text("changed", encoding="utf-8")
                            self.assertEqual(caught.exception.errno, errno.EROFS, path)
                        self.assertEqual(Path.cwd(), self.project)
                        for mounted in outputs.values():
                            (mounted / "built").write_text(str(fail), encoding="utf-8")
                        if fail:
                            raise RuntimeError("build failed")
                # Path.cwd() raises if the process is still in the detached bind.
                self.assertEqual(Path.cwd(), self.project / "src")
                for output, mounted in outputs.items():
                    self.assertFalse(mounted.is_mount())
                    # The stale symlink is now a directory, so the bind did not follow the symlink
                    # out of `project`.
                    self.assertTrue((self.project / output).is_dir())
                    self.assertFalse((self.project / output).is_symlink())
                    self.assertEqual(
                        (self.project / output / "built").read_text(encoding="utf-8"), str(fail)
                    )
                self.assertEqual(list((self.root / "elsewhere").iterdir()), [])
                # `project` is writable again after the context exits.
                (self.project / "input").write_text("input", encoding="utf-8")

    def test_an_output_leaving_the_project_is_refused(self) -> None:
        # `store` is a symlink out of `project`. The other outputs leave `project` through `..` or
        # name an absolute path outside it.
        (self.root / "elsewhere").mkdir()
        (self.project / "store").symlink_to(self.root / "elsewhere")
        for output in (
            Path("store/gen/out"),
            Path("../out"),
            Path("out/../../out"),
            self.root / "elsewhere/out",
        ):
            with self.subTest(output=output), chdir(self.project):
                with self.assertRaises(ValueError):
                    with rootfs.readonly_project(self.project, {output: self.root / "mounted"}):
                        pass
                self.assertEqual(list((self.root / "elsewhere").iterdir()), [])
                self.assertFalse((self.root / "out").exists())
                self.assertFalse((self.root / "mounted").is_mount())

    def test_two_outputs_sharing_a_mount_point_are_refused(self) -> None:
        mounted = self.root / "mounted"
        with chdir(self.project), self.assertRaises(ValueError):
            with rootfs.readonly_project(self.project, {Path("out"): mounted, Path("other"): mounted}):
                pass

    def test_an_output_is_named_relative_to_the_project_or_by_its_full_path(self) -> None:
        mounted = self.root / "mounted"
        for output in (Path("out"), self.project / "out"):
            with self.subTest(output=output), chdir(self.project / "src"):
                with rootfs.readonly_project(self.project, {output: mounted}):
                    (mounted / "built").write_text("built", encoding="utf-8")
                self.assertEqual((self.project / "out/built").read_text(encoding="utf-8"), "built")


class TestCaptureOnExit(unittest.TestCase):
    def test_captures_after_success(self) -> None:
        output = Path("output")
        with mock.patch.object(rootfs, "capture") as capture:
            with rootfs.capture_on_exit(output):
                pass

        capture.assert_called_once_with(output, None)

    def test_capture_failure_does_not_hide_body_failure(self) -> None:
        output = Path("output")
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(RuntimeError, "transaction failed") as raised,
        ):
            with rootfs.capture_on_exit(output):
                raise RuntimeError("transaction failed")

        self.assertEqual(
            raised.exception.__notes__,
            ["rootfs capture of output also failed: OSError: bad name"],
        )

    def test_capture_failure_after_success_is_reported(self) -> None:
        with (
            mock.patch.object(rootfs, "capture", side_effect=OSError("bad name")),
            self.assertRaisesRegex(OSError, "bad name"),
            rootfs.capture_on_exit("output"),
        ):
            pass


class TestCapture(unittest.TestCase):
    def test_keeps_an_opaque_mark_only_where_a_lower_layer_has_the_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            lower, upper = Path(directory) / "lower", Path(directory) / "upper"
            (lower / "usr/share/doc/old").mkdir(parents=True)
            (lower / "usr/share/doc/old/file").write_text("old")
            for name in ("old", "new"):
                (upper / "usr/share/doc" / name).mkdir(parents=True, exist_ok=True)
                os.setxattr(str(upper / "usr/share/doc" / name), b"user.overlay.opaque", b"y")

            rootfs.capture(upper, [lower])

            self.assertEqual(rootfs.deletions(upper), [Path("usr/share/doc/old/.wh..wh..opq")])
            self.assertRaises(OSError, os.getxattr, str(upper / "usr/share/doc/new"), b"user.overlay.opaque")

    def test_keeps_every_opaque_mark_without_lower_layers(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            tree = Path(directory)
            (tree / "new").mkdir()
            os.setxattr(str(tree / "new"), b"user.overlay.opaque", b"y")

            rootfs.capture(tree)

            self.assertEqual(rootfs.deletions(tree), [Path("new/.wh..wh..opq")])


class TestDeletions(unittest.TestCase):
    def test_lists_whiteouts_and_opaque_markers_only(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            delta = Path(directory)
            (delta / "etc/opaque").mkdir(parents=True)
            (delta / "etc/added").write_text("added")
            (delta / "etc/.wh.removed").write_bytes(b"")
            (delta / "etc/opaque/.wh..wh..opq").write_bytes(b"")
            (delta / ".esc..wh.esc%5Caped").write_bytes(b"")
            (delta / ".esc.back%5Cslash").write_text("kept")

            self.assertEqual(
                rootfs.deletions(delta),
                [Path(".esc..wh.esc%5Caped"), Path("etc/.wh.removed"), Path("etc/opaque/.wh..wh..opq")],
            )


if __name__ == "__main__":
    unittest.main()
