# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Debian installer.

buck test tine//package_system/deb:test
"""

import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import debfile
from debfile_test import add
from extract_test import deb

import install


class TestPathRules(unittest.TestCase):
    def test_a_later_rule_carves_the_licenses_back_out(self) -> None:
        self.assertEqual(install.path_rules(docs=True, langs=[]), [])
        rules = install.path_rules(docs=False, langs=["en"])
        self.assertIn((False, "usr/share/doc/*"), rules)
        self.assertLess(rules.index((False, "usr/share/doc/*")), rules.index((True, install.COPYRIGHT)))
        self.assertLess(
            rules.index((False, "usr/share/locale/*")), rules.index((True, "usr/share/locale/en/*"))
        )


class TestInstall(unittest.TestCase):
    def test_apt_is_told_to_refuse_a_removal(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(install.aptget, "run") as run:
            root = Path(scratch)
            closure = root / "closure"
            closure.mkdir()
            (closure / "demo_1_amd64.deb").touch()
            installroot = root / "root"
            (installroot / debfile.ADMINDIR).mkdir(parents=True)
            (installroot / debfile.ADMINDIR / "status").write_text("Package: bash\n")

            install.install(closure, installroot, arch="amd64", langs=[], docs=True)

        arguments = run.call_args.args[0]
        self.assertLess(arguments.index("--no-remove"), arguments.index("install"))


class TestLayDown(unittest.TestCase):
    def test_links_land_before_a_package_that_ships_a_path_under_one(self) -> None:
        def links(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/lib/os-release", b"ID=debian\n")
            link = tarfile.TarInfo("./lib")
            link.type, link.linkname = tarfile.SYMTYPE, "usr/lib"
            archive.addfile(link)

        def vendor(archive: tarfile.TarFile) -> None:
            add(archive, "./lib/systemd/system/vendor.service", b"[Unit]\n")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            packages = [
                deb(root, "aaa-vendor_1_amd64.deb", vendor),
                deb(root, "base-files_14_amd64.deb", links),
            ]
            install.lay_down(packages, root / "root", [])

            self.assertTrue((root / "root/lib").is_symlink())
            self.assertTrue((root / "root/usr/lib/systemd/system/vendor.service").is_file())

    def test_refuses_a_link_a_directory_is_in_the_place_of(self) -> None:
        def links(archive: tarfile.TarFile) -> None:
            link = tarfile.TarInfo("./lib")
            link.type, link.linkname = tarfile.SYMTYPE, "usr/lib"
            archive.addfile(link)

        def vendor(archive: tarfile.TarFile) -> None:
            add(archive, "./lib/systemd/system/vendor.service", b"[Unit]\n")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            packages = [deb(root, "aaa-vendor_1_amd64.deb", vendor), deb(root, "links_1_amd64.deb", links)]
            with self.assertRaises(SystemExit) as failure:
                install.lay_down(packages, root / "root", [])
        self.assertIn("a directory is in its place", str(failure.exception))

    def test_drops_the_excluded_trees_whole(self) -> None:
        def build(archive: tarfile.TarFile) -> None:
            add(archive, "./usr/bin/demo", b"x")
            add(archive, "./usr/share/doc/demo/copyright", b"x")
            add(archive, "./usr/share/man/man1/demo.1", b"x")
            add(archive, "./usr/share/locale/fr/LC_MESSAGES/demo.mo", b"x")

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            excluded = install.excluded_trees(docs=False, langs=[])
            install.lay_down([deb(root, "demo_1_amd64.deb", build)], root / "root", excluded)

            self.assertTrue((root / "root/usr/bin/demo").is_file())
            # `lay_down()` removes the whole tree, the licenses included. dpkg restores the
            # licenses when it unpacks the packages.
            self.assertFalse((root / "root/usr/share/doc").exists())
            self.assertFalse((root / "root/usr/share/man").exists())
            self.assertTrue((root / "root/usr/share/locale/fr/LC_MESSAGES/demo.mo").is_file())


class TestEmptyClosure(unittest.TestCase):
    def _closure(self, root: Path) -> Path:
        empty = root / "closure"
        empty.mkdir()
        return empty

    def test_refuses_a_root_with_nothing_installed_underneath(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            installroot = root / "root"
            installroot.mkdir()
            with self.assertRaises(SystemExit):
                install.install(self._closure(root), installroot, arch="amd64", langs=[], docs=True)

    def test_an_empty_closure_leaves_the_database_alone(self) -> None:
        # A closure without packages has nothing for dpkg to do. Install an empty closure over a root
        # with a database, and check that install() returns without touching the database.
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            installroot = root / "root"
            admin = installroot / debfile.ADMINDIR
            admin.mkdir(parents=True)
            (admin / "status").write_text("Package: bash\n")
            stamped = (admin / "status").stat().st_mtime_ns

            install.install(self._closure(root), installroot, arch="amd64", langs=[], docs=True)

            self.assertEqual((admin / "status").stat().st_mtime_ns, stamped)
            self.assertFalse((admin / "updates").exists())


if __name__ == "__main__":
    unittest.main()
