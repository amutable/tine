# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the UAPI.16 file uapi16.

buck test tine//image:test
"""

import base64
import json
import os
import socket
import stat
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import override

import uapi16

# The epoch every image action runs under (box/runtime.bzl); any value works, but using the real
# one keeps the clamping assertions honest about what a build actually produces.
EPOCH = 1739577600


def parse(data: bytes) -> list[dict[str, object]]:
    """Decode an RFC7464 JSON-SEQ stream the way a consumer has to."""
    head, separator, rest = data.partition(b"\x1e")
    assert separator, "no record separator in the stream"
    assert head == b"", "the stream begins with something other than a record"
    objects: list[dict[str, object]] = []
    for record in rest.split(b"\x1e"):
        assert record.endswith(b"\n"), "a record is not terminated by a line feed"
        objects.append(json.loads(record.decode("utf-8")))
    assert objects[-1] == {"mediaType": uapi16.TRAILER_TYPE}
    return objects[:-1]


class TreeTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp", prefix="manifest.")))

    def manifest(self, tree: Path | None = None, roots: Sequence[str] = ()) -> list[dict[str, object]]:
        return parse(b"".join(uapi16.records(tree or self.tree, EPOCH, roots)))

    def named(self, tree: Path | None = None) -> dict[str, dict[str, object]]:
        return {str(obj["name"]): obj for obj in self.manifest(tree)[1:]}


class TestSequence(TreeTest):
    def test_the_first_object_is_the_root_and_names_the_media_type(self) -> None:
        (self.tree / "a").write_text("a")
        root = self.manifest()[0]
        self.assertEqual(root, {"mediaType": "application/vnd.uapi.16.manifest"})

    def test_an_empty_tree_has_no_named_objects(self) -> None:
        # An empty filesystem still has its root and trailer, with no named file objects.
        self.assertEqual(len(self.manifest()), 1)

    def test_each_record_is_framed_by_a_separator_and_a_line_feed(self) -> None:
        # A tree with one file needs three JSON-SEQ records: the root, file and trailer.
        (self.tree / "a").write_text("a")
        data = b"".join(uapi16.records(self.tree, EPOCH))
        self.assertEqual(data.count(b"\x1e"), 3)
        self.assertTrue(data.endswith(b"\n"))

    def test_the_json_carries_no_gratuitous_whitespace(self) -> None:
        (self.tree / "a").write_text("a")
        self.assertNotIn(b" ", b"".join(uapi16.records(self.tree, EPOCH)))


class TestOrder(TreeTest):
    def test_a_directory_is_followed_immediately_by_its_own_contents(self) -> None:
        (self.tree / "b/deep").mkdir(parents=True)
        (self.tree / "b/deep/leaf").write_text("leaf")
        (self.tree / "b/sibling").write_text("sibling")
        (self.tree / "a").write_text("a")
        (self.tree / "c").write_text("c")
        self.assertEqual(
            [obj["name"] for obj in self.manifest()[1:]],
            ["a", "b", "b/deep", "b/deep/leaf", "b/sibling", "c"],
        )

    def test_names_are_ordered_by_their_bytes_rather_than_by_case_or_locale(self) -> None:
        for name in ("b", "A", "a", "B", "_", "é"):
            (self.tree / name).write_text(name)
        self.assertEqual(
            [obj["name"] for obj in self.manifest()[1:]],
            ["A", "B", "_", "a", "b", "é"],
        )

    def test_the_bytes_do_not_depend_on_the_order_the_tree_was_created_in(self) -> None:
        names = ["gamma", "alpha", "beta", "delta"]
        outputs = []
        for order in (names, list(reversed(names))):
            with tempfile.TemporaryDirectory(dir="/var/tmp", prefix="manifest.") as scratch:
                tree = Path(scratch)
                for name in order:
                    (tree / name).mkdir()
                    (tree / name / "file").write_text(name)
                    os.utime(tree / name / "file", (EPOCH - 1, EPOCH - 1))
                    os.utime(tree / name, (EPOCH - 1, EPOCH - 1))
                outputs.append(b"".join(uapi16.records(tree, EPOCH)))
        self.assertEqual(outputs[0], outputs[1])


class TestRegularFiles(TreeTest):
    def test_a_regular_file_carries_its_size_mode_and_content_digest(self) -> None:
        (self.tree / "file").write_bytes(b"contents")
        (self.tree / "file").chmod(0o640)
        obj = self.named()["file"]
        self.assertEqual(obj["type"], "reg")
        self.assertEqual(obj["size"], 8)
        self.assertEqual(obj["mode"], 0o640)
        self.assertEqual(
            obj["sha256"],
            "d1b2a59fbea7e20077af9f91b27e95e865061b270be03ff539ab3b73587882e8",
        )

    def test_an_empty_file_hashes_to_the_digest_of_no_bytes(self) -> None:
        (self.tree / "empty").touch()
        obj = self.named()["empty"]
        self.assertEqual(obj["size"], 0)
        self.assertEqual(
            obj["sha256"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_the_contents_are_left_implied_so_the_name_beside_the_manifest_is_the_file(self) -> None:
        (self.tree / "file").write_text("x")
        self.assertNotIn("contents", self.named()["file"])

    def test_a_setuid_bit_survives_into_the_mode(self) -> None:
        (self.tree / "file").write_text("x")
        (self.tree / "file").chmod(0o4755)
        self.assertEqual(self.named()["file"]["mode"], 0o4755)


class TestDirectories(TreeTest):
    def test_a_directory_carries_a_mode_but_neither_a_size_nor_a_digest(self) -> None:
        (self.tree / "dir").mkdir(mode=0o750)
        obj = self.named()["dir"]
        self.assertEqual(obj["type"], "dir")
        self.assertEqual(obj["mode"], 0o750)
        self.assertNotIn("size", obj)
        self.assertNotIn("sha256", obj)
        self.assertNotIn("contents", obj)


class TestSymlinks(TreeTest):
    def test_a_symlink_carries_its_target_inline_and_sizes_itself_by_that_target(self) -> None:
        (self.tree / "link").symlink_to("../usr/bin/bash")
        obj = self.named()["link"]
        self.assertEqual(obj["type"], "lnk")
        self.assertEqual(obj["size"], len("../usr/bin/bash"))
        contents = obj["contents"]
        assert isinstance(contents, list)
        self.assertEqual(base64.b64decode(contents[0]["literal"]), b"../usr/bin/bash")

    def test_a_symlink_has_no_mode_because_the_format_gives_it_none(self) -> None:
        (self.tree / "link").symlink_to("target")
        self.assertNotIn("mode", self.named()["link"])

    def test_a_dangling_symlink_is_listed_as_the_link_it_is(self) -> None:
        (self.tree / "link").symlink_to("nowhere")
        self.assertEqual(self.named()["link"]["type"], "lnk")

    def test_a_symlink_to_a_directory_is_not_descended_into(self) -> None:
        (self.tree / "dir").mkdir()
        (self.tree / "dir/file").write_text("x")
        (self.tree / "link").symlink_to("dir")
        self.assertEqual(sorted(self.named()), ["dir", "dir/file", "link"])

    def test_a_target_that_is_not_utf8_still_encodes(self) -> None:
        os.symlink(b"\xff\xfe", self.tree / "link")
        contents = self.named()["link"]["contents"]
        assert isinstance(contents, list)
        self.assertEqual(base64.b64decode(contents[0]["literal"]), b"\xff\xfe")
        self.assertEqual(self.named()["link"]["size"], 2)


class TestHardlinks(TreeTest):
    def test_two_names_for_one_inode_share_a_token(self) -> None:
        (self.tree / "one").write_text("shared")
        os.link(self.tree / "one", self.tree / "two")
        objects = self.named()
        self.assertEqual(objects["one"]["inodeToken"], 1)
        self.assertEqual(objects["two"]["inodeToken"], 1)

    def test_a_file_reached_by_one_name_alone_carries_no_token(self) -> None:
        (self.tree / "one").write_text("alone")
        self.assertNotIn("inodeToken", self.named()["one"])

    def test_a_link_count_above_one_is_not_enough_when_the_other_name_is_outside_the_tree(
        self,
    ) -> None:
        outside = Path(
            self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp", prefix="manifest.outside."))
        )
        (outside / "one").write_text("shared")
        os.link(outside / "one", self.tree / "inside")
        self.assertEqual((self.tree / "inside").stat().st_nlink, 2)
        self.assertNotIn("inodeToken", self.named()["inside"])

    def test_tokens_count_up_from_one_in_the_order_the_manifest_reaches_them(self) -> None:
        for name in ("a", "b"):
            (self.tree / name).write_text(name)
            os.link(self.tree / name, self.tree / (name + ".link"))
        objects = self.named()
        self.assertEqual(objects["a"]["inodeToken"], 1)
        self.assertEqual(objects["a.link"]["inodeToken"], 1)
        self.assertEqual(objects["b"]["inodeToken"], 2)
        self.assertEqual(objects["b.link"]["inodeToken"], 2)

    def test_a_directory_never_takes_a_token_however_many_subdirectories_it_has(self) -> None:
        (self.tree / "dir/sub").mkdir(parents=True)
        self.assertNotIn("inodeToken", self.named()["dir"])


class TestOtherInodeTypes(TreeTest):
    def test_a_fifo_is_listed_with_its_mode(self) -> None:
        os.mkfifo(self.tree / "fifo", 0o600)
        obj = self.named()["fifo"]
        self.assertEqual(obj["type"], "fifo")
        self.assertEqual(obj["mode"], 0o600)
        self.assertNotIn("size", obj)

    def test_a_unix_socket_is_listed_as_one(self) -> None:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.tree / "sock"))
            self.assertEqual(self.named()["sock"]["type"], "sock")

    def test_the_inode_type_names_follow_the_format(self) -> None:
        self.assertEqual(
            [uapi16.inode_type(mode) for mode in (stat.S_IFBLK, stat.S_IFSOCK, stat.S_IFLNK)],
            ["blk", "sock", "lnk"],
        )

    def test_an_inode_type_with_no_representation_fails_rather_than_being_guessed(self) -> None:
        with self.assertRaises(SystemExit):
            uapi16.inode_type(0)


class TestDeviceNodes(unittest.TestCase):
    """Device nodes, from a fabricated inode: this sandbox refuses mknod."""

    def device(self, mode: int, rdev: int) -> uapi16.Object:
        st = os.stat_result(
            (mode, 0, 0, 1, 0, 0, 0, 0, EPOCH - 1, 0),
            {"st_rdev": rdev, "st_mtime_ns": (EPOCH - 1) * 10**9},
        )
        return uapi16.file_object(uapi16.Entry(name="dev/x", path=Path("dev/x"), st=st), epoch=EPOCH)

    def test_a_character_device_carries_its_numbers_and_nothing_a_file_would(self) -> None:
        self.assertEqual(
            self.device(stat.S_IFCHR | 0o666, os.makedev(1, 3)),
            {
                "name": "dev/x",
                "type": "chr",
                "major": 1,
                "minor": 3,
                "mode": 0o666,
                "uid": 0,
                "gid": 0,
                "mTimeNSec": (EPOCH - 1) * 10**9,
            },
        )

    def test_a_block_device_is_listed_the_same_way(self) -> None:
        obj = self.device(stat.S_IFBLK | 0o660, os.makedev(259, 4))
        self.assertEqual(obj["type"], "blk")
        self.assertEqual((obj["major"], obj["minor"]), (259, 4))


class TestTimestamps(TreeTest):
    def test_an_mtime_past_the_epoch_is_clamped_onto_it(self) -> None:
        (self.tree / "file").write_text("x")
        os.utime(self.tree / "file", (EPOCH + 10, EPOCH + 10))
        self.assertEqual(self.named()["file"]["mTimeNSec"], EPOCH * 10**9)

    def test_an_mtime_before_the_epoch_survives_in_nanoseconds(self) -> None:
        (self.tree / "file").write_text("x")
        os.utime(self.tree / "file", ns=(0, (EPOCH - 5) * 10**9 + 123))
        self.assertEqual(self.named()["file"]["mTimeNSec"], (EPOCH - 5) * 10**9 + 123)


class TestUnencodableNames(TreeTest):
    def test_a_name_that_is_not_utf8_fails_the_build_rather_than_being_dropped(self) -> None:
        os.mkdir(self.tree / os.fsdecode(b"\xff"))
        with self.assertRaises(SystemExit):
            self.manifest()

    def test_the_reserved_manifest_name_is_refused_at_the_top_level(self) -> None:
        (self.tree / "Uapi16Manifest").write_text("x")
        with self.assertRaises(SystemExit):
            self.manifest()

    def test_the_reserved_name_is_an_ordinary_file_name_further_down(self) -> None:
        (self.tree / "dir").mkdir()
        (self.tree / "dir/Uapi16Manifest").write_text("x")
        self.assertIn("dir/Uapi16Manifest", self.named())

    def test_a_reserved_suffix_at_the_top_level_is_refused_too(self) -> None:
        (self.tree / "Uapi16Manifest.gz").write_text("x")
        with self.assertRaises(SystemExit):
            self.manifest()


class TestWrite(TreeTest):
    def test_the_count_includes_the_root_and_trailer(self) -> None:
        # Writing one file must report three records and finish with the mandatory trailer.
        (self.tree / "file").write_text("x")
        out = (
            Path(self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp", prefix="manifest.out.")))
            / "m"
        )
        self.assertEqual(uapi16.write(self.tree, out, EPOCH), 3)
        self.assertEqual([obj.get("name") for obj in parse(out.read_bytes())], [None, "file"])


class TestScope(unittest.TestCase):
    def test_a_leading_and_trailing_slash_is_not_part_of_the_name(self) -> None:
        self.assertEqual(uapi16.scope(["/usr:", "/boot/"]), ["boot", "usr:"])

    def test_the_same_root_named_twice_is_listed_once(self) -> None:
        self.assertEqual(uapi16.scope(["/usr", "usr/"]), ["usr"])

    def test_a_root_below_another_is_dropped_rather_than_listed_twice(self) -> None:
        self.assertEqual(uapi16.scope(["/usr", "/usr/lib", "/boot"]), ["boot", "usr"])

    def test_a_name_that_merely_shares_a_prefix_is_kept(self) -> None:
        self.assertEqual(uapi16.scope(["/usr", "/usrfoo"]), ["usr", "usrfoo"])

    def test_the_whole_tree_covers_every_other_root(self) -> None:
        self.assertEqual(uapi16.scope(["/", "/usr"]), [""])


class TestScopedTree(TreeTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        for name in ("usr/bin", "opt/thing", "var/lib"):
            (self.tree / name).mkdir(parents=True)
            (self.tree / name / "file").write_text(name)

    def test_only_the_roots_named_are_listed(self) -> None:
        names = {str(obj["name"]) for obj in self.manifest(roots=["usr", "opt"])[1:]}
        self.assertEqual(names, {"opt", "opt/thing", "opt/thing/file", "usr", "usr/bin", "usr/bin/file"})

    def test_a_root_is_listed_before_the_contents_it_holds(self) -> None:
        names = [str(obj["name"]) for obj in self.manifest(roots=["usr"])[1:]]
        self.assertEqual(names, ["usr", "usr/bin", "usr/bin/file"])

    def test_a_root_the_image_does_not_have_is_skipped(self) -> None:
        names = {str(obj["name"]) for obj in self.manifest(roots=["usr", "efi"])[1:]}
        self.assertEqual(names, {"usr", "usr/bin", "usr/bin/file"})

    def test_a_root_naming_a_file_lists_that_file_alone(self) -> None:
        objects = self.manifest(roots=["usr/bin/file"])[1:]
        self.assertEqual([obj["name"] for obj in objects], ["usr/bin/file"])
        self.assertEqual(objects[0]["type"], "reg")

    def test_no_roots_is_the_whole_tree(self) -> None:
        names = {str(obj["name"]) for obj in self.manifest()[1:]}
        self.assertIn("var/lib/file", names)


class TestMerge(TreeTest):
    def part(self, name: str, roots: Sequence[str]) -> Path:
        out = self.tree / name
        uapi16.write(self.tree / "parts", out, EPOCH, roots)
        return out

    @override
    def setUp(self) -> None:
        super().setUp()
        self.parts = self.tree / "parts"
        for name in ("usr/bin", "boot/EFI"):
            (self.parts / name).mkdir(parents=True)
            (self.parts / name / "file").write_text(name)

    def merged(self, *sources: Path) -> list[dict[str, object]]:
        out = self.tree / "merged"
        uapi16.merge(sources, out)
        return parse(out.read_bytes())

    def test_the_whole_is_every_part_under_one_root_object(self) -> None:
        objects = self.merged(self.part("a", ["usr"]), self.part("b", ["boot"]))
        self.assertEqual(objects[0], {"mediaType": "application/vnd.uapi.16.manifest"})
        self.assertEqual(
            [obj["name"] for obj in objects[1:]],
            ["boot", "boot/EFI", "boot/EFI/file", "usr", "usr/bin", "usr/bin/file"],
        )

    def test_the_parts_are_ordered_by_what_they_hold_rather_than_by_the_order_given(self) -> None:
        objects = self.merged(self.part("a", ["boot"]), self.part("b", ["usr"]))
        self.assertEqual([obj["name"] for obj in objects[1:]][0], "boot")

    def test_a_name_two_parts_carry_is_taken_from_the_one_mounted_over_the_other(self) -> None:
        under = self.part("under", ["boot"])
        (self.parts / "boot").chmod(0o701)
        over = self.part("over", ["boot"])
        objects = {str(obj["name"]): obj for obj in self.merged(under, over)[1:]}
        self.assertEqual(objects["boot"]["mode"], 0o701)

    def test_a_directory_still_comes_before_the_contents_it_holds(self) -> None:
        (self.parts / "usr.txt").write_text("a sibling sorting between usr and usr/bin")
        names = [obj["name"] for obj in self.merged(self.part("a", ["usr", "usr.txt"]))[1:]]
        self.assertEqual(names, ["usr", "usr/bin", "usr/bin/file", "usr.txt"])

    def test_each_part_keeps_its_own_inodes_grouped(self) -> None:
        for name in ("usr/bin", "boot/EFI"):
            os.link(self.parts / name / "file", self.parts / name / "link")
        objects = {
            str(obj["name"]): obj
            for obj in self.merged(self.part("a", ["usr"]), self.part("b", ["boot"]))[1:]
        }
        self.assertEqual(objects["usr/bin/file"]["inodeToken"], objects["usr/bin/link"]["inodeToken"])
        self.assertEqual(objects["boot/EFI/file"]["inodeToken"], objects["boot/EFI/link"]["inodeToken"])
        self.assertNotEqual(objects["usr/bin/file"]["inodeToken"], objects["boot/EFI/file"]["inodeToken"])

    def test_something_that_is_not_a_manifest_is_refused(self) -> None:
        (self.tree / "junk").write_bytes(b'{"mediaType":"application/json"}\n')
        with self.assertRaises(SystemExit):
            self.merged(self.tree / "junk")


class TestReader(TreeTest):
    def test_requires_trailer_and_rejects_trailing_data(self) -> None:
        # A partial write and data after a trailer must fail instead of becoming a snapshot.
        out = self.tree / "manifest"
        root = b'\x1e{"mediaType":"application/vnd.uapi.16.manifest"}\n'
        trailer = b'\x1e{"mediaType":"application/vnd.uapi.16.trailer"}\n'
        for data in (root, root + trailer + trailer, root + trailer + b" ", root + trailer + b"\n"):
            out.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(SystemExit):
                uapi16.read(out)
        out.write_bytes(root + trailer)
        self.assertEqual(uapi16.read(out), [{"mediaType": uapi16.MEDIA_TYPE}])

    def test_accepts_whitespace_before_the_trailers_line_feed(self) -> None:
        # JSON whitespace may precede the trailer's final LF, but any bytes after that LF
        # are trailing data and must still fail.
        out = self.tree / "manifest"
        root = b'\x1e{"mediaType":"application/vnd.uapi.16.manifest"}\n'
        trailer = b'\x1e { "mediaType": "application/vnd.uapi.16.trailer" } \t\r\n'
        out.write_bytes(root + trailer)
        self.assertEqual(uapi16.read(out), [{"mediaType": uapi16.MEDIA_TYPE}])
        out.write_bytes(root + trailer + b" ")
        with self.assertRaises(SystemExit):
            uapi16.read(out)

    def test_accepts_root_metadata(self) -> None:
        # A storage manifest includes root metadata, which must survive parsing unchanged.
        out = self.tree / "manifest"
        objects: list[uapi16.Object] = [{"mediaType": uapi16.MEDIA_TYPE, "mode": 0o750}]
        uapi16.write_objects(objects, out)
        self.assertEqual(uapi16.read(out), objects)

    def test_rejects_non_relative_names(self) -> None:
        # UAPI.16 forbids path traversal and unnormalized names even in an external uapi16.
        for name in ("", "/a", "a/", "a//b", "a/../b", "./a", "a/.", "a/..", "a\n"):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                uapi16.check_name(name)


if __name__ == "__main__":
    unittest.main()
