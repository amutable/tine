# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for writing a release's TUF targets.

buck test tine//quarry:test
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import targets

ARCH = "x86-64"
VERSION = "26.09.05"
BASE = f"image_{VERSION}_{ARCH}"
EXT = f"demo-ext_{VERSION}_{ARCH}"
# An extension of both classes is one image, named without a class suffix.
BOTH = f"demo-both_{VERSION}_{ARCH}"
PORTABLE = f"demo-portable_{VERSION}_{ARCH}"
USR = f"{BASE}.usr-{ARCH}.0123456789abcdef0123456789abcdef.raw"
VERITY = f"{BASE}.usr-{ARCH}-verity.fedcba9876543210fedcba9876543210.raw"
VERITY_SIG = f"{BASE}.usr-{ARCH}-verity-sig.00000000000000000000000000000000.raw"
HASH = "0123456789abcdef" * 4
FILES: dict[str, targets.Entry] = {
    f"{BASE}.efi": {"image": BASE, "kind": "uki"},
    f"{BASE}.initrd": {"image": BASE, "kind": "initrd"},
    f"{BASE}.vmlinuz": {"image": BASE, "kind": "kernel"},
    f"{BASE}.qcow2": {"image": BASE, "kind": "disk", "subkind": "qcow2"},
    f"{BASE}.raw": {"image": BASE, "kind": "disk", "subkind": "raw"},
    f"{BASE}.raw.zst": {"image": BASE, "kind": "disk", "subkind": "raw.zst"},
    USR: {"image": BASE, "kind": "partition", "subkind": "usr", "root_hash": HASH},
    VERITY: {"image": BASE, "kind": "partition", "subkind": "usr-verity"},
    VERITY_SIG: {"image": BASE, "kind": "partition", "subkind": "usr-verity-sig"},
    f"{BASE}.spdx.json": {"image": BASE, "kind": "sbom", "subkind": "spdx"},
    f"{BASE}.cdx.json": {"image": BASE, "kind": "sbom", "subkind": "cyclonedx"},
    f"{BASE}.pkgdb.sqlite": {"image": BASE, "kind": "pkgdb", "subkind": "rpm"},
    f"{BASE}.usr.Uapi16Manifest": {"image": BASE, "kind": "listing", "subkind": "uapi16manifest"},
    f"{BASE}.initrd.spdx.json": {"image": f"{BASE}.initrd", "kind": "sbom", "subkind": "spdx"},
    f"{BASE}.initrd.pkgdb.sqlite": {"image": f"{BASE}.initrd", "kind": "pkgdb", "subkind": "rpm"},
    f"{EXT}.sysext.raw": {
        "image": f"{EXT}.sysext",
        "kind": "extension",
        "subkind": "sysext",
        "root_hash": "f" * 64,
    },
    f"{EXT}.sysext.roothash": {"image": f"{EXT}.sysext", "kind": "roothash"},
    f"{EXT}.spdx.json": {"image": f"{EXT}.sysext", "kind": "sbom", "subkind": "spdx"},
    f"{EXT}.pkgdb.sqlite": {"image": f"{EXT}.sysext", "kind": "pkgdb", "subkind": "rpm"},
    f"{BOTH}.raw": {"image": BOTH, "kind": "extension", "subkind": "sysext+confext", "root_hash": "e" * 64},
    f"{BOTH}.spdx.json": {"image": BOTH, "kind": "sbom", "subkind": "spdx"},
    f"{PORTABLE}.raw": {"image": PORTABLE, "kind": "portable", "root_hash": "d" * 64},
    f"{PORTABLE}.roothash": {"image": PORTABLE, "kind": "roothash"},
    f"{PORTABLE}.spdx.json": {"image": PORTABLE, "kind": "sbom", "subkind": "spdx"},
}
for _entry in FILES.values():
    _entry["version"] = VERSION

# The sparse raw disk is never published.
EXCLUDE = ["disk/raw"]


class TestSupplements(unittest.TestCase):
    def test_uki_brings_usr_partition_and_initrd_listings(self) -> None:
        self.assertEqual(
            sorted(targets.supplements(f"{BASE}.efi", FILES)),
            sorted([USR, f"{BASE}.initrd.spdx.json", f"{BASE}.initrd.pkgdb.sqlite"]),
        )

    def test_usr_partition_brings_verity_and_listings(self) -> None:
        self.assertEqual(
            sorted(targets.supplements(USR, FILES)),
            sorted([VERITY, VERITY_SIG, f"{BASE}.spdx.json", f"{BASE}.cdx.json", f"{BASE}.pkgdb.sqlite"]),
        )

    def test_extension_brings_its_listings(self) -> None:
        self.assertEqual(
            sorted(targets.supplements(f"{EXT}.sysext.raw", FILES)),
            [f"{EXT}.pkgdb.sqlite", f"{EXT}.spdx.json"],
        )
        self.assertEqual(targets.supplements(f"{BOTH}.raw", FILES), [f"{BOTH}.spdx.json"])

    def test_portable_brings_its_listings(self) -> None:
        self.assertEqual(targets.supplements(f"{PORTABLE}.raw", FILES), [f"{PORTABLE}.spdx.json"])

    def test_listings_supplement_nothing(self) -> None:
        self.assertEqual(targets.supplements(f"{BASE}.spdx.json", FILES), [])
        self.assertEqual(targets.supplements(f"{BASE}.usr.Uapi16Manifest", FILES), [])

    def test_types(self) -> None:
        self.assertEqual(
            targets.supplement_type({"image": BASE, "kind": "sbom", "subkind": "spdx"}),
            {"type": "application/spdx+json"},
        )
        self.assertEqual(
            targets.supplement_type({"image": BASE, "kind": "sbom", "subkind": "cyclonedx"}),
            {"type": "application/vnd.cyclonedx+json"},
        )
        self.assertEqual(
            targets.supplement_type({"image": BASE, "kind": "pkgdb", "subkind": "rpm"}),
            {"type": "application/vnd.amutable.pkgdb"},
        )
        self.assertEqual(targets.supplement_type({"image": BASE, "kind": "partition", "subkind": "usr"}), {})


class TestMarks(unittest.TestCase):
    def test_update(self) -> None:
        for name in (f"{BASE}.efi", USR, VERITY_SIG, f"{EXT}.sysext.raw", f"{BOTH}.raw", f"{PORTABLE}.raw"):
            self.assertTrue(targets.is_update(FILES[name]), name)
        for name in (
            f"{BASE}.qcow2",
            f"{BASE}.raw",
            f"{BASE}.spdx.json",
            f"{EXT}.sysext.roothash",
            f"{PORTABLE}.roothash",
        ):
            self.assertFalse(targets.is_update(FILES[name]), name)

    def test_custom_marks(self) -> None:
        custom = targets.custom_marks(targets.published(FILES, EXCLUDE))
        self.assertNotIn(f"{BASE}.raw", custom)
        self.assertEqual(
            custom[USR]["quarry"],
            {
                "update": True,
                "supplemented-by": {
                    VERITY: {},
                    VERITY_SIG: {},
                    f"{BASE}.cdx.json": {"type": "application/vnd.cyclonedx+json"},
                    f"{BASE}.pkgdb.sqlite": {"type": "application/vnd.amutable.pkgdb"},
                    f"{BASE}.spdx.json": {"type": "application/spdx+json"},
                },
                "verity-root-hash": HASH,
            },
        )
        self.assertNotIn(
            f"{BASE}.spdx.json", custom, "a plain file needs no marks; the version is hardhat's"
        )
        self.assertEqual(custom[f"{EXT}.sysext.raw"]["quarry"]["verity-root-hash"], "f" * 64)


class TestSelected(unittest.TestCase):
    def test_kind_picks_every_subkind(self) -> None:
        self.assertTrue(targets.selected(FILES[f"{BASE}.qcow2"], ["disk"]))
        self.assertTrue(targets.selected(FILES[f"{BASE}.efi"], ["uki"]))
        self.assertFalse(targets.selected(FILES[f"{BASE}.efi"], ["disk"]))

    def test_subkind(self) -> None:
        self.assertTrue(targets.selected(FILES[f"{BASE}.qcow2"], ["disk/qcow2"]))
        self.assertFalse(targets.selected(FILES[f"{BASE}.raw.zst"], ["disk/qcow2"]))
        # A kind without subkinds is not picked by a selector of a subkind.
        self.assertFalse(targets.selected(FILES[f"{BASE}.efi"], ["uki/x"]))

    def test_subkind_glob(self) -> None:
        for name in (USR, VERITY, VERITY_SIG):
            self.assertTrue(targets.selected(FILES[name], ["partition/usr*"]), name)
        self.assertFalse(targets.selected(FILES[USR], ["partition/root*"]))


class TestVersion(unittest.TestCase):
    def test_one_version(self) -> None:
        self.assertEqual(targets.release_version(FILES), VERSION)

    def test_unversioned_files_follow_the_release(self) -> None:
        files: dict[str, targets.Entry] = FILES | {"foreign.txt": {"image": "foreign", "kind": "listing"}}
        self.assertEqual(targets.release_version(files), VERSION)

    def test_mixed_versions(self) -> None:
        files: dict[str, targets.Entry] = FILES | {
            "old.raw": {"image": "old", "kind": "portable", "version": "1"}
        }
        with self.assertRaisesRegex(SystemExit, "exactly one version"):
            targets.release_version(files)

    def test_no_version(self) -> None:
        with self.assertRaisesRegex(SystemExit, "exactly one version"):
            targets.release_version({"foreign.txt": {"image": "foreign", "kind": "listing"}})


class TestHardhatArgs(unittest.TestCase):
    def test_transfer_options(self) -> None:
        argv = targets.hardhat_transfer_args(
            ["sysupdate.d=transfers/sysupdate.d", "sysupdate.app.d=confext:app"]
        )
        self.assertEqual(argv[0], "--quarry-transfer-dir")
        self.assertTrue(argv[1].startswith("sysupdate.d=/"), argv[1])
        self.assertTrue(argv[1].endswith("/transfers/sysupdate.d"), argv[1])
        # A confext source is passed through as it is.
        self.assertEqual(argv[2:], ["--quarry-transfer-dir", "sysupdate.app.d=confext:app"])
        self.assertEqual(targets.hardhat_transfer_args([]), [])
        with self.assertRaises(SystemExit):
            targets.hardhat_transfer_args(["sysupdate.d"])


class TestPublished(unittest.TestCase):
    def test_exclude(self) -> None:
        names = targets.published(FILES, EXCLUDE)
        self.assertNotIn(f"{BASE}.raw", names)
        self.assertIn(f"{BASE}.raw.zst", names)
        self.assertIn(USR, names)

    def test_rejects_a_path(self) -> None:
        with self.assertRaises(SystemExit):
            targets.published({"a/b": {"image": "a", "kind": "uki"}}, [])


# This fake hardhat records how it was called, and what the files it was given by name hold, as its
# -o output.
_FAKE_HARDHAT = """\
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
named = [a for a in argv if not os.path.isabs(a) and os.path.isfile(a)]
Path(argv[argv.index("-o") + 1]).write_text(json.dumps({
    "argv": argv,
    "custom": json.loads(Path(argv[argv.index("--custom-from") + 1]).read_text()),
    "named": {n: Path(n).read_text() for n in named},
}))
"""


class TestCmdTargets(unittest.TestCase):
    def test_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hardhat = base / "hardhat"
            hardhat.write_text(f"#!{sys.executable}\n{_FAKE_HARDHAT}")
            hardhat.chmod(0o755)
            files = base / "files"
            files.mkdir()
            for name in FILES:
                (files / name).write_text(name)
            description = base / "description.json"
            description.write_text(json.dumps({"arch": ARCH, "files": FILES}))
            transfers = base / "transfers"
            transfers.mkdir()
            out = base / "out"
            out.mkdir()
            targets.main([
                "--artifacts", os.fspath(description),
                "--files", os.fspath(files),
                "--output", os.fspath(out / f"targets_{ARCH}.json"),
                "--hardhat", os.fspath(hardhat),
                "--exclude", "disk/raw",
                "--transfer-dir", f"sysupdate.d={transfers}",
            ])  # fmt: skip

            self.assertFalse((out / "1.root.json").exists())
            called = json.loads((out / f"targets_{ARCH}.json").read_text())
            self.assertEqual(called["argv"][0], "targets")
            # The build holds no signing key.
            self.assertIn("--unsigned", called["argv"])
            self.assertNotIn("--keystore", called["argv"])
            self.assertIn(f"sysupdate.d={transfers}", called["argv"])
            self.assertEqual(called["argv"][called["argv"].index("--custom-version") + 1], VERSION)
            # Every published file but the excluded one is read through the stage.
            expected = {n: n for n in FILES if n != f"{BASE}.raw"}
            self.assertEqual(called["named"], expected)
            self.assertEqual(called["custom"], targets.custom_marks(targets.published(FILES, EXCLUDE)))


if __name__ == "__main__":
    unittest.main()
