# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the image generators.

buck test tine//image:test
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import finalize


class TestKernelVersions(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))
        self.modules = self.tree / "usr/lib/modules"

    def test_an_image_without_modules_gives_depmod_nothing_to_do(self) -> None:
        self.assertEqual(finalize.kernel_versions(self.tree), [])

    def test_finds_every_installed_kernel(self) -> None:
        for version in ("6.19.0-1", "7.2.0-0.rc5"):
            (self.modules / version).mkdir(parents=True)
            (self.modules / version / "modules.order").touch()
        self.assertEqual(finalize.kernel_versions(self.tree), ["6.19.0-1", "7.2.0-0.rc5"])

    def test_skips_a_directory_left_behind_for_a_kernel_that_is_not_installed(self) -> None:
        (self.modules / "6.19.0-1/updates").mkdir(parents=True)
        self.assertEqual(finalize.kernel_versions(self.tree), [])


class TestDepmod(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))

    def test_reads_only_the_configuration_it_is_given(self) -> None:
        self.assertEqual(
            finalize.depmod_command(
                "/usr/bin/depmod",
                Path("/buildroot"),
                "6.19.0-1",
                config=Path("/tmp/depmod.d"),
                moduledir=True,
            ),
            [
                "/usr/bin/depmod",
                "--all",
                "--basedir=/buildroot",
                "--moduledir=/usr/lib/modules",
                "--config=/tmp/depmod.d",
                "6.19.0-1",
            ],
        )

    def test_leaves_out_the_module_directory_for_a_kmod_without_the_option(self) -> None:
        cmd = finalize.depmod_command(
            "/usr/bin/depmod", Path("/buildroot"), "6.19.0-1", config=Path("/tmp/depmod.d"), moduledir=False
        )
        self.assertNotIn("--moduledir=/usr/lib/modules", cmd)

    def test_an_earlier_directory_hides_a_file_of_the_same_name(self) -> None:
        for directory, content in (("etc/depmod.d", "etc"), ("usr/lib/depmod.d", "usr")):
            (self.tree / directory).mkdir(parents=True)
            (self.tree / directory / "search.conf").write_text(content)
        (self.tree / "usr/lib/depmod.d/other.conf").write_text("other")
        self.assertEqual(finalize.depmod_config(self.tree), {"search.conf": "etc", "other.conf": "other"})

    def test_reads_only_conf_files(self) -> None:
        (self.tree / "usr/lib/depmod.d").mkdir(parents=True)
        (self.tree / "usr/lib/depmod.d/README").write_text("readme")
        (self.tree / "usr/lib/depmod.d/.hidden.conf").write_text("hidden")
        self.assertEqual(finalize.depmod_config(self.tree), {})

    def test_an_external_module_directory_fails_the_build(self) -> None:
        (self.tree / "etc/depmod.d").mkdir(parents=True)
        (self.tree / "etc/depmod.d/external.conf").write_text("external * /opt/modules\n")
        with self.assertRaises(SystemExit):
            finalize.depmod_config(self.tree)

    def test_an_absolute_module_symlink_fails_the_build(self) -> None:
        kernel = self.tree / "usr/lib/modules/6.19.0-1/weak-updates"
        kernel.mkdir(parents=True)
        (kernel / "foo.ko.xz").symlink_to("/usr/lib/modules/6.18.0-1/extra/foo.ko.xz")
        with self.assertRaises(SystemExit):
            finalize.check_module_symlinks(self.tree, "6.19.0-1")

    def test_ignores_symlinks_in_build_and_source(self) -> None:
        kernel = self.tree / "usr/lib/modules/6.19.0-1/build"
        kernel.mkdir(parents=True)
        (kernel / "foo.ko").symlink_to("/usr/src/foo.ko")
        (kernel / "relative.ko").symlink_to("foo.ko")
        finalize.check_module_symlinks(self.tree, "6.19.0-1")


class TestHwdb(unittest.TestCase):
    def test_writes_the_database_the_image_ships_in_usr(self) -> None:
        self.assertEqual(
            finalize.hwdb_command("/usr/bin/systemd-hwdb", Path("/buildroot"), usr=True, strict=True),
            ["/usr/bin/systemd-hwdb", "--root=/buildroot", "--usr", "--strict", "update"],
        )

    def test_leaves_out_the_flags_it_was_told_not_to_pass(self) -> None:
        self.assertEqual(
            finalize.hwdb_command("/usr/bin/systemd-hwdb", Path("/buildroot"), usr=False, strict=False),
            ["/usr/bin/systemd-hwdb", "--root=/buildroot", "update"],
        )


class TestLocales(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))
        (self.tree / "etc").mkdir()

    def test_an_image_without_locale_gen_generates_nothing(self) -> None:
        self.assertFalse(finalize.wants_locales(self.tree))

    def test_a_commented_out_locale_gen_asks_for_nothing(self) -> None:
        (self.tree / "etc/locale.gen").write_text("# en_US.UTF-8 UTF-8\n\n")
        self.assertFalse(finalize.wants_locales(self.tree))

    def test_an_uncommented_locale_is_a_request(self) -> None:
        (self.tree / "etc/locale.gen").write_text("# a comment\nen_US.UTF-8 UTF-8\n")
        self.assertTrue(finalize.wants_locales(self.tree))


class TestWorldWritable(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="finalize.")))

    def test_restores_the_mode_a_layer_could_not_carry(self) -> None:
        for name in ("tmp", "var/tmp"):
            (self.tree / name).mkdir(parents=True, mode=0o755)
        finalize.world_writable(self.tree)
        for name in ("tmp", "var/tmp"):
            self.assertEqual((self.tree / name).stat().st_mode & 0o7777, 0o1777)

    def test_leaves_an_image_that_ships_neither_alone(self) -> None:
        finalize.world_writable(self.tree)
        self.assertFalse((self.tree / "tmp").exists())

    def test_does_not_chmod_through_a_symlink(self) -> None:
        (self.tree / "var").mkdir()
        (self.tree / "elsewhere").mkdir(mode=0o755)
        (self.tree / "var/tmp").symlink_to("../elsewhere")
        finalize.world_writable(self.tree)
        self.assertEqual((self.tree / "elsewhere").stat().st_mode & 0o7777, 0o755)
