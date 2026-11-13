# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for shared package installation root handling."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import installer
import storage


class TestPathRules(unittest.TestCase):
    def test_every_rule_names_a_directory_tree(self) -> None:
        self.assertEqual(installer.path_rules(docs=True, langs=[]), [])
        rules = installer.path_rules(docs=False, langs=["en"])
        self.assertIn((False, "usr/share/doc"), rules)
        self.assertLess(rules.index((False, "usr/share/locale")), rules.index((True, "usr/share/locale/en")))
        for _, tree in rules:
            self.assertNotIn("*", tree)


class TestConfigureBox(unittest.TestCase):
    def test_keeps_authselect_nsswitch_link(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            factory = root / "usr/share/factory/etc/nsswitch.conf"
            factory.parent.mkdir(parents=True)
            factory.write_text("passwd: files systemd\n")
            (root / "etc").mkdir()
            nsswitch = root / "etc/nsswitch.conf"
            nsswitch.symlink_to("/etc/authselect/nsswitch.conf")

            installer._configure_box(root)

            self.assertEqual(nsswitch.readlink(), Path("/etc/authselect/nsswitch.conf"))


def _snapshot(root: Path, name: str, files: dict[str, str]) -> Path:
    """Capture a tree with `files` as the snapshot store `name` under `root`, with the epoch of the tests."""
    tree = root / f"{name}.tree"
    # A box root contains the mount points of the API filesystems. Without them, the install would create
    # them and a box layer would export them.
    for path in ("etc", "dev", "proc", "run", "tmp", "var/tmp"):
        (tree / path).mkdir(parents=True)
    for path, content in files.items():
        (tree / path).write_text(content)
    store = root / name
    storage.capture(storage.Root(tree), store, epoch=0)
    return store


class TestRun(unittest.TestCase):
    def test_package_scripts_tolerate_unmapped_acl_groups(self) -> None:
        # The test box mounts /var/tmp as tmpfs, so a host mounted with noacl cannot hide this failure.
        for mode in ("fresh", "layered", "mounted"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory(dir="/var/tmp") as scratch,
                mock.patch.dict(os.environ, {"SYSTEMD_IN_CHROOT": "0", "SOURCE_DATE_EPOCH": "0"}),
            ):
                root = Path(scratch)
                target = root / "target"
                target.mkdir()
                lower = _snapshot(root, "lower", {}) if mode == "layered" else None
                (root / "packages").mkdir()
                (root / "packages" / "systemd.rpm").write_text("")
                spec = installer.InstallSpec(
                    arch="x86_64",
                    packages_dir=str(root / "packages"),
                    snapshot=None if mode == "mounted" else str(root / "snapshot"),
                    installroot=str(target) if mode == "mounted" else None,
                    lower=[str(lower)] if lower is not None else [],
                    target=None,
                    box_config=False,
                    langs=[],
                    docs=True,
                )

                def install(
                    packages_dir: Path,
                    installroot: Path,
                    request: installer.InstallSpec,
                ) -> None:
                    (installroot / "journal").mkdir()
                    # Debian's systemd postinst applies an ACL for adm (GID 4), which a namespace
                    # mapping only root cannot store. tmpfiles tolerates it in a detected chroot.
                    subprocess.run(
                        ["systemd-tmpfiles", "--create", f"--root={installroot}", "-"],
                        input="a+ /journal - - - - group:4:r-x\n",
                        text=True,
                        check=True,
                    )

                with mock.patch.object(installer.specs, "parse", return_value=spec):
                    installer.run("install", install, [])

    def test_a_box_layer_may_add_but_not_delete(self) -> None:
        # The sandbox mounts the directories of a box's layers without decoding deletions. Install a box
        # layer over a lower snapshot that adds a file, and once more so that it also deletes a file of the
        # lower snapshot. The first install must export only the added file, the second must fail.
        for deletes in (False, True):
            with (
                self.subTest(deletes=deletes),
                tempfile.TemporaryDirectory(dir="/var/tmp") as scratch,
                mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "0"}),
            ):
                root = Path(scratch)
                lower = _snapshot(root, "lower", {"doomed": ""})
                (root / "packages").mkdir()
                (root / "packages" / "systemd.rpm").write_text("")
                spec = installer.InstallSpec(
                    arch="x86_64",
                    packages_dir=str(root / "packages"),
                    snapshot=str(root / "snapshot"),
                    installroot=None,
                    lower=[str(lower)],
                    target=str(root / "target"),
                    box_config=False,
                    langs=[],
                    docs=True,
                )

                def install(
                    packages_dir: Path,
                    installroot: Path,
                    request: installer.InstallSpec,
                    deletes: bool = deletes,
                ) -> None:
                    (installroot / "added").write_text("")
                    if deletes:
                        (installroot / "doomed").unlink()

                with mock.patch.object(installer.specs, "parse", return_value=spec):
                    if deletes:
                        with self.assertRaisesRegex(SystemExit, "cannot delete .*doomed"):
                            installer.run("install", install, [])
                    else:
                        installer.run("install", install, [])
                        self.assertEqual([path.name for path in (root / "target").iterdir()], ["added"])

    def test_an_empty_closure_repeats_the_lower_snapshot(self) -> None:
        # A layered install with no packages to install must not run the package manager. Install an empty
        # package directory over one lower snapshot, and check that the package manager doesn't run and
        # that the new snapshot restores the same tree as the lower snapshot.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            lower = _snapshot(root, "lower", {"etc/os-release": "ID=test\n"})
            (root / "packages").mkdir()
            spec = installer.InstallSpec(
                arch="x86_64",
                packages_dir=str(root / "packages"),
                snapshot=str(root / "snapshot"),
                installroot=None,
                lower=[str(lower)],
                target=None,
                box_config=False,
                langs=[],
                docs=True,
            )
            install = mock.Mock()
            with mock.patch.object(installer.specs, "parse", return_value=spec):
                installer.run("install", install, [])
            install.assert_not_called()
            self.assertEqual(
                storage.Snapshot([lower, root / "snapshot"]).objects, storage.Snapshot([lower]).objects
            )
            self.assertEqual(list((root / "snapshot/blobs").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
