# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the staging of an RPM spec and its sources in an rpmbuild tree.

buck test tine//package_system/rpm:rpmbuild-test
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import rpmbuild


class Stage(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpmbuild-test.", dir="/var/tmp"))
        )
        self.spec_file = self.scratch / "example.spec"
        self.spec_file.write_text("Name: example\n")
        self.source = self.scratch / "source"
        self.source.write_text("source input\n")
        self.topdir = self.scratch / "topdir"

    def stage(self, *, spec_file: Path | None = None, sources: list[Path] | None = None) -> rpmbuild.Staged:
        parse = rpmbuild.Parse(
            spec_file=str(spec_file or self.spec_file),
            sources=[str(source) for source in (sources if sources is not None else [self.source])],
            dist=".test",
            source_date_epoch=1754006400,
            release="7",
            rpmbuild_options=[],
            in_place_rpmbuild_options=[],
            incremental=False,
        )
        return rpmbuild.stage(self.topdir, parse, None)

    def directory(self) -> Path:
        directory = self.scratch / "directory"
        (directory / "tests").mkdir(parents=True)
        (directory / "fix.patch").write_text("patch\n")
        (directory / "tests" / "test.sh").write_text("test\n")
        return directory

    def test_a_directory_contributes_its_top_level_files(self) -> None:
        # A dist-git repository contains its Source and Patch files at the top level. Stage a directory with
        # a file at the top and one in a subdirectory, and check that SOURCES contains only the top one.
        self.stage(sources=[self.directory()])
        self.assertEqual(sorted(path.name for path in (self.topdir / "SOURCES").iterdir()), ["fix.patch"])

    def test_a_symlink_at_the_top_of_a_source_directory_fails(self) -> None:
        # The copy into SOURCES follows symlinks. A symlink at the top of a fetched tree must not copy the
        # file that it points to.
        directory = self.directory()
        (directory / "secret").symlink_to(self.source)
        with self.assertRaisesRegex(SystemExit, "contains symlinks at its top level: secret"):
            self.stage(sources=[directory])

    def test_a_symlinked_source_directory_fails(self) -> None:
        # A source directory that is itself a symlink fails to stage.
        linked = self.scratch / "linked"
        linked.symlink_to(self.directory())
        with self.assertRaisesRegex(SystemExit, "the source is a symlink"):
            self.stage(sources=[linked])

    def test_a_source_directory_beneath_a_symlink_fails(self) -> None:
        # A source directory below a symlinked directory fails to stage, like a symlink of its own.
        external = self.scratch / "external"
        (external / "nested").mkdir(parents=True)
        (external / "nested" / "helper").write_text("external input\n")
        tree = self.scratch / "tree"
        tree.mkdir()
        (tree / "rpm").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "the source is a symlink"):
            self.stage(sources=[tree / "rpm" / "nested"])

    def test_a_spec_beneath_a_symlink_fails(self) -> None:
        # stage() reads the spec outside the chroot. A spec below a symlinked directory fails to stage.
        linked = self.scratch / "linked"
        linked.symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "RPM spec is a symlink"):
            self.stage(spec_file=linked / self.spec_file.name)

    def test_a_symlinked_spec_fails(self) -> None:
        # A spec that is a symlink fails to stage.
        linked = self.scratch / "linked.spec"
        linked.symlink_to(self.spec_file)
        with self.assertRaisesRegex(SystemExit, "RPM spec is a symlink"):
            self.stage(spec_file=linked)


if __name__ == "__main__":
    unittest.main()
