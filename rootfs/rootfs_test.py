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
        # overlayfs takes the mode and the times of the mount root from the upper directory. The
        # setgid bit and the old timestamp make the test fail if the root has the mode and the
        # times of a fresh upper directory.
        self.source.chmod(0o2750)
        os.utime(self.source, ns=(1234567890123456789, 1234567890123456789))

    def test_an_invalid_overlay_has_a_possible_locked_mount_hint(self) -> None:
        failure = OSError(errno.EINVAL, "fsconfig create: Invalid argument")
        with (
            mock.patch.object(rootfs.Overlay, "__enter__", side_effect=failure),
            self.assertRaises(OSError) as raised,
            rootfs.source_overlay(self.source, self.scratch / "target"),
        ):
            pass
        self.assertIn("Locked mounts below the source can cause EINVAL", "".join(raised.exception.__notes__))

    def test_preserves_the_tree(self) -> None:
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
            with rootfs.rootfs(scratch / "target", bind=lower, apivfs=True) as tree:
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
            rootfs.rootfs(scratch / "target", bind=lower, apivfs=True) as tree,
        ):
            (tree / "var/tmp/staged").write_text("staged", encoding="utf-8")
            backing = next(scratch.glob("var-tmp.*"))
            self.assertEqual((backing / "staged").read_text(encoding="utf-8"), "staged")
            self.assertEqual(backing.stat().st_mode & 0o7777, 0o1777)


class TestMountPoints(unittest.TestCase):
    def test_an_absolute_symlink_in_a_layer_resolves_inside_the_root(self) -> None:
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="mount-test.", dir="/var/tmp")))
        outside = scratch / "outside"
        outside.mkdir()
        lower = scratch / "lower"
        (lower / outside.relative_to("/")).mkdir(parents=True)
        # In the image, /run names the copy of `outside` inside the image. On the host, the same path
        # is `outside`.
        (lower / "run").symlink_to(outside)
        for apivfs in (False, True):
            with (
                self.subTest(apivfs=apivfs),
                rootfs.rootfs(
                    scratch / "target",
                    bind=lower,
                    binds=[] if apivfs else [(scratch, "/run/project")],
                    apivfs=apivfs,
                ) as tree,
            ):
                # The bind of `scratch` is on the same filesystem as the root, so is_mount() can't detect
                # it. `scratch` contains `lower`, and the root doesn't.
                if not apivfs:
                    self.assertTrue((tree / outside.relative_to("/") / "project/lower").is_dir())
                self.assertFalse(outside.is_mount())
                self.assertFalse((outside / "project").exists())


class TestReadonlyProject(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="project.", dir="/var/tmp")))
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "input").write_text("input", encoding="utf-8")

    def test_a_missing_project_is_not_created(self) -> None:
        with self.assertRaises(FileNotFoundError), rootfs.readonly_project(self.root / "missing", {}):
            self.fail("a missing project must not be created")
        self.assertFalse((self.root / "missing").exists())

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
                        # The writes are checked before the working directory. Without the fchdir()
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

    def test_a_symlink_outside_the_project_is_left_alone(self) -> None:
        # The output path names a symlink, but only through the symlink `store` out of `project`.
        (self.root / "elsewhere/gen").mkdir(parents=True)
        (self.root / "elsewhere/gen/out").symlink_to("target")
        (self.project / "store").symlink_to(self.root / "elsewhere")
        with chdir(self.project), self.assertRaises(ValueError):
            with rootfs.readonly_project(self.project, {Path("store/gen/out"): self.root / "mounted"}):
                pass
        self.assertTrue((self.root / "elsewhere/gen/out").is_symlink())

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

        capture.assert_called_once_with(output)

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
    def test_makes_directories_writable_and_escapes_backslashes(self) -> None:
        # Buck can't remove a tree through a directory without owner write access, and it can't store a
        # name with a backslash. Capture a tree with a read-only directory that contains such a name, and
        # check that the directory becomes writable and the name is escaped.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            tree = Path(directory)
            (tree / "readonly").mkdir()
            (tree / "readonly/back\\slash").write_text("kept")
            (tree / "readonly").chmod(0o555)

            rootfs.capture(tree)

            self.assertEqual((tree / "readonly").stat().st_mode & 0o777, 0o755)
            self.assertEqual([path.name for path in (tree / "readonly").iterdir()], [".esc.back%5Cslash"])


if __name__ == "__main__":
    unittest.main()
