# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Exercise scoped mounts inside the test sandbox's mount namespace."""

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, chdir
from pathlib import Path
from typing import override
from unittest import mock

import isolation


class TestChroot(unittest.TestCase):
    def test_root_and_cwd_are_restored_on_success_and_failure(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            for fail in (False, True):
                with self.subTest(fail=fail):
                    try:
                        with isolation.chroot(root):
                            self.assertEqual(Path.cwd(), Path("/"))
                            self.assertTrue(Path("/inside").exists())
                            if fail:
                                raise RuntimeError("body failed")
                    except RuntimeError as error:
                        self.assertTrue(fail)
                        self.assertEqual(str(error), "body failed")
                    self.assertEqual(Path.cwd(), previous)
                    self.assertTrue((root / "inside").exists())

    def test_failed_chroot_closes_the_root_descriptor(self) -> None:
        with (
            mock.patch.object(os, "open", return_value=123),
            mock.patch.object(os, "chroot", side_effect=OSError("chroot failed")),
            mock.patch.object(os, "close") as close,
            self.assertRaisesRegex(OSError, "chroot failed"),
            isolation.chroot(Path("/missing")),
        ):
            self.fail("failed chroot must not enter the body")
        close.assert_called_once_with(123)

    def test_reentry_does_not_lose_the_saved_root(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            context = isolation.chroot(root)
            with context:
                with self.assertRaisesRegex(RuntimeError, "already entered"), context:
                    self.fail("an entered chroot context must reject reentry")
                self.assertTrue(Path("/inside").exists())
            self.assertEqual(Path.cwd(), previous)
            self.assertTrue((root / "inside").exists())

    def test_failed_chdir_restores_the_root_and_cwd(self) -> None:
        previous = Path.cwd()
        chdir = os.chdir

        def fail_chdir(path: str) -> None:
            if Path(path) == Path("/"):
                raise OSError("chdir failed")
            chdir(path)

        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "inside").touch()
            with (
                mock.patch.object(os, "chdir", side_effect=fail_chdir),
                self.assertRaisesRegex(OSError, "chdir failed"),
                isolation.chroot(root),
            ):
                self.fail("failed chdir must not enter the body")
            self.assertEqual(Path.cwd(), previous)
            self.assertTrue((root / "inside").exists())


class TestStartup(unittest.TestCase):
    def test_import_does_not_load_heavy_helpers(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import isolation; "
                "print('\\n'.join(sys.modules))",
                str(Path(isolation.__file__).parent),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        unwanted = {"annotationlib", "dataclasses", "inspect", "pathlib"}
        if sys.version_info >= (3, 14):  # noqa: UP036
            unwanted.add("typing")
        self.assertFalse(unwanted & set(process.stdout.splitlines()))


class TestLibseccomp(unittest.TestCase):
    def test_libseccomp_is_found_in_each_library_directory(self) -> None:
        for directory in ("usr/lib64", f"usr/lib/{os.uname().machine}-linux-gnu", "usr/lib"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory(dir="/var/tmp") as root:
                library = Path(root, directory, "libseccomp.so.2")
                library.parent.mkdir(parents=True)
                library.touch()

                self.assertEqual(isolation._libseccomp(root), str(library))

    def test_a_root_without_libseccomp_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            with self.assertRaises(isolation.SandboxOSError) as raised:
                isolation._libseccomp(root)

            self.assertEqual(raised.exception.message, "the box contains no libseccomp.so.2")


def _path(fd: int) -> Path:
    """Close `fd` and return the path it referred to."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    finally:
        os.close(fd)


class TestKernel(unittest.TestCase):
    def test_the_running_kernel_passes(self) -> None:
        isolation.check_kernel()

    def test_an_unavailable_syscall_names_the_kernel_and_seccomp(self) -> None:
        # No kernel implements system call number 1023, so the probe fails with ENOSYS like an old kernel.
        for release in ("6.12", "7.1"):
            with (
                self.subTest(release=release),
                mock.patch.object(isolation, "_NR_OPEN_TREE_ATTR", 1023),
                mock.patch.object(os, "uname", return_value=os.uname_result(("Linux", "", release, "", ""))),
                self.assertRaises(isolation.SandboxOSError) as raised,
            ):
                isolation.check_kernel()
            self.assertIn(f"unavailable on Linux {release}", raised.exception.message)
            self.assertIn("open_tree_attr()", raised.exception.message)
            self.assertIn("seccomp", raised.exception.message)

    def test_an_unsupported_root_explains_pivot_root_failure(self) -> None:
        with (
            mock.patch.object(isolation, "_move_mount"),
            mock.patch.object(os, "fchdir"),
            mock.patch.object(isolation._LIBC, "pivot_root", return_value=-1),
            mock.patch.object(isolation.ctypes, "get_errno", return_value=errno.EINVAL),
            self.assertRaises(isolation.SandboxOSError) as raised,
        ):
            isolation._switch_root(123)
        self.assertIn("initramfs", raised.exception.message)


class TestPaths(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp")))
        self.fd = os.open(self.root, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)

    def test_dot_components_do_not_escape_the_root(self) -> None:
        (self.root / "sub").mkdir()
        for path in ("/", "/.", "/..", "/../..", "/sub/..", "/sub/../../"):
            with self.subTest(path=path):
                self.assertEqual(_path(isolation._directory(self.fd, path)), self.root)

    def test_absolute_symlinks_resolve_inside_the_root(self) -> None:
        (self.root / "real").mkdir()
        (self.root / "real/file").touch()
        (self.root / "alias").symlink_to("/real")
        (self.root / "real/link").symlink_to("/real/file")

        self.assertEqual(_path(isolation._lookup(self.fd, "/alias/link")), self.root / "real/file")
        self.assertEqual(
            _path(isolation._lookup(self.fd, "/alias/link", os.O_NOFOLLOW)), self.root / "real/link"
        )

    def test_parent_components_are_resolved_after_symlinks(self) -> None:
        (self.root / "sub/directory").mkdir(parents=True)
        (self.root / "jump").symlink_to("sub/directory")
        (self.root / "sub/file").touch()

        self.assertEqual(_path(isolation._lookup(self.fd, "/jump/../file")), self.root / "sub/file")

    def test_a_lookup_that_races_with_a_mount_is_retried(self) -> None:
        busy = BlockingIOError(errno.EAGAIN, "openat2")
        with mock.patch.object(isolation, "_syscall", side_effect=[busy, busy, 42]) as syscall:
            self.assertEqual(isolation._lookup(self.fd, "/sub/../file"), 42)
        self.assertEqual(syscall.call_count, 3)

    def test_relative_paths_resolve_against_the_working_directory(self) -> None:
        (self.root / "target").touch()
        (self.root / "alias").symlink_to("target")

        with chdir(self.root):
            self.assertEqual(_path(isolation._lookup(isolation.AT_FDCWD, "alias")), self.root / "target")
            self.assertEqual(
                _path(isolation._lookup(isolation.AT_FDCWD, "alias", os.O_NOFOLLOW)), self.root / "alias"
            )

    def test_missing_directories_are_created_inside_the_root(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp")))
        (self.root / outside.relative_to("/")).mkdir(parents=True)
        (self.root / "alias").symlink_to(outside)

        created = _path(isolation._directory(self.fd, "/alias/new/directory"))

        self.assertEqual(created, self.root / outside.relative_to("/") / "new/directory")
        self.assertFalse((outside / "new").exists())

    def test_symlink_target_text_is_preserved(self) -> None:
        target = self.root / "link"
        isolation.Symlink("./usr/bin/", target).attach()

        self.assertEqual(os.readlink(target), "./usr/bin/")

    def test_symlink_parents_are_resolved_inside_the_root(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp")))
        inside = self.root / outside.relative_to("/")
        inside.mkdir(parents=True)
        (self.root / "alias").symlink_to(outside)

        isolation.Symlink("target", "/alias/link").attach(self.fd)

        self.assertFalse((outside / "link").is_symlink())
        self.assertEqual((inside / "link").readlink(), Path("target"))

    def test_mounts_sort_by_components_and_bind_over_symlinks(self) -> None:
        parent = isolation.Tmpfs("/usr")
        child = isolation.Bind("/source", "/usr/./bin/")
        sibling = isolation.Tmpfs("/usr-bin")
        link = isolation.Symlink("usr/lib", "/lib")
        bind = isolation.Bind("/source", "/lib")

        self.assertEqual(
            sorted([sibling, child, bind, parent, link], key=isolation._filesystem_key),
            [link, bind, parent, child, sibling],
        )


class TestExitStack(unittest.TestCase):
    def test_every_context_exits_in_reverse_order_when_an_exit_fails(self) -> None:
        calls = mock.MagicMock()
        calls.middle.__exit__.side_effect = OSError("middle")
        with self.assertRaisesRegex(OSError, "middle"), isolation.ExitStack() as stack:
            for context in (calls.first, calls.middle, calls.last):
                stack.enter_context(context)
        self.assertEqual(
            [call[0] for call in calls.mock_calls if call[0].endswith("__exit__")],
            ["last.__exit__", "middle.__exit__", "first.__exit__"],
        )


class TestMountContexts(unittest.TestCase):
    def test_failed_mount_metadata_lookup_detaches_the_mount(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, target, jail = (root / name for name in ("source", "target", "jail"))
            for path in (source, target, jail):
                path.mkdir()
            (target / "underlying").touch()
            with (
                isolation.Bind("/proc", jail / "proc") as proc,
                isolation.Close(os.open(target, os.O_PATH | os.O_DIRECTORY)) as fd,
                isolation.Bind(source, ".") as mounted,
            ):
                proc.mount()
                with (
                    isolation.chroot(jail),
                    mock.patch.object(isolation, "_statmount_point", side_effect=OSError("lookup failed")),
                    self.assertRaisesRegex(OSError, "lookup failed"),
                ):
                    os.fchdir(fd)
                    mounted.mount()
            self.assertTrue((target / "underlying").exists())

    def test_overlay_paths_may_contain_option_separators(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            # The mount option parser splits options at `,`. overlayfs splits lowerdir at `:` and treats `\`
            # as an escape. overlayfs gets its layers as file descriptors and doesn't parse their paths.
            top, bottom, upper, work = (root / name for name in ("to:p", "bot,tom", "up,per", "wo\\rk"))
            for path in (top, bottom, upper, work):
                path.mkdir()
            (top / "original").write_text("original")
            (bottom / "underneath").write_text("underneath")
            (top / "shadowed").write_text("from the top")
            (bottom / "shadowed").write_text("from the bottom")
            target = root / "target"
            with isolation.Overlay((top, bottom), upper, work, target, lazy_unmount=False) as mounted:
                mounted.mount()
                self.assertEqual((target / "original").read_text(), "original")
                self.assertEqual((target / "underneath").read_text(), "underneath")
                self.assertEqual((target / "shadowed").read_text(), "from the top")
                (target / "generated").write_text("output")
            self.assertEqual((upper / "generated").read_text(), "output")

    def test_overlay_without_an_upper_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            top, bottom = root / "top", root / "bottom"
            for path in (top, bottom):
                path.mkdir()
            (top / "shadowed").write_text("from the top")
            (bottom / "shadowed").write_text("from the bottom")
            (bottom / "underneath").write_text("underneath")
            target = root / "target"
            with isolation.Overlay((top, bottom), None, None, target, lazy_unmount=False) as mounted:
                mounted.mount()
                self.assertEqual((target / "shadowed").read_text(), "from the top")
                self.assertEqual((target / "underneath").read_text(), "underneath")
                with self.assertRaises(OSError) as raised:
                    (target / "generated").write_text("output")
                self.assertEqual(raised.exception.errno, errno.EROFS)

    def test_overlay_needs_a_workdir_with_its_upper_and_two_lowers_without(self) -> None:
        for lowers, upper, work in ((("lower",), "upper", None), (("lower",), None, None)):
            with self.subTest(lowers=lowers, upper=upper, work=work), self.assertRaises(ValueError):
                isolation.Overlay(lowers, upper, work, "target", lazy_unmount=False)

    def test_overlay_parent_keeps_its_mode_with_a_permissive_umask(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            lower, upper, work = (root / name for name in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            target = root / "parent/target"

            with ExitStack() as stack:
                stack.callback(os.umask, os.umask(0))
                stack.enter_context(mock.patch.object(isolation, "_move_mount"))
                with isolation.Overlay((lower,), upper, work, target, lazy_unmount=True) as overlay:
                    overlay.attach()

            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_readonly_file_bind_covers_a_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, other, target = (root / name for name in ("source", "other", "target"))
            source.write_text("source")
            other.write_text("other")
            target.symlink_to("other")

            with isolation.Bind(source, target, readonly=True) as mounted:
                mounted.mount()
                self.assertEqual(target.read_text(), "source")
                with self.assertRaises(OSError) as raised:
                    target.write_text("changed")
                self.assertEqual(raised.exception.errno, errno.EROFS)
                self.assertEqual(other.read_text(), "other")

            self.assertEqual(target.readlink(), Path("other"))
            self.assertEqual(source.read_text(), "source")

    def test_file_covers_its_target_with_its_own_content_read_only(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("host")

            with isolation.File("box", target) as mounted:
                mounted.mount()
                self.assertEqual(target.read_text(), "box")
                with self.assertRaises(OSError) as raised:
                    target.write_text("changed")
                self.assertEqual(raised.exception.errno, errno.EROFS)

            self.assertEqual(target.read_text(), "host")

    def test_bind_takes_its_source_from_a_tree(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            top, bottom, target = root / "top", root / "bottom", root / "target"
            (top / "real").mkdir(parents=True)
            (top / "real/file").write_text("from the top")
            (bottom / "real").mkdir(parents=True)
            (bottom / "real/other").write_text("from the bottom")
            # An absolute symlink in a tree resolves inside the tree, not in the host's root.
            (top / "alias").symlink_to("/real")
            for layers in ((top,), (top, bottom)):
                with self.subTest(layers=len(layers)):
                    with (
                        isolation.Close(
                            isolation.open_layers(tuple(str(layer) for layer in layers))
                        ) as tree,
                        isolation.Bind("/alias", target, root=tree) as bind,
                    ):
                        bind.mount()
                        self.assertEqual((target / "file").read_text(), "from the top")
                        self.assertEqual((target / "other").exists(), len(layers) == 2)
                        with self.assertRaises(OSError) as raised:
                            (target / "generated").touch()
                        self.assertEqual(raised.exception.errno, errno.EROFS)

    def test_a_failed_attach_names_the_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.touch()
            target.mkdir()

            with isolation.Bind(source, target) as bind, self.assertRaises(OSError) as raised:
                bind.attach()

            self.assertEqual(raised.exception.filename, str(target))

    def test_mounts_unwind_on_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            lower, upper, work = (root / name for name in ("lower", "upper", "work"))
            for path in (lower, upper, work):
                path.mkdir()
            target = root / "target"
            target.mkdir()
            (target / "underlying").touch()
            mounts = [
                isolation.Bind(lower, target),
                isolation.Tmpfs(target),
                isolation.Devices(target),
                # Strict, so that unwinding covers both unmount flavours.
                isolation.Overlay((lower,), upper, work, target, lazy_unmount=False),
            ]
            for mount in mounts:
                for fail in (False, True):
                    with self.subTest(mount=type(mount).__name__, fail=fail):
                        try:
                            with mount as entered:
                                self.assertIs(entered, mount)
                                entered.mount()
                                self.assertFalse((target / "underlying").exists())
                                (target / "during").touch()
                                if fail:
                                    raise RuntimeError("body failed")
                        except RuntimeError as error:
                            self.assertTrue(fail)
                            self.assertEqual(str(error), "body failed")
                        self.assertTrue((target / "underlying").exists())
                        self.assertFalse((target / "during").exists())

    def test_unmount_uses_original_target_after_symlink_and_cwd_change(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            target, other = root / "target", root / "other"
            target.mkdir()
            other.mkdir()
            (target / "underlying").touch()
            alias = root / "alias"
            alias.symlink_to("target")
            with ExitStack() as stack:
                stack.enter_context(chdir(root))
                with isolation.Tmpfs(Path("alias")) as mounted:
                    mounted.mount()
                    alias.unlink()
                    alias.symlink_to("other")
                    stack.enter_context(chdir(other))
                    self.assertFalse((target / "underlying").exists())
                self.assertTrue((target / "underlying").exists())

    def test_mount_unmounts_while_proc_is_covered(self) -> None:
        # The child process covers /proc in a mount namespace of its own. Its mounts are slaves of the
        # test's mounts, so the tmpfs on /proc doesn't propagate back to the test.
        script = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import isolation\n"
            "isolation.unshare(isolation.CLONE_NEWNS)\n"
            "isolation._set_propagation('/', isolation.MS_SLAVE)\n"
            "with isolation.Tmpfs('/proc') as proc:\n"
            "    proc.attach()\n"
            "with isolation.Tmpfs(sys.argv[2]) as mounted:\n"
            "    mounted.mount()\n"
            "    assert os.path.ismount(sys.argv[2])\n"
            "assert not os.path.ismount(sys.argv[2])\n"
        )
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            process = subprocess.run(
                [sys.executable, "-I", "-c", script, str(Path(isolation.__file__).parent), directory],
                capture_output=True,
                text=True,
            )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_bind_of_symlink_unmounts_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.symlink_to("missing")
            with isolation.Bind(source, target, nofollow=True) as mounted:
                mounted.mount()
                self.assertEqual(target.readlink(), Path("missing"))
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.is_file())

    def test_a_failed_enter_closes_its_file_descriptors(self) -> None:
        mounts = [
            (
                isolation.Devices("/var/tmp/dev"),
                mock.patch.object(isolation, "_touch", side_effect=OSError("failed")),
            ),
            (
                isolation.Tmpfs("/var/tmp/tmpfs"),
                mock.patch.object(isolation, "_fsmount", side_effect=OSError("failed")),
            ),
        ]
        for mount, failure in mounts:
            with self.subTest(mount=type(mount).__name__):
                before = os.listdir("/proc/self/fd")
                with failure, self.assertRaisesRegex(OSError, "failed"), mount:
                    self.fail("a failed __enter__ must not enter the body")
                self.assertEqual(os.listdir("/proc/self/fd"), before)
                self.assertEqual(mount.fd, -1)


if __name__ == "__main__":
    unittest.main()
