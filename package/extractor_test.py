# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bootstrap extraction of package archives.

buck test tine//package:test
"""

import tempfile
import unittest
from pathlib import Path

import specs

import extractor


class TestRun(unittest.TestCase):
    def test_writes_files_that_the_owner_cannot_read(self) -> None:
        # Packages install files such as /etc/gshadow with mode 0000, and the extract action does
        # not run as root. An unpack callback writes such a file and a directory without write
        # permission. The Buck tree must contain both files.
        def unpack(package: Path, tree: Path) -> int:
            (tree / "etc").mkdir()
            (tree / "etc/gshadow").write_text(package.name)
            (tree / "etc/gshadow").chmod(0)
            (tree / "usr").mkdir()
            (tree / "usr/file").write_text("file")
            (tree / "usr").chmod(0o500)
            return 4

        with tempfile.TemporaryDirectory(prefix="extractor-test.", dir="/var/tmp") as scratch_dir:
            scratch = Path(scratch_dir)
            package, out = scratch / "package.rpm", scratch / "out"
            package.write_text("")
            spec = specs.write(scratch / "spec.json", {"out": str(out), "packages": [str(package)]})

            extractor.run("extract", unpack, ["--spec", str(spec)])

            self.assertEqual((out / "etc/gshadow").read_text(), "package.rpm")
            self.assertEqual((out / "usr/file").read_text(), "file")

    def test_refuses_no_packages(self) -> None:
        # A directory without packages gives the callback nothing to unpack, which must fail.
        with tempfile.TemporaryDirectory(prefix="extractor-test.", dir="/var/tmp") as scratch_dir:
            scratch = Path(scratch_dir)
            (scratch / "packages").mkdir()
            spec = specs.write(
                scratch / "spec.json", {"out": str(scratch / "out"), "packages": [str(scratch / "packages")]}
            )
            with self.assertRaises(SystemExit):
                extractor.run("extract", lambda package, tree: 0, ["--spec", str(spec)])


if __name__ == "__main__":
    unittest.main()
