# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the listing of a logical image.

buck test tine//image:test
"""

import tempfile
import unittest
from pathlib import Path

import specs

import manifest
import storage
import uapi16


class TestManifest(unittest.TestCase):
    def test_lists_the_snapshot_without_blob_references(self) -> None:
        # The listing of an image must list every object of the top snapshot. A blob only exists
        # inside a store, so a regular file must lose its `contents`, while a symlink keeps its
        # target. Capture a tree with a file and a symlink and write its listing.
        with tempfile.TemporaryDirectory(prefix="manifest-test.", dir="/var/tmp") as scratch_dir:
            scratch = Path(scratch_dir)
            tree, store, out = scratch / "tree", scratch / "store", scratch / "listing"
            tree.mkdir()
            (tree / "file").write_text("contents")
            (tree / "link").symlink_to("file")
            storage.capture(storage.Root(tree), store, epoch=0)
            spec = specs.write(scratch / "spec.json", {"lower": [str(store)], "out": str(out)})

            manifest.main(["--spec", str(spec)])

            objects = {obj.get("name"): obj for obj in uapi16.read(out)}
            self.assertEqual(list(objects), [None, "file", "link"])
            self.assertNotIn("contents", objects["file"])
            self.assertEqual(uapi16.symlink_target(objects["link"]), "file")


if __name__ == "__main__":
    unittest.main()
