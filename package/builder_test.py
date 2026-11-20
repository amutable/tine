# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the buildroot that a package build or query runs in."""

import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import builder
import installer


class TestBuildroot(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(tempfile, "tempdir", str(self.scratch)))
        # One parent records the calls of all three, in their order.
        self.calls = mock.Mock()
        self.calls.rootfs.return_value = contextlib.nullcontext(Path("/buildroot"))
        self.calls.chroot.return_value = contextlib.nullcontext(Path("/"))
        self.enterContext(mock.patch.object(builder.rootfs, "rootfs", self.calls.rootfs))
        self.enterContext(mock.patch.object(builder.rootfs, "chroot", self.calls.chroot))
        self.enterContext(mock.patch.object(builder.installer, "install_into", self.calls.install_into))

    def install(self, *packages: str) -> installer.RootInstall:
        closure = self.scratch / "closure"
        closure.mkdir()
        for package in packages:
            (closure / package).write_text("")
        return installer.RootInstall(
            installer="installer", arch="x86_64", packages_dir=str(closure), langs=[], docs=True
        )

    def test_installs_the_closure_into_the_mounted_root_before_the_chroot(self) -> None:
        install = self.install("jq.rpm")
        with builder.buildroot(["base"], install, self.scratch / "topdir") as root:
            self.assertEqual(root, Path("/"))
        self.assertEqual(
            [name for name, _, _ in self.calls.mock_calls], ["rootfs", "install_into", "chroot"]
        )
        self.assertEqual(self.calls.rootfs.call_args.kwargs["lowers"], ["base"])
        self.calls.install_into.assert_called_once_with(install, Path("/buildroot"), self.scratch)
        self.calls.chroot.assert_called_once_with(Path("/buildroot"))

    def test_an_empty_closure_installs_nothing(self) -> None:
        with builder.buildroot(["base"], self.install(), self.scratch / "topdir"):
            pass
        self.calls.install_into.assert_not_called()

    def test_a_failed_install_fails_before_the_chroot(self) -> None:
        self.calls.install_into.side_effect = subprocess.CalledProcessError(1, ["installer"])
        with (
            self.assertRaises(subprocess.CalledProcessError),
            builder.buildroot(["base"], self.install("jq.rpm"), self.scratch / "topdir"),
        ):
            pass
        self.calls.chroot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
