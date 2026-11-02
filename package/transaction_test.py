# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for what a transaction records beyond its packages."""

import json
import tempfile
import unittest
from pathlib import Path

import snapshotter
import transaction

FILES = [{"out": "InRelease", "sha256": "ab" * 32, "size": 3, "url": "https://mirror.invalid/InRelease"}]


class TestInstallSpecs(unittest.TestCase):
    def test_files_extend_the_listed_specs_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            path = Path(scratch) / "requires.json"
            path.write_text(json.dumps(["meson", "gcc", "jq"]))
            self.assertEqual(
                transaction.install_specs(["gcc", "make"], [str(path)]), ["gcc", "make", "meson", "jq"]
            )

    def test_an_empty_file_is_a_solve_with_nothing_to_install(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            path = Path(scratch) / "requires.json"
            path.write_text("[]")
            self.assertEqual(transaction.install_specs([], [str(path)]), [])

    def test_a_solve_without_specs_or_files_fails(self) -> None:
        with self.assertRaisesRegex(SystemExit, "at least one install spec"):
            transaction.install_specs([], [])


class TestWrite(unittest.TestCase):
    def test_records_the_metadata_a_vouching_repository_was_resolved_against(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in ("vouching", "also", "plain", "local", "rolling"):
                (root / name).mkdir()
            manifest = json.dumps({"files": FILES, "pinned_at": "2026-01-03T00:00:00Z"})
            (root / "vouching" / snapshotter.MANIFEST).write_text(manifest)
            (root / "also" / snapshotter.MANIFEST).write_text(manifest)
            (root / "local" / snapshotter.MANIFEST).write_text(manifest)
            rolling = json.dumps({"files": FILES, "pinned_at": None})
            (root / "rolling" / snapshotter.MANIFEST).write_text(rolling)
            repositories = [
                transaction.Repository("plain", root / "plain", 1, "https://mirror.invalid/plain"),
                transaction.Repository("vouching", root / "vouching", 1, "https://mirror.invalid/v"),
                transaction.Repository("also", root / "also", 1, "https://mirror.invalid/a"),
                transaction.Repository("local", root / "local", 1, None),
                transaction.Repository("rolling", root / "rolling", 1, "https://mirror.invalid/r"),
            ]
            package = transaction.entry("pkg", repositories[0], "cd" * 32, "pool/pkg.deb", size=1)
            out = root / "transaction.json"
            transaction.write(out, [package], repositories)
            written = json.loads(out.read_text())

        # Packages first, then one metadata entry per pinned vouching remote repository in id order,
        # and nothing for a repository whose packages vouch for themselves, one this build produced,
        # or a rolling one whose metadata URLs do not outlive the mirror's next advance.
        self.assertEqual(written[0]["source"], "repo")
        self.assertEqual(
            written[1:],
            [
                {"repo": repo, "source": "metadata", "files": FILES, "pinned_at": "2026-01-03T00:00:00Z"}
                for repo in ("also", "vouching")
            ],
        )


if __name__ == "__main__":
    unittest.main()
