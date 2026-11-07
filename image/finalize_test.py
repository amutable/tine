# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the image generators.

buck test tine//image:test
"""

import gzip
import tempfile
import unittest
from pathlib import Path
from typing import override

import finalize
import rootfs


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
        self.locales = self.tree / "usr/share/i18n/locales"
        self.locales.mkdir(parents=True)
        self.overrides = self.tree / "usr/local/share/i18n/locales"
        self.charmaps = self.tree / "usr/share/i18n/charmaps"
        self.charmaps.mkdir()

    def test_an_image_without_locale_gen_generates_nothing(self) -> None:
        self.assertIsNone(finalize.locale_entries(self.tree))

    def test_an_empty_locale_gen_generates_nothing(self) -> None:
        (self.tree / "etc/locale.gen").touch()
        self.assertIsNone(finalize.locale_entries(self.tree))

    def test_a_commented_out_locale_gen_lists_no_locale(self) -> None:
        (self.tree / "etc/locale.gen").write_text("# en_US.UTF-8 UTF-8\n\n")
        self.assertEqual(finalize.locale_entries(self.tree), [])

    def test_reads_every_uncommented_entry(self) -> None:
        (self.tree / "etc/locale.gen").write_text(
            "# a comment\nen_US.UTF-8 UTF-8\n  de_DE@euro ISO-8859-15  \n"
        )
        self.assertEqual(
            finalize.locale_entries(self.tree),
            [("en_US.UTF-8", "UTF-8"), ("de_DE@euro", "ISO-8859-15")],
        )

    def test_an_entry_without_a_charset_fails_the_build(self) -> None:
        (self.tree / "etc/locale.gen").write_text("en_US.UTF-8\n")
        with self.assertRaises(SystemExit):
            finalize.locale_entries(self.tree)

    def test_strips_the_charset_but_keeps_the_modifier(self) -> None:
        (self.locales / "de_DE@euro").touch()
        self.assertEqual(
            finalize.locale_source(self.tree, "de_DE.ISO-8859-15@euro"),
            (self.locales / "de_DE@euro").resolve(),
        )

    def test_prefers_a_source_named_after_the_whole_locale(self) -> None:
        (self.locales / "en_US").touch()
        (self.locales / "en_US.UTF-8").touch()
        self.assertEqual(
            finalize.locale_source(self.tree, "en_US.UTF-8"), (self.locales / "en_US.UTF-8").resolve()
        )

    def test_prefers_an_override_in_usr_local(self) -> None:
        self.overrides.mkdir(parents=True)
        (self.locales / "en_US").touch()
        (self.overrides / "en_US").touch()
        self.assertEqual(
            finalize.locale_source(self.tree, "en_US.UTF-8"), (self.overrides / "en_US").resolve()
        )

    def test_a_locale_without_a_source_fails_the_build(self) -> None:
        with self.assertRaises(SystemExit):
            finalize.locale_source(self.tree, "xx_XX.UTF-8")

    def test_takes_a_charmap_by_its_file_name(self) -> None:
        with gzip.open(self.charmaps / "UTF-8.gz", "wt") as charmap:
            charmap.write("<code_set_name> UTF-8\n")
        self.assertEqual(finalize.charmap_name(self.tree, "UTF-8"), "UTF-8")

    def test_finds_a_charmap_by_an_alias_in_its_header(self) -> None:
        with gzip.open(self.charmaps / "ISO-8859-1.gz", "wt") as charmap:
            charmap.write(
                "<code_set_name> ISO-8859-1\n"
                "% alias ISO_8859-1\n"
                "% alias LATIN1\n"
                "CHARMAP\n"
                "% alias NOT-AN-ALIAS\n"
            )
        self.assertEqual(finalize.charmap_name(self.tree, "latin1"), "ISO-8859-1")
        with self.assertRaises(SystemExit):
            finalize.charmap_name(self.tree, "NOT-AN-ALIAS")

    def test_a_charset_without_a_charmap_fails_the_build(self) -> None:
        with self.assertRaises(SystemExit):
            finalize.charmap_name(self.tree, "UTF8X")

    def test_reads_an_absolute_symlink_inside_the_image(self) -> None:
        (self.tree / "usr/share/i18n/SUPPORTED").write_text("en_US.UTF-8 UTF-8\n")
        (self.tree / "etc/locale.gen").symlink_to("/usr/share/i18n/SUPPORTED")
        with rootfs.chroot(self.tree) as root:
            entries = finalize.locale_entries(root)
        self.assertEqual(entries, [("en_US.UTF-8", "UTF-8")])

    def test_writes_into_the_archive_of_the_image(self) -> None:
        self.assertEqual(
            finalize.localedef_command(
                "/usr/bin/localedef",
                Path("/buildroot"),
                Path("/buildroot/usr/share/i18n/locales/en_US"),
                "UTF-8",
                "en_US.UTF-8",
                Path("/buildroot/usr/share/locale/locale.alias"),
            ),
            [
                "/usr/bin/localedef",
                "--prefix=/buildroot",
                "--inputfile=/buildroot/usr/share/i18n/locales/en_US",
                "--charmap=UTF-8",
                "--alias-file=/buildroot/usr/share/locale/locale.alias",
                "en_US.UTF-8",
            ],
        )
