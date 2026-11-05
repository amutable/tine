# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the directories that preload creates on an ESP.

buck test tine//image:test
"""

import unittest

from preload import directories

PRESENT = ["/EFI", "/EFI/BOOT", "/EFI/BOOT/BOOTX64.EFI", "/extensions"]


class DirectoriesTest(unittest.TestCase):
    def test_creates_missing_parents_first(self) -> None:
        targets = ["/portables/p.raw.v/p_1_x86-64.raw", "/extensions/e.raw.v/e_1_x86-64.raw"]
        self.assertEqual(
            directories(PRESENT, targets), ["/extensions/e.raw.v", "/portables", "/portables/p.raw.v"]
        )

    def test_creates_a_shared_parent_once(self) -> None:
        targets = ["/extensions/e.raw.v/e_1_x86-64.raw", "/extensions/e.raw.v/e_2_x86-64.raw"]
        self.assertEqual(directories(PRESENT, targets), ["/extensions/e.raw.v"])

    def test_matches_regardless_of_case(self) -> None:
        self.assertEqual(directories(PRESENT, ["/efi/linux/a.efi"]), ["/efi/linux"])

    def test_refuses_a_present_target(self) -> None:
        with self.assertRaisesRegex(SystemExit, "already contains /efi/boot/bootx64.efi"):
            directories(PRESENT, ["/efi/boot/bootx64.efi"])


if __name__ == "__main__":
    unittest.main()
