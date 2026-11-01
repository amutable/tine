# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the pin bumper

    buck test tine//bin:test

The GitHub API, the release index and the downloads are stubbed, so every kind of pin is tested offline.
"""

import contextlib
import http.client
import io
import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast, override
from unittest import mock

import bump


def git(repository: Path, *args: str) -> str:
    """Output of a git command in `repository`, with an identity so committing works anywhere.

    Only stdout: what git says when a fixture command fails belongs on the console.
    """
    command = ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@e.st", *args]
    run = subprocess.run(command, check=True, stdout=subprocess.PIPE, encoding="utf-8")
    return run.stdout.strip()


class GitPin(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = self.root / "origin"
        git(self.root, "init", "--quiet", "--initial-branch=main", str(self.repository))
        self.head = self.commit()

    def commit(self) -> str:
        """One more commit on the repository's only branch; its hash."""
        git(self.repository, "commit", "--quiet", "--allow-empty", "--message=a change")
        return git(self.repository, "rev-parse", "HEAD")

    def pin(self, ref: str = "main", commit: str = "0" * 40) -> dict[str, str]:
        return {"repository": str(self.repository), "ref": ref, "commit": commit}

    def test_takes_the_commit_the_ref_points_at(self) -> None:
        pin = self.pin()
        self.assertEqual(bump._bump_ref("hello", pin), ("0" * 12, self.head[:12]))
        self.assertEqual(pin["commit"], self.head)

    def test_leaves_a_pin_that_has_not_moved(self) -> None:
        pin = self.pin(commit=self.head)
        self.assertIsNone(bump._bump_ref("hello", pin))
        self.assertEqual(pin["commit"], self.head)

    def test_follows_the_ref_rather_than_the_default_branch(self) -> None:
        git(self.repository, "branch", "release")
        moved = self.commit()
        release, main = self.pin(ref="release"), self.pin()
        bump._bump_ref("hello", release)
        bump._bump_ref("hello", main)
        self.assertEqual((release["commit"], main["commit"]), (self.head, moved))

    def test_rejects_a_ref_that_matches_twice(self) -> None:
        git(self.repository, "tag", "main")
        with self.assertRaisesRegex(ValueError, "main matches 2 refs"):
            bump._bump_ref("hello", self.pin())

    def test_fails_on_a_ref_that_is_gone(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            bump._bump_ref("hello", self.pin(ref="never-existed"))

    def test_ignores_the_configuration_of_the_checkout_it_runs_in(self) -> None:
        """A pin resolves through the credentials that clone it, not the current checkout's.

        On a runner those differ: actions/checkout persists one in the local configuration that
        reaches the repository being bumped and nothing else. Stand in for it with a local rewrite
        sending the fixture somewhere there is no repository at all.
        """
        enclosing = self.root / "enclosing"
        git(self.root, "init", "--quiet", str(enclosing))
        git(enclosing, "config", f"url.{self.root / 'gone'}.insteadOf", str(self.repository))
        with contextlib.chdir(enclosing):
            self.assertEqual(bump._bump_ref("hello", self.pin()), ("0" * 12, self.head[:12]))

    def test_writes_the_pin_back_to_the_data_file(self) -> None:
        data = self.root / "pins.json"
        data.write_text(json.dumps({"hello": self.pin()}))
        bump.main(data, None, ["--all"])
        self.assertEqual(json.loads(data.read_text())["hello"]["commit"], self.head)


class ReleasePin(unittest.TestCase):
    """The release path with GitHub stubbed out, so only tine's own decisions are under test.

    A release pin is chosen by two things: which asset in the release succeeds the pinned one, and
    (for CPython alone) the minor that the ty configuration pins the interpreter to.
    """

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ty_config = self.root / "ty.toml"
        self.ty_config.write_text('[environment]\npython-version = "3.14"\n')
        self.data = self.root / "tine.lock.json"

    @staticmethod
    def asset(name: str, size: int = 1) -> dict[str, object]:
        # The digest only has to be well-formed: what it hashes is never downloaded here.
        return {"name": name, "size": size, "digest": f"sha256:{'ab' * 32}"}

    @staticmethod
    def syft() -> dict[str, object]:
        """One pin whose artifact embeds the release, and which reads no ty configuration."""
        return {
            "syft": {
                "repository": "anchore/syft",
                "release": "v1.50.0",
                "platforms": {"x86_64": {"artifact": "syft_1.50.0_linux_amd64.tar.gz"}},
            },
        }

    def bump(
        self, pins: Mapping[str, object], release: Mapping[str, object], *, ty_config: bool = True
    ) -> dict[str, Any]:
        """Bump `pins` against one stubbed release; the rewritten file.

        `ty_config` off stands in for a consuming project without a ty configuration.
        """
        self.data.write_text(json.dumps(pins, indent=2) + "\n")
        with mock.patch.object(bump, "_github_json", return_value=release):
            bump.main(self.data, self.ty_config if ty_config else None, ["--all"])
        return cast(dict[str, Any], json.loads(self.data.read_text()))

    def test_reads_the_pinned_python_minor_from_the_ty_config(self) -> None:
        """A CPython asset is matched by minor, so it comes from the file ty resolves imports with.

        Reading it anywhere else would pin an interpreter ty does not check against.
        """
        self.assertEqual(bump._python_minor(self.ty_config), "3.14")

    def test_fails_on_a_ty_config_pinning_no_python(self) -> None:
        self.ty_config.write_text('[rules]\nall = "error"\n')
        with self.assertRaisesRegex(ValueError, "environment table in .*ty.toml"):
            bump._python_minor(self.ty_config)

    def test_reads_pyproject_configuration_for_consuming_projects(self) -> None:
        path = self.root / "pyproject.toml"
        path.write_text('[tool.ty.environment]\npython-version = "3.13"\n')
        self.assertEqual(bump._python_minor(path), "3.13")

    def test_fails_on_a_cpython_pin_with_no_ty_config(self) -> None:
        """Only a CPython pin needs a ty configuration, so bump fails on that pin alone."""
        with self.assertRaisesRegex(ValueError, "needs a ty.toml"):
            bump._python_minor(None)

    def test_follows_a_cpython_asset_across_patch_and_date(self) -> None:
        pins = {
            "python3": {
                "repository": "astral-sh/python-build-standalone",
                "release": "20260805",
                "platforms": {
                    "x86_64": {
                        "artifact": "cpython-3.14.7+20260805-x86_64-unknown-linux-gnu-install_only.tar.gz",
                        "sha256": "0" * 64,
                        "size": 1,
                        "strip_prefix": "python",
                    },
                },
            },
        }
        release = {
            "tag_name": "20260814",
            "assets": [
                # A newer patch and date on the pinned platform, plus the near misses around it:
                # another minor, another platform, and an earlier patch in the same release.
                self.asset("cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz", 9),
                self.asset("cpython-3.14.6+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.15.0+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.14.8+20260814-aarch64-unknown-linux-gnu-install_only.tar.gz"),
                self.asset("cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-debug-full.tar.gz"),
            ],
        }
        entry = self.bump(pins, release)["python3"]["platforms"]["x86_64"]
        self.assertEqual(
            entry,
            {
                "artifact": "cpython-3.14.8+20260814-x86_64-unknown-linux-gnu-install_only.tar.gz",
                "sha256": "ab" * 32,
                "size": 9,
                "strip_prefix": "python",
            },
        )

    def test_follows_an_asset_named_after_the_release(self) -> None:
        """syft embeds the tag without its leading "v", so both spellings have to wildcard."""
        release = {
            "tag_name": "v1.51.0",
            "assets": [
                self.asset("syft_1.51.0_linux_amd64.tar.gz"),
                self.asset("syft_1.51.0_linux_arm64.tar.gz"),
            ],
        }
        data = self.bump(self.syft(), release)
        self.assertEqual(data["syft"]["release"], "v1.51.0")
        self.assertEqual(data["syft"]["platforms"]["x86_64"]["artifact"], "syft_1.51.0_linux_amd64.tar.gz")

    def test_moves_a_versioned_strip_prefix_with_the_release(self) -> None:
        """gh names the top directory of its archive after the tag without its leading "v"."""
        pins = {
            "gh": {
                "repository": "cli/cli",
                "release": "v2.103.0",
                "platforms": {
                    "x86_64": {
                        "artifact": "gh_2.103.0_linux_amd64.tar.gz",
                        "sha256": "0" * 64,
                        "size": 1,
                        "strip_prefix": "gh_2.103.0_linux_amd64",
                    },
                },
            },
        }
        release = {"tag_name": "v2.103.3", "assets": [self.asset("gh_2.103.3_linux_amd64.tar.gz")]}
        entry = self.bump(pins, release)["gh"]["platforms"]["x86_64"]
        self.assertEqual(entry["artifact"], "gh_2.103.3_linux_amd64.tar.gz")
        self.assertEqual(entry["strip_prefix"], "gh_2.103.3_linux_amd64")

    def test_bumps_a_pin_needing_no_python_with_no_ty_config(self) -> None:
        """A consuming project can have no ty configuration."""
        release = {"tag_name": "v1.51.0", "assets": [self.asset("syft_1.51.0_linux_amd64.tar.gz")]}
        data = self.bump(self.syft(), release, ty_config=False)
        self.assertEqual(data["syft"]["release"], "v1.51.0")

    def test_leaves_a_release_that_has_not_moved(self) -> None:
        pins = {
            "cargo-auditable": {
                "repository": "rust-secure-code/cargo-auditable",
                "release": "v0.7.5",
                "platforms": {
                    "x86_64": {
                        "artifact": "cargo-auditable-x86_64-unknown-linux-musl.tgz",
                        "sha256": "ab" * 32,
                        "size": 1,
                    },
                },
            },
        }
        release = {
            "tag_name": "v0.7.5",
            "assets": [self.asset("cargo-auditable-x86_64-unknown-linux-musl.tgz")],
        }
        original = json.dumps(pins, indent=2) + "\n"
        self.assertEqual(self.bump(pins, release), json.loads(original))
        self.assertEqual(self.data.read_text(), original)

    def test_fails_on_a_release_missing_the_pinned_platform(self) -> None:
        release = {"tag_name": "v1.51.0", "assets": [self.asset("syft_1.51.0_linux_arm64.tar.gz")]}
        with self.assertRaises(SystemExit):
            self.bump(self.syft(), release)


class UrlPin(unittest.TestCase):
    """Tests for pins that download from a plain URL, with fixtures for the index and the downloads."""

    INDEX = json.dumps(
        [{"version": version} for version in ("go1.27.10", "go1.27.9", "go1.27rc1", "go1.26.3")]
    )

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "tine.lock.json"
        self.downloads: list[str] = []

    def download(self, url: str) -> tuple[str, int]:
        self.downloads.append(url)
        return "cd" * 32, 7

    @staticmethod
    def pin(release: str, **entry: object) -> dict[str, object]:
        return {
            "url": "https://go.dev/dl",
            "release": release,
            "releases": {"url": "https://go.dev/dl/?mode=json&include=all", "pattern": "go1\\.27\\.[0-9]+"},
            "platforms": {
                "x86_64": {"artifact": f"{release}.linux-amd64.tar.gz", "strip_prefix": "go", **entry}
            },
        }

    def bump(self, pin: dict[str, object], index: str = INDEX) -> dict[str, Any]:
        self.data.write_text(json.dumps({"go": pin}, indent=2) + "\n")
        with (
            mock.patch.object(bump, "_read", return_value=index) as read,
            mock.patch.object(bump, "_download", side_effect=self.download),
        ):
            bump.main(self.data, None, ["--all"])
        self.reads = [call.args[0] for call in read.call_args_list]
        return cast(dict[str, Any], json.loads(self.data.read_text()))["go"]

    def test_takes_the_highest_release_that_matches_the_pattern(self) -> None:
        pin = self.bump(self.pin("go1.27.2", sha256="ab" * 32, size=1))
        self.assertEqual(self.reads, ["https://go.dev/dl/?mode=json&include=all"])
        self.assertEqual(pin["release"], "go1.27.10")
        self.assertEqual(
            pin["platforms"]["x86_64"],
            {
                "artifact": "go1.27.10.linux-amd64.tar.gz",
                "strip_prefix": "go",
                "sha256": "cd" * 32,
                "size": 7,
            },
        )
        self.assertEqual(self.downloads, ["https://go.dev/dl/go1.27.10.linux-amd64.tar.gz"])

    def test_leaves_a_release_that_has_not_moved(self) -> None:
        pin = self.pin("go1.27.10", sha256="ab" * 32, size=1)
        self.assertEqual(self.bump(pin), pin)
        self.assertEqual(self.downloads, [])

    def test_fills_in_the_digests_of_a_pin_without_releases(self) -> None:
        pin = self.pin("go1.27.2")
        del pin["releases"]
        entry = self.bump(pin)["platforms"]["x86_64"]
        self.assertEqual(self.reads, [])
        self.assertEqual(
            (entry["artifact"], entry["sha256"], entry["size"]),
            ("go1.27.2.linux-amd64.tar.gz", "cd" * 32, 7),
        )

    def test_fails_on_an_index_without_a_matching_release(self) -> None:
        pin = self.pin("go1.27.2")
        pin["releases"] = {"url": "https://go.dev/dl/?mode=json&include=all", "pattern": "go1\\.28\\.[0-9]+"}
        with self.assertRaisesRegex(SystemExit, "contains no release that matches"):
            self.bump(pin)

    def test_moves_a_versioned_strip_prefix_with_the_release(self) -> None:
        pin: dict[str, object] = {
            "url": "https://nodejs.org/dist",
            "release": "v22.11.0",
            "releases": {"url": "https://nodejs.org/dist/index.json", "pattern": "v22\\.[0-9]+\\.[0-9]+"},
            "platforms": {
                "x86_64": {
                    "artifact": "v22.11.0/node-v22.11.0-linux-x64.tar.xz",
                    "sha256": "ab" * 32,
                    "size": 1,
                    "strip_prefix": "node-v22.11.0-linux-x64",
                }
            },
        }
        entry = self.bump(pin, index="v22.11.0 v22.12.0")["platforms"]["x86_64"]
        self.assertEqual(entry["artifact"], "v22.12.0/node-v22.12.0-linux-x64.tar.xz")
        self.assertEqual(entry["strip_prefix"], "node-v22.12.0-linux-x64")

    def test_moves_an_artifact_named_after_the_release_without_its_v(self) -> None:
        pin = self.pin("v1.2.3", sha256="ab" * 32, size=1)
        pin["releases"] = {"url": "https://example.com/releases", "pattern": "v1\\.2\\.[0-9]+"}
        pin["platforms"] = {
            "x86_64": {"artifact": "foo-1.2.3-linux-amd64.tar.gz", "sha256": "ab" * 32, "size": 1}
        }
        entry = self.bump(pin, index="v1.2.3 v1.2.4")["platforms"]["x86_64"]
        self.assertEqual((entry["artifact"], entry["sha256"]), ("foo-1.2.4-linux-amd64.tar.gz", "cd" * 32))

    def test_downloads_an_unversioned_artifact_again_when_the_release_moves(self) -> None:
        pin = self.pin("1.0.0")
        pin["releases"] = {"url": "https://example.com/releases", "pattern": "[0-9]+\\.[0-9]+\\.[0-9]+"}
        pin["platforms"] = {
            "x86_64": {"artifact": "tool-linux-amd64.tar.gz", "sha256": "ab" * 32, "size": 1}
        }
        entry = self.bump(pin, index="1.0.0 1.1.0")["platforms"]["x86_64"]
        self.assertEqual(self.downloads, ["https://go.dev/dl/tool-linux-amd64.tar.gz"])
        self.assertEqual(entry["sha256"], "cd" * 32)

    def test_replaces_only_whole_releases(self) -> None:
        self.assertEqual(
            bump._replace_release("tool-4-linux-amd64.tar.gz", "4", "5"), "tool-5-linux-amd64.tar.gz"
        )
        self.assertEqual(bump._replace_release("tool-1.2.3.tar.gz", "1.2", "1.3"), "tool-1.2.3.tar.gz")
        self.assertEqual(
            bump._replace_release("go1.27.linux-arm64.tar.gz", "go1.27", "go1.27.2"),
            "go1.27.2.linux-arm64.tar.gz",
        )

    def test_two_spellings_of_one_version_pick_the_same_release(self) -> None:
        pin = self.pin("v1.2.3", sha256="ab" * 32, size=1)
        pin["releases"] = {"url": "https://example.com/releases", "pattern": "v?1\\.2\\.[0-9]+"}
        for index in ("v1.2.4 1.2.4", "1.2.4 v1.2.4"):
            with self.subTest(index=index):
                self.assertEqual(self.bump(pin, index=index)["release"], "v1.2.4")

    def test_says_that_it_only_pinned_the_digests(self) -> None:
        pin = self.pin("go1.27.2")
        del pin["releases"]
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.bump(pin)
        self.assertEqual(printed.getvalue(), "go: pinned the digests of go1.27.2\n")


class Download(unittest.TestCase):
    """Tests for the transfer checks, with a fixture in place of the HTTP response."""

    @staticmethod
    def response(body: bytes, length: int | None = None, error: Exception | None = None) -> mock.MagicMock:
        stream = io.BytesIO(body)

        def read(amount: int) -> bytes:
            chunk = stream.read(amount)
            if not chunk and error is not None:
                raise error
            return chunk

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {} if length is None else {"Content-Length": str(length)}
        response.read.side_effect = read
        return response

    def test_hashes_the_whole_body(self) -> None:
        with mock.patch.object(bump, "urlopen", return_value=self.response(b"abc", 3)):
            self.assertEqual(
                bump._download("https://example.com/a"), (bump.hashlib.sha256(b"abc").hexdigest(), 3)
            )

    def test_fails_on_a_body_shorter_than_its_length(self) -> None:
        with (
            mock.patch.object(bump, "urlopen", return_value=self.response(b"abc", 10)),
            self.assertRaisesRegex(ValueError, "https://example.com/a: received 3 of 10 bytes"),
        ):
            bump._download("https://example.com/a")

    def test_names_the_url_of_a_broken_transfer(self) -> None:
        response = self.response(b"", error=http.client.IncompleteRead(b""))
        with (
            mock.patch.object(bump, "urlopen", return_value=response),
            self.assertRaisesRegex(ValueError, "^https://example.com/index: IncompleteRead"),
        ):
            bump._read("https://example.com/index")


if __name__ == "__main__":
    unittest.main()
