# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the checkout of a source checkout's pinned packaging.

buck test tine//package:packaging-test
"""

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import packaging


def git(directory: Path, *args: str) -> str:
    command = ["git", "-C", str(directory), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    return subprocess.run([*command, *args], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


class CheckOut(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="packaging-test.", dir="/var/tmp"))
        )
        self.enterContext(
            mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        )
        self.upstream = self.scratch / "rpms" / "hello"
        self.upstream.mkdir(parents=True)
        git(self.upstream, "init", "--quiet", "--initial-branch=rawhide")
        (self.upstream / "hello.spec").write_text("Name: hello\n")
        (self.upstream / "fix.patch").write_text("first\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "--message", "first")
        self.first = git(self.upstream, "rev-parse", "HEAD")
        (self.upstream / "fix.patch").write_text("second\n")
        git(self.upstream, "commit", "--quiet", "--all", "--message", "second")
        git(self.upstream, "switch", "--quiet", "--create", "f42", self.first)
        (self.upstream / "other.patch").write_text("branch\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "--message", "f42")
        self.other = git(self.upstream, "rev-parse", "HEAD")
        self.out = self.scratch / "packaging"

    def check_out(self, pin: object) -> None:
        self.check_out_pins({"fedora": pin, "centos": self.pin(self.other) | {"branch": "c10s"}})

    def check_out_pins(self, pins: object, subdir: str | None = None) -> None:
        path = self.scratch / "checkout" / "packaging.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pins))
        packaging.check_out(path, "fedora", self.out, subdir)

    def pin(self, commit: str) -> dict[str, str]:
        return {"url": self.upstream.as_uri(), "branch": "rawhide", "commit": commit}

    def test_the_pinned_commit_is_checked_out_without_its_repository(self) -> None:
        self.check_out(self.pin(self.first))
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ["fix.patch", "hello.spec"])
        self.assertEqual((self.out / "fix.patch").read_text(), "first\n")

    def test_a_commit_must_be_spelled_out_in_full(self) -> None:
        for commit in (self.first[:12], self.first.upper(), "main"):
            with self.subTest(commit=commit), self.assertRaisesRegex(SystemExit, "40 lowercase hex digits"):
                self.check_out(self.pin(commit))

    def test_a_malformed_pin_names_its_file(self) -> None:
        for pin in (
            [],
            {"url": self.upstream.as_uri(), "commit": self.first},
            self.pin(self.first) | {"ref": ""},
        ):
            with (
                self.subTest(pin=pin),
                self.assertRaisesRegex(SystemExit, "packaging.json: the packaging pin of fedora must"),
            ):
                self.check_out(pin)

    def test_a_pin_file_must_map_distros_to_pins(self) -> None:
        with self.assertRaisesRegex(SystemExit, "packaging.json: the packaging pin must be a JSON object"):
            self.check_out_pins([self.pin(self.first)])

    def test_a_pin_file_without_the_distro_fails(self) -> None:
        with self.assertRaisesRegex(SystemExit, "packaging.json: the packaging pin has no entry for fedora"):
            self.check_out_pins({"centos": self.pin(self.first)})

    def test_the_commit_must_be_on_the_branch(self) -> None:
        with self.assertRaisesRegex(SystemExit, f"commit {self.other} is not on branch rawhide"):
            self.check_out(self.pin(self.other))

    def test_a_relative_url_names_a_repository_in_the_working_directory(self) -> None:
        with contextlib.chdir(self.scratch):
            self.check_out(self.pin(self.first) | {"url": "rpms/hello"})
        self.assertEqual((self.out / "fix.patch").read_text(), "first\n")

    def test_a_symlink_at_the_top_of_the_tree_fails(self) -> None:
        git(self.upstream, "switch", "--quiet", "rawhide")
        (self.upstream / "escape.patch").symlink_to("/etc/os-release")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "--message", "symlink")
        with self.assertRaisesRegex(SystemExit, "contains symlinks at the top of its tree: escape.patch"):
            self.check_out(self.pin(git(self.upstream, "rev-parse", "HEAD")))

    def test_a_subdirectory_is_checked_out_alone(self) -> None:
        git(self.upstream, "switch", "--quiet", "rawhide")
        (self.upstream / "debian").mkdir()
        (self.upstream / "debian" / "rules").write_text("rules\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "--message", "debian")
        pin = self.pin(git(self.upstream, "rev-parse", "HEAD"))
        with contextlib.chdir(self.scratch):
            self.check_out_pins({"fedora": pin | {"url": "rpms/hello"}}, subdir="debian")
        self.assertEqual([path.name for path in self.out.iterdir()], ["debian"])
        self.assertEqual((self.out / "debian" / "rules").read_text(), "rules\n")

    def test_a_missing_subdirectory_fails(self) -> None:
        with self.assertRaisesRegex(SystemExit, "contains no directory debian"):
            self.check_out_pins({"fedora": self.pin(self.first)}, subdir="debian")

    def test_a_missing_branch_names_the_pin(self) -> None:
        with self.assertRaisesRegex(SystemExit, "packaging.json: cannot fetch branch f41"):
            self.check_out(self.pin(self.first) | {"branch": "f41"})


if __name__ == "__main__":
    unittest.main()
