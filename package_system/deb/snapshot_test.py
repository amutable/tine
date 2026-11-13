# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for pinning a Debian suite's index and inventory.

buck test tine//package_system/deb:test
"""

import gzip
import tempfile
import unittest
from pathlib import Path

import snapshot

BASEURL = "https://snapshot.invalid/archive/debian/20260701T000000Z"
BASE = f"{BASEURL}/dists/trixie"
XZ = "3ab4e811cf4f3e5a335d382c58cc19d85f1abe7a4ef4689160ca1f637fa0e9b3"
PLAIN = "be70297f6ea499e8ef0bd93906719298c795bc3dc4ce72560f13e6c9836bfedb"
DEB = "03571d298ed24c615641d54dec4eb023ef5710a2b8cb9304acb66e372cab9c46"


def release(*, by_hash: str = "yes", entries: str | None = None) -> dict[str, str]:
    stated = entries
    if stated is None:
        stated = (
            f"\n{PLAIN} 56561288 main/binary-amd64/Packages\n{XZ}  9672648 main/binary-amd64/Packages.xz"
        )
    return {
        "architectures": "all amd64 arm64",
        "components": "main contrib non-free",
        "acquire-by-hash": by_hash,
        "sha256": stated,
    }


def release_flat(*, by_hash: str = "yes", entries: str | None = None) -> dict[str, str]:
    stated = entries
    if stated is None:
        stated = f"\n{PLAIN} 65651288 Packages\n{XZ}  9672648 Packages.xz"
    return {
        "architectures": "all amd64 arm64",
        "acquire-by-hash": by_hash,
        "sha256": stated,
    }


def packages_record(*, name: str = "bash", digest: str = DEB, filename: str | None = None) -> str:
    location = filename or f"pool/main/b/{name}/{name}_5.2.37-2_amd64.deb"
    return (
        f"Package: {name}\n"
        "Version: 5.2.37-2\n"
        "Architecture: amd64\n"
        f"Filename: {location}\n"
        "Size: 1501148\n"
        f"SHA256: {digest}\n\n"
    )


class TestPackageIndex(unittest.TestCase):
    def test_prefers_the_smallest_form_and_pins_it_by_hash(self) -> None:
        stream = snapshot.package_index("main", release(), BASE, "trixie", "main", "amd64")

        # The pinned index keeps the path of the mirror, because verify.py looks the path up in
        # the signed Release.
        self.assertEqual(stream["out"], "dists/trixie/main/binary-amd64/Packages.xz")
        self.assertEqual(stream["sha256"], XZ)
        self.assertEqual(stream["size"], 9672648)
        self.assertEqual(stream["url"], f"{BASE}/main/binary-amd64/by-hash/SHA256/{XZ}")

    def test_tolerates_an_empty_index_it_is_not_pinning(self) -> None:
        # Debian publishes a `Contents-udeb-all` of zero bytes in every component.
        entries = f"\n{PLAIN} 0 main/Contents-udeb-all\n{XZ}  9672648 main/binary-amd64/Packages.xz"
        stream = snapshot.package_index("main", release(entries=entries), BASE, "trixie", "main", "amd64")
        self.assertEqual(stream["size"], 9672648)

    def test_falls_back_to_the_stated_path_without_by_hash(self) -> None:
        stream = snapshot.package_index("main", release(by_hash="no"), BASE, "trixie", "main", "amd64")
        self.assertEqual(stream["url"], f"{BASE}/main/binary-amd64/Packages.xz")

    def test_rejects_a_component_or_architecture_the_release_does_not_index(self) -> None:
        for component, arch in (("contrib", "amd64"), ("main", "arm64")):
            with self.subTest(component=component, arch=arch), self.assertRaises(SystemExit):
                snapshot.package_index("main", release(), BASE, "trixie", component, arch)

    def test_rejects_a_malformed_or_escaping_release_entry(self) -> None:
        for entries in (
            f"\n{XZ} 9672648",
            f"\n{XZ} 0 main/binary-amd64/Packages.xz",
            f"\n{XZ} 9672648 ../../etc/shadow",
            "\nnotadigest 9672648 main/binary-amd64/Packages.xz",
            f"\n{XZ} 9672648 main/binary-amd64/Packages.xz\n{PLAIN} 1 main/binary-amd64/Packages.xz",
        ):
            with self.subTest(entries=entries), self.assertRaises(SystemExit):
                snapshot.package_index("main", release(entries=entries), BASE, "trixie", "main", "amd64")

    def test_tolerates_flat_release_with_no_suite(self) -> None:
        stream = snapshot.package_index("main", release_flat(), BASE, "", "", "amd64")
        self.assertEqual(stream["out"], "Packages.xz")
        self.assertEqual(stream["sha256"], XZ)
        self.assertEqual(stream["size"], 9672648)
        self.assertEqual(stream["url"], f"{BASE}/by-hash/SHA256/{XZ}")


class TestRepositoryBase(unittest.TestCase):
    def test_standard_repo_preserves_suite(self) -> None:
        base, suite = snapshot._repository_base("main", BASEURL, "trixie")
        self.assertEqual(base, f"{BASEURL}/dists/trixie")
        self.assertEqual(suite, "trixie")

    def test_flat_repo_removes_suite(self) -> None:
        base, suite = snapshot._repository_base("main", BASEURL, "not/really/a/suite/")
        self.assertEqual(base, f"{BASEURL}/not/really/a/suite")
        self.assertEqual(suite, "")

    def test_flat_repo_tolerates_slash_only(self) -> None:
        base, suite = snapshot._repository_base("main", BASEURL, "/")
        self.assertEqual(base, BASEURL)
        self.assertEqual(suite, "")


class TestIndexedTogether(unittest.TestCase):
    def test_refuses_a_suite_that_indexes_architecture_all_apart(self) -> None:
        snapshot._indexed_together("main", {}, ["amd64", "arm64"])
        together = {"no-support-for-architecture-all": "Packages"}
        snapshot._indexed_together("main", together, ["all", "amd64"])
        with self.assertRaises(SystemExit):
            snapshot._indexed_together("main", {}, ["all", "amd64"])


class TestInventory(unittest.TestCase):
    def _index(self, root: Path, content: str, *, compress: bool = False) -> Path:
        index = root / ("Packages.gz" if compress else "Packages")
        raw = content.encode()
        index.write_bytes(gzip.compress(raw) if compress else raw)
        return index

    def test_keys_packages_by_content_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            index = self._index(Path(scratch), packages_record(), compress=True)
            packages = snapshot.inventory("main", index)

        self.assertEqual(
            packages,
            {DEB: {"location": "pool/main/b/bash/bash_5.2.37-2_amd64.deb", "size": 1501148}},
        )

    def test_settles_one_checksum_served_under_two_names(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            index = self._index(
                Path(scratch),
                packages_record(filename="pool/main/b/bash/z.deb")
                + packages_record(name="bash-static", filename="pool/main/b/bash/a.deb"),
            )
            packages = snapshot.inventory("main", index)

        self.assertEqual(packages[DEB]["location"], "pool/main/b/bash/a.deb")

    def test_rejects_a_record_that_is_not_a_pinnable_deb(self) -> None:
        for record in (
            packages_record(filename="pool/main/b/bash/bash.udeb"),
            packages_record(filename="../../../etc/shadow.deb"),
            packages_record(filename="https://elsewhere.invalid/bash.deb"),
            packages_record(digest="short"),
        ):
            with tempfile.TemporaryDirectory() as scratch, self.assertRaises(SystemExit):
                snapshot.inventory("main", self._index(Path(scratch), record))


if __name__ == "__main__":
    unittest.main()
