# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the repart driver's split-artifact handling.

    buck test tine//image:test

repart itself is not run here: what matters is the definition the driver hands it and what the
driver records about the artifact that comes back, both of which decide how a partition is
published.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import ddi

UUID = "fb1bb90c-9de1-b90c-8b89-e04b87c721bd"
PUBLISHED = f"AmutableOS_9_x86-64.usr-x86-64.{UUID}.raw"


DEFINITION = {
    "name": "usr",
    "type": "usr",
    "label": "AmutableOS_9",
    "filesystem": "erofs",
    "copy_files": ["/usr:/"],
    "size_min": None,
    "size_max": None,
    "minimize": "best",
    "compression": "zstd",
    "verity": "data",
    "verity_match_key": "usr",
}


class TestSplitName(unittest.TestCase):
    def test_split_names_by_type_and_uuid(self) -> None:
        # systemd-sysupdate reads the UUID back out of the name as @u, so the artifact repart
        # writes is already named the way it is published.
        self.assertIn("SplitName=%t.%U", ddi.Definition.parse(DEFINITION).render(split=True).splitlines())

    def test_no_split_name_without_split(self) -> None:
        rendered = ddi.Definition.parse(DEFINITION).render(split=False)
        self.assertNotIn("SplitName", rendered)


class TestCopyPartition(unittest.TestCase):
    def _copy(self, *, contents: bytes, raw_size: int) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            scratch = Path(scratch_dir)
            source = scratch / PUBLISHED
            source.write_bytes(contents)
            blocks = scratch / "out" / "usr.raw"
            metadata = scratch / "out" / "usr.json"
            row = {
                "split_path": str(source),
                "label": "AmutableOS_9",
                "raw_size": raw_size,
                "type": "usr-x86-64",
                "uuid": UUID,
            }
            ddi._copy_partition(row, "usr", blocks, metadata)
            self.assertEqual(blocks.stat().st_size, raw_size)
            return cast(dict[str, object], json.loads(metadata.read_text()))

    def test_records_the_published_name(self) -> None:
        metadata = self._copy(contents=b"x" * 512, raw_size=512)
        self.assertEqual(metadata["published"], PUBLISHED)
        self.assertEqual(metadata["type"], "usr-x86-64")
        self.assertEqual(metadata["uuid"], UUID)

    def test_restores_stripped_padding(self) -> None:
        # A signature artifact leaves repart shorter than its partition; CopyBlocks needs it back.
        self._copy(contents=b"signature", raw_size=512)

    def test_refuses_an_artifact_larger_than_its_partition(self) -> None:
        with self.assertRaises(SystemExit):
            self._copy(contents=b"x" * 1024, raw_size=512)

    def test_refuses_a_partition_repart_did_not_split(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            scratch = Path(scratch_dir)
            with self.assertRaises(SystemExit):
                ddi._copy_partition({"split_path": "-"}, "usr", scratch / "usr.raw", scratch / "usr.json")


class TestPinRelease(unittest.TestCase):
    PARENT = {"ID": "fedora", "VERSION_ID": "45", "NAME": "Fedora Linux"}
    BUILD = {"IMAGE_ID": "demo", "IMAGE_VERSION": "7"}

    def test_pins_id_and_version_id(self) -> None:
        pinned = ddi.pin_release(self.PARENT, {"SYSEXT_SCOPE": "portable"})
        self.assertEqual(pinned, {"ID": "fedora", "VERSION_ID": "45", "SYSEXT_SCOPE": "portable"})
        # The driver writes the fields in this order, and derives the default seed from it.
        self.assertEqual(list(pinned), ["ID", "VERSION_ID", "SYSEXT_SCOPE"])

    def test_release_overrides_the_parent(self) -> None:
        pinned = ddi.pin_release(self.PARENT, {"VERSION_ID": "46"})
        self.assertEqual(pinned, {"ID": "fedora", "VERSION_ID": "46"})

    def test_rolling_parent_pins_only_id(self) -> None:
        pinned = ddi.pin_release({"ID": "arch"}, {})
        self.assertEqual(pinned, {"ID": "arch"})

    def test_pins_the_image_build(self) -> None:
        pinned = ddi.pin_release(self.PARENT | self.BUILD, {"SYSEXT_VERSION_ID": "1"})
        self.assertEqual(
            pinned, {"ID": "fedora", "VERSION_ID": "45"} | self.BUILD | {"SYSEXT_VERSION_ID": "1"}
        )

    def test_any_pins_nothing(self) -> None:
        pinned = ddi.pin_release(self.PARENT | self.BUILD, {"ID": "_any"})
        self.assertEqual(pinned, {"ID": "_any"})


class TestMkfsEnvironment(unittest.TestCase):
    def test_records_erofs_digests_without_options(self) -> None:
        self.assertEqual(
            ddi.mkfs_environment({}), {"SYSTEMD_REPART_MKFS_OPTIONS_EROFS": "--xattr-inode-digest"}
        )

    def test_appends_the_digest_to_erofs_options(self) -> None:
        environment = ddi.mkfs_environment({"erofs": ["-zzstd", "-C524288"], "vfat": ["--invariant"]})
        self.assertEqual(
            environment,
            {
                "SYSTEMD_REPART_MKFS_OPTIONS_EROFS": "-zzstd -C524288 --xattr-inode-digest",
                "SYSTEMD_REPART_MKFS_OPTIONS_VFAT": "--invariant",
            },
        )

    def test_keeps_a_digest_name_the_caller_picked(self) -> None:
        environment = ddi.mkfs_environment({"erofs": ["--xattr-inode-digest=user.digest"]})
        self.assertEqual(
            environment, {"SYSTEMD_REPART_MKFS_OPTIONS_EROFS": "--xattr-inode-digest=user.digest"}
        )


HASH = "d3b20557d33fdb5d496b6faec48fbdf517db6f428dae1c362b59c16dac405bfe"


class TestWriteRootHash(unittest.TestCase):
    def _write(self, rows: list[dict[str, object]]) -> str:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch:
            output = Path(scratch) / "roothash"
            ddi.write_root_hash(rows, output)
            return output.read_text()

    def test_writes_the_generated_hash(self) -> None:
        # The data partition has no root hash and must not count.
        content = self._write([{"type": "usr-x86-64"}, {"type": "usr-x86-64-verity", "roothash": HASH}])
        self.assertEqual(content, HASH + "\n")

    def test_ignores_the_placeholder(self) -> None:
        content = self._write([{"roothash": "TBD"}, {"roothash": HASH}])
        self.assertEqual(content, HASH + "\n")

    def test_refuses_no_generated_hash(self) -> None:
        with self.assertRaises(SystemExit):
            self._write([{"roothash": "TBD"}, {"type": "esp"}])

    def test_refuses_more_than_one_hash(self) -> None:
        with self.assertRaises(SystemExit):
            self._write([{"roothash": HASH}, {"roothash": HASH.replace("d", "0")}])


class TestEmptyDirectories(unittest.TestCase):
    def test_removes_the_contents_and_keeps_the_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            tree = Path(scratch_dir)
            (tree / "boot/EFI/Linux").mkdir(parents=True)
            (tree / "boot/EFI/Linux/image.efi").write_text("uki")
            (tree / "boot/.hidden").write_text("hidden")
            (tree / "usr").mkdir()
            (tree / "usr/os-release").write_text("ID=test")
            ddi.empty_directories(tree, ["/boot", "/efi"])
            self.assertTrue((tree / "boot").is_dir())
            self.assertEqual(list((tree / "boot").iterdir()), [])
            # A directory the image doesn't have is left alone.
            self.assertFalse((tree / "efi").exists())
            self.assertEqual((tree / "usr/os-release").read_text(), "ID=test")


class TestStripFiles(unittest.TestCase):
    def _tree(self, scratch_dir: str) -> Path:
        tree = Path(scratch_dir)
        for version in ("6.17.0", "6.18.0"):
            modules = tree / "usr/lib/modules" / version
            (modules / "kernel/fs").mkdir(parents=True)
            (modules / "vmlinuz").write_text("kernel")
            (modules / "modules.dep").write_text("deps")
            (modules / "kernel/fs/xfs.ko").write_text("module")
        return tree

    def test_removes_every_match_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            tree = self._tree(scratch_dir)
            ddi.strip_files(tree, ["usr/lib/modules/*/vmlinuz"])
            self.assertEqual(sorted(tree.glob("usr/lib/modules/*/vmlinuz")), [])
            self.assertEqual(len(sorted(tree.glob("usr/lib/modules/*/modules.dep"))), 2)

    def test_takes_a_directory_with_its_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            tree = self._tree(scratch_dir)
            ddi.strip_files(tree, ["usr/lib/modules/*/kernel"])
            self.assertEqual(sorted(tree.glob("usr/lib/modules/*/kernel")), [])
            self.assertTrue((tree / "usr/lib/modules/6.17.0").is_dir())

    def test_reads_a_leading_slash_as_the_image_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            tree = self._tree(scratch_dir)
            ddi.strip_files(tree, ["/usr/lib/modules/*/vmlinuz"])
            self.assertEqual(sorted(tree.glob("usr/lib/modules/*/vmlinuz")), [])

    def test_ignores_a_pattern_that_matches_nothing(self) -> None:
        # A glob holds for every image the rule builds, so one without the file is not an error.
        with tempfile.TemporaryDirectory(prefix="ddi_test.") as scratch_dir:
            tree = self._tree(scratch_dir)
            ddi.strip_files(tree, ["usr/lib/modules/*/.vmlinuz.hmac"])
            self.assertEqual(len(sorted(tree.glob("usr/lib/modules/*/vmlinuz"))), 2)


if __name__ == "__main__":
    unittest.main()
