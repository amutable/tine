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
import storage
import uapi16

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
        self.assertIn(
            "SplitName=%t.%U", ddi.Definition.parse(DEFINITION).render(split=True, exclude=[]).splitlines()
        )

    def test_no_split_name_without_split(self) -> None:
        rendered = ddi.Definition.parse(DEFINITION).render(split=False, exclude=[])
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


class TestExcludeFiles(unittest.TestCase):
    def test_leaves_out_the_contents_of_excluded_directories(self) -> None:
        # The ESP of a bootable disk copies /boot in another repart run. The root partition must
        # leave out the contents of /boot and keep the directory, which a trailing slash tells
        # repart.
        rendered = ddi.Definition.parse(DEFINITION).render(split=False, exclude=["/boot", "efi"])
        self.assertIn("ExcludeFiles=/boot/", rendered.splitlines())
        self.assertIn("ExcludeFiles=/efi/", rendered.splitlines())


class TestListing(unittest.TestCase):
    OBJECTS: list[uapi16.Object] = [
        {"mediaType": uapi16.MEDIA_TYPE, "type": "dir", "mode": 0o755},
        {"name": "boot", "type": "dir", "mode": 0o755},
        {"name": "boot/uki.efi", "type": "reg", "mode": 0o644, "inodeToken": 1},
        {"name": "usr", "type": "dir", "mode": 0o755},
        {"name": "usr/a", "type": "reg", "mode": 0o644, "inodeToken": 1},
        {"name": "usr/b", "type": "reg", "mode": 0o644, "inodeToken": 2},
        {"name": "usr/c", "type": "reg", "mode": 0o644, "inodeToken": 2},
    ]

    def test_lists_the_roots_without_excluded_contents(self) -> None:
        # A partition that copies / and excludes /boot lists the image without the contents of
        # /boot. Its root object contains only the media type, because the partition doesn't
        # contain the image root. usr/a loses its token, because its other name is excluded.
        listed = ddi._listing(self.OBJECTS, ("/",), ["/boot"])
        self.assertEqual(listed[0], {"mediaType": uapi16.MEDIA_TYPE})
        self.assertEqual([obj["name"] for obj in listed[1:]], ["boot", "usr", "usr/a", "usr/b", "usr/c"])
        self.assertNotIn("inodeToken", listed[3])
        self.assertEqual(listed[4]["inodeToken"], 1)

    def test_lists_only_the_partition_sources(self) -> None:
        # OBJECTS contains /boot and /usr. The listing of a partition that copies /usr must contain /usr
        # and its contents and nothing of /boot.
        listed = ddi._listing(self.OBJECTS, ("/usr",), [])
        self.assertEqual([obj["name"] for obj in listed[1:]], ["usr", "usr/a", "usr/b", "usr/c"])

    def test_a_source_the_image_does_not_have_lists_nothing(self) -> None:
        # The ESP definition names both /boot and /efi for images that have one or the other.
        # A source the image doesn't have adds nothing to the listing.
        listed = ddi._listing(self.OBJECTS, ("/boot", "/efi"), [])
        self.assertEqual([obj["name"] for obj in listed[1:]], ["boot", "boot/uki.efi"])

    def test_a_source_naming_a_file_lists_that_file_alone(self) -> None:
        # A partition can copy a single file. Its listing must contain that file and nothing else.
        listed = ddi._listing(self.OBJECTS, ("/usr/a",), [])
        self.assertEqual([obj["name"] for obj in listed[1:]], ["usr/a"])


class TestAddDirectories(unittest.TestCase):
    def test_adds_missing_directories_in_order(self) -> None:
        # An extension delta can lack /opt and /usr, which its partition copies. Restoring the
        # snapshot must create /opt with the metadata of the full image and /usr with defaults,
        # each in its place in the manifest order.
        full: list[uapi16.Object] = [
            {"mediaType": uapi16.MEDIA_TYPE, "type": "dir", "mode": 0o755, "mTimeNSec": 7},
            {"name": "etc", "type": "dir", "mode": 0o755, "mTimeNSec": 1},
            {"name": "opt", "type": "dir", "mode": 0o700, "mTimeNSec": 2},
        ]
        snapshot = storage.Snapshot([])
        snapshot.objects = [full[0], full[1]]
        ddi._add_directories(snapshot, full, ["opt", "usr", "etc"])
        self.assertEqual([obj.get("name") for obj in snapshot.objects], [None, "etc", "opt", "usr"])
        self.assertEqual(snapshot.objects[2]["mode"], 0o700)
        self.assertEqual(snapshot.objects[3], {"name": "usr", "type": "dir", "mode": 0o755, "mTimeNSec": 7})


if __name__ == "__main__":
    unittest.main()
