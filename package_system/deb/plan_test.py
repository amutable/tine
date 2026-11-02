# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for staging repositories for APT and reading back what it would fetch.

buck test tine//package_system/deb:test
"""

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aptget
import specs

import plan
import transaction

SHA256 = "0123456789abcdef" * 4
OTHER_SHA256 = "abcdef0123456789" * 4


def packages_record(*, digest: str = SHA256, version: str = "1.0-1", filename: str = "pool/demo.deb") -> str:
    return (
        "Package: demo\n"
        f"Version: {version}\n"
        "Architecture: amd64\n"
        f"Filename: {filename}\n"
        "Size: 42\n"
        f"SHA256: {digest}\n\n"
    )


def repository(
    root: Path, rid: str, priority: int, baseurl: str | None, content: str
) -> transaction.Repository:
    path = root / rid
    (path / "dists/testing/main/binary-amd64").mkdir(parents=True)
    (path / "dists/testing/main/binary-amd64/Packages").write_text(content, encoding="utf-8")
    return transaction.Repository(rid, path, priority, baseurl)


def fetch(staged: Path, position: int, location: str, name: str, digest: str = SHA256) -> str:
    """Return one download line in the format of `apt-get --print-uris`."""
    return f"'file:{staged}/{position}/{location}' {name} 42 SHA256:{digest}\n"


class TestMain(unittest.TestCase):
    def test_accepts_the_shared_solve_spec_without_prebuilt_caches(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            spec = specs.write(
                root / "solve.spec.json",
                {
                    "arch": "amd64",
                    "cache": [],
                    "install": ["bash"],
                    "install_files": [],
                    "lower": ["lower"],
                    "repositories": [],
                },
            )
            out = root / "transaction.json"
            with mock.patch.object(plan, "solve", return_value=[]) as solve:
                plan.main(["solve", "--spec", str(spec), "--out", str(out)])

            solve.assert_called_once_with([], ["bash"], ["lower"], "amd64")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), [])

    def test_an_empty_install_file_resolves_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            (root / "requires.json").write_text("[]")
            spec = specs.write(
                root / "solve.spec.json",
                {
                    "arch": "amd64",
                    "cache": [],
                    "install": [],
                    "install_files": [str(root / "requires.json")],
                    "lower": ["lower"],
                    "repositories": [],
                },
            )
            out = root / "transaction.json"
            with mock.patch.object(plan, "solve") as solve:
                plan.main(["solve", "--spec", str(spec), "--out", str(out)])

            solve.assert_not_called()
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), [])


class TestResolvedPackages(unittest.TestCase):
    def test_maps_a_fetch_to_the_repository_apt_read_it_from(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            preferred = repository(root, "preferred", 50, None, packages_record())
            fallback = repository(root, "fallback", 99, "https://example.invalid/debian", packages_record())
            staged = root / "staged"
            # Both repositories hold the same package. The URI names position 1, which is `fallback`.
            output = "Reading package lists...\n" + fetch(
                staged, 1, "pool/main/d/demo_1.0-1%2bb1_amd64.deb", "demo_2%3a1.0-1+b1_amd64.deb"
            )
            result = plan.resolved_packages([preferred, fallback], staged, output)

        self.assertEqual(
            result,
            [
                {
                    "package_id": "demo_2:1.0-1+b1_amd64",
                    "repo": "fallback",
                    "pkg_checksum": SHA256,
                    "source": "repo",
                    "size": 42,
                    "url": "https://example.invalid/debian/pool/main/d/demo_1.0-1+b1_amd64.deb",
                }
            ],
        )

    def test_rejects_a_fetch_from_outside_the_staged_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "main", 99, "https://example.invalid", packages_record())
            output = fetch(root / "elsewhere", 0, "pool/demo.deb", "demo_1.0-1_amd64.deb")
            with self.assertRaises(SystemExit) as failure:
                plan.resolved_packages([repo], root / "staged", output)
        self.assertIn("not a pinned repository", str(failure.exception))

    def test_rejects_a_fetch_it_cannot_read(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = repository(root, "main", 99, "https://example.invalid", packages_record())
            staged = root / "staged"
            for output in (
                f"'file:{staged}/0/pool/demo.deb' demo_1.0-1_amd64.deb 42\n",
                f"'file:{staged}/0/pool/demo.deb' demo_1.0-1_amd64.deb\n",
                f"'file:{staged}/0/pool/a#b.deb' demo_1.0-1_amd64.deb 42 SHA256:{SHA256}\n",
                f"'file:{staged}/0/pool/a?b.deb' demo_1.0-1_amd64.deb 42 SHA256:{SHA256}\n",
                f"'file:{staged}/0/pool/demo.deb' demo_1.0-1_amd64.deb 42 MD5Sum:{'0' * 32}\n",
                f"'file:{staged}/0/pool/demo.deb' demo_1.0-1_amd64.deb 42 SHA256:short\n",
                f"'file:{staged}/0/pool/demo.deb' demo_1.0-1_amd64.deb -1 SHA256:{SHA256}\n",
                f"'file:{staged}/0/pool/../demo.deb' demo_1.0-1_amd64.deb 42 SHA256:{SHA256}\n".replace(
                    "..", "%2e%2e"
                ),
            ):
                with self.subTest(output=output), self.assertRaises(SystemExit):
                    plan.resolved_packages([repo], staged, output)


class TestIndexEntries(unittest.TestCase):
    def test_a_compressed_index_is_stated_under_both_names(self) -> None:
        plain = b"Package: demo\n\n"
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "Packages.gz"
            index.write_bytes(gzip.compress(plain))
            entries = plan.index_entries(index)

            # APT looks for the uncompressed name `Packages` in a Release, so the entry must exist.
            self.assertEqual([name for _, _, name in entries], ["Packages", "Packages.gz"])
            self.assertEqual(entries[0][:2], (hashlib.sha256(plain).hexdigest(), len(plain)))
            self.assertEqual(entries[1][1], index.stat().st_size)

    def test_an_uncompressed_index_is_stated_once(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            index = Path(scratch) / "Packages"
            index.write_bytes(b"Package: demo\n\n")
            self.assertEqual([name for _, _, name in plan.index_entries(index)], ["Packages"])


class TestIsolation(unittest.TestCase):
    def test_disowns_the_boxes_own_configuration_before_apt_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            env = aptget.environment(root)
            written = Path(env["APT_CONFIG"]).read_text(encoding="utf-8")
            options = aptget.options(root, root / "status", "amd64")

            self.assertIn('Dir::Etc::main "/dev/null";', written)
            self.assertIn(f'Dir::Etc::parts "{root / "nothing.d"}";', written)
            self.assertTrue((root / "nothing.d").is_dir())

            # APT resolves both settings while it loads its configuration, so a `-o` option takes
            # effect too late.
            self.assertNotIn("Dir::Etc::main", " ".join(options))
            self.assertNotIn("Dir::Etc::parts", " ".join(options))

            # APT must write its log into the scratch directory and not into the box.
            self.assertIn(f"Dir::Log={root / 'log'}", options)
            self.assertTrue((root / "log").is_dir())


class TestStageRepositories(unittest.TestCase):
    def test_labels_sources_and_translates_lower_priorities_to_higher_pins(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            preferred = repository(root, "local.repository", 50, None, packages_record())
            fallback = repository(root, "main.repository", 99, "https://example.invalid", packages_record())
            sources, preferences, staged = plan.stage_repositories(
                [preferred, fallback], root / "apt", "amd64"
            )

            self.assertEqual(len(sources.read_text(encoding="utf-8").splitlines()), 2)
            pins = preferences.read_text(encoding="utf-8")
            self.assertIn("Pin: release l=local.repository\nPin-Priority: 1003", pins)
            self.assertIn("Pin: release l=main.repository\nPin-Priority: 1002", pins)
            release = (staged / "0/Release").read_text(encoding="utf-8")
            self.assertIn("Label: local.repository\n", release)


if __name__ == "__main__":
    unittest.main()
