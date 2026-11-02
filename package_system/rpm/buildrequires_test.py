# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the BuildRequires query of an RPM spec.

The tests query specs with the dev box's rpmbuild outside a buildroot. They replace the chroot with a mock
and point `builder.CHROOT_TOPDIR` at the staged rpmbuild tree.

buck test tine//package_system/rpm:buildrequires-test
"""

import contextlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import builder
import buildrequires


def setUpModule() -> None:
    # The dev box contains rpmbuild only when it builds from a Fedora release.
    if shutil.which("rpmbuild") is None:
        raise unittest.SkipTest("the box contains no rpmbuild")


SPEC = """\
Name: example
Version: 1
Release: %autorelease
Summary: BuildRequires fixture
License: MPL-2.0
%bcond_without docs
%bcond_without lto
BuildRequires: example-compiler
BuildRequires: example-meson >= 0.60
BuildRequires: (python3-foo or python3-bar)
BuildRequires: %(cat %{_sourcedir}/extra-buildrequires)
%if %{with docs}
BuildRequires: docbook-style-xsl
%endif
%if %{with lto}
BuildRequires: lto-tool
%endif
%if 0%{?common}
BuildRequires: common-devel
%endif
%if "%{?dist}" == ".test"
BuildRequires: dist-devel
%endif
%if "%{release}" == "7.test"
BuildRequires: release-devel
%endif

%description
A fixture.
"""

GENERATOR_SPEC = """\
Name: example
Version: 1
Release: 1
Summary: %generate_buildrequires fixture
License: MPL-2.0
BuildRequires: /usr/bin/rpmbuild

%description
A fixture.

%generate_buildrequires
echo generated-devel
"""

IN_PLACE_SPEC = SPEC.replace(
    "%description", "%if %{defined _build_in_place}\nBuildRequires: in-place-devel\n%endif\n\n%description"
)


class Query(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpm-br-test.", dir="/var/tmp"))
        )
        self.spec_file = self.scratch / "example.spec"
        self.spec_file.write_text(SPEC)
        self.extra = self.scratch / "extra-buildrequires"
        self.extra.write_text("source-devel\n")
        self.enterContext(mock.patch.object(tempfile, "tempdir", str(self.scratch)))
        self.mounted = self.enterContext(
            mock.patch.object(builder, "buildroot", return_value=contextlib.nullcontext())
        )
        self.enterContext(mock.patch.object(builder, "CHROOT_TOPDIR", self.scratch / "topdir"))
        self.enterContext(mock.patch.dict(os.environ))

    def specification(
        self,
        *,
        spec_file: Path | None = None,
        sources: list[Path] | None = None,
        source_tree: Path | None = None,
        rpmbuild_options: list[str] | None = None,
        in_place_rpmbuild_options: list[str] | None = None,
        incremental: bool = False,
    ) -> buildrequires.Spec:
        return buildrequires.Spec(
            lower=["buildroot"],
            spec_file=str(spec_file or self.spec_file),
            sources=[str(source) for source in (sources if sources is not None else [self.extra])],
            source_tree=str(source_tree) if source_tree is not None else None,
            dist=".test",
            source_date_epoch=1754006400,
            release="7",
            rpmbuild_options=["--define=common 1"] if rpmbuild_options is None else rpmbuild_options,
            in_place_rpmbuild_options=in_place_rpmbuild_options or [],
            incremental=incremental,
            out=str(self.scratch / "buildrequires.json"),
        )

    def test_query_uses_the_options_of_the_build(self) -> None:
        self.assertEqual(
            buildrequires.query(self.specification()),
            [
                "(python3-foo or python3-bar)",
                "common-devel",
                "dist-devel",
                "docbook-style-xsl",
                "example-compiler",
                "example-meson >= 0.60",
                "lto-tool",
                "release-devel",
                "source-devel",
            ],
        )
        self.mounted.assert_called_once_with(["buildroot"], self.scratch / "topdir")

    def test_dev_mode_options_apply(self) -> None:
        self.assertNotIn("lto-tool", buildrequires.query(self.specification(incremental=True)))

    def test_query_uses_the_environment_of_the_build(self) -> None:
        self.spec_file.write_text(
            SPEC.replace(
                "%description",
                f'%if "%{{getenv:HOME}}" == "{builder.CHROOT_TOPDIR}"\n'
                "BuildRequires: home-devel\n%endif\n"
                '%if "%{getenv:SOURCE_DATE_EPOCH}" == "1754006400"\n'
                "BuildRequires: epoch-devel\n%endif\n%description",
            )
        )
        with mock.patch.dict(os.environ, {"HOME": "/root", "SOURCE_DATE_EPOCH": "1739577600"}):
            found = buildrequires.query(self.specification())
        self.assertIn("home-devel", found)
        self.assertIn("epoch-devel", found)

    def test_in_place_queries_define_the_debug_prefix_map(self) -> None:
        checkout = self.scratch / "checkout"
        checkout.mkdir()
        (checkout / "extra-buildrequires").write_text("source-devel\n")
        (checkout / "example.spec").write_text(
            SPEC.replace(
                "%description",
                "%if %{defined _distro_extra_cflags}\nBuildRequires: flags-devel\n%endif\n%description",
            )
        )
        found = buildrequires.query(
            self.specification(spec_file=checkout / "example.spec", sources=[], source_tree=checkout)
        )
        self.assertIn("flags-devel", found)

    def test_options_select_conditional_buildrequires(self) -> None:
        found = buildrequires.query(self.specification(rpmbuild_options=[]))
        self.assertIn("docbook-style-xsl", found)
        self.assertNotIn("common-devel", found)

    def test_hyphenated_options_select_conditional_buildrequires(self) -> None:
        self.spec_file.write_text(
            SPEC.replace(
                "%description",
                "%if %{defined _with_foo_bar}\nBuildRequires: foo-bar-devel\n%endif\n"
                "%if %{defined _without_baz_qux}\nBuildRequires: baz-qux-devel\n%endif\n%description",
            )
        )
        found = buildrequires.query(
            self.specification(rpmbuild_options=["--with=foo-bar", "--without=baz-qux"])
        )
        self.assertIn("foo-bar-devel", found)
        self.assertIn("baz-qux-devel", found)

    def test_in_place_options_apply_after_the_others(self) -> None:
        found = buildrequires.query(
            self.specification(
                rpmbuild_options=["--with=docs"], in_place_rpmbuild_options=["--without=docs"]
            )
        )
        self.assertNotIn("docbook-style-xsl", found)

    def test_only_an_in_place_build_defines_build_in_place(self) -> None:
        checkout = self.scratch / "checkout"
        checkout.mkdir()
        (checkout / "extra-buildrequires").write_text("source-devel\n")
        (checkout / "example.spec").write_text(IN_PLACE_SPEC)
        self.assertIn(
            "in-place-devel",
            buildrequires.query(
                self.specification(spec_file=checkout / "example.spec", sources=[], source_tree=checkout)
            ),
        )

    def test_an_archive_build_does_not_define_build_in_place(self) -> None:
        self.spec_file.write_text(IN_PLACE_SPEC)
        self.assertNotIn("in-place-devel", buildrequires.query(self.specification()))

    def test_a_noarch_spec_parses_with_the_macro_files_of_noarch(self) -> None:
        # rpmbuild expands %{_lib} to lib for a noarch spec on a 64-bit host, not to lib64.
        self.spec_file.write_text(
            SPEC.replace(
                "License: MPL-2.0\n",
                "License: MPL-2.0\nBuildArch: noarch\nBuildRequires: /usr/%{_lib}/libexample.so\n",
            )
        )
        found = buildrequires.query(self.specification())
        self.assertIn("/usr/lib/libexample.so", found)
        # The reload keeps the definitions of the command line.
        self.assertIn("common-devel", found)

    def test_an_in_place_spec_parses_in_the_checkout_like_the_build(self) -> None:
        checkout = self.scratch / "checkout"
        (checkout / "rpm").mkdir(parents=True)
        (checkout / "VERSION").write_text("2.5\n")
        (checkout / "rpm" / "extra.inc").write_text("BuildRequires: include-devel\n")
        (checkout / "rpm" / "extra-buildrequires").write_text("source-devel\n")
        # rpm opens %include relative to the working directory, which is the root of the checkout.
        spec = SPEC.replace("Version: 1\n", "Version: %(cat VERSION)\n%include rpm/extra.inc\n")
        (checkout / "rpm" / "example.spec").write_text(spec)

        found = buildrequires.query(
            self.specification(spec_file=checkout / "rpm" / "example.spec", sources=[], source_tree=checkout)
        )

        self.assertIn("include-devel", found)
        self.assertIn("source-devel", found)
        self.assertEqual((checkout / "rpm" / "example.spec").read_text(), spec)

    def test_parse_time_writes_do_not_modify_the_checkout(self) -> None:
        checkout = self.scratch / "checkout"
        checkout.mkdir()
        (checkout / "extra-buildrequires").write_text("source-devel\n")
        spec = SPEC.replace(
            "%description",
            '%if "%(echo yes > written && cat written)" == "yes"\n'
            "BuildRequires: generated-devel\n%endif\n%description",
        )
        (checkout / "example.spec").write_text(spec)

        found = buildrequires.query(
            self.specification(spec_file=checkout / "example.spec", sources=[], source_tree=checkout)
        )
        self.assertIn("generated-devel", found)

        self.assertEqual(
            sorted(path.name for path in checkout.iterdir()), ["example.spec", "extra-buildrequires"]
        )

    def test_query_leaves_the_spec_unchanged(self) -> None:
        buildrequires.query(self.specification())
        self.assertEqual(self.spec_file.read_text(), SPEC)

    def test_main_writes_a_json_list(self) -> None:
        spec = self.specification()
        path = self.scratch / "spec.json"
        path.write_text(json.dumps(spec))
        readonly = self.enterContext(
            mock.patch.object(
                buildrequires.rootfs, "readonly_project", return_value=contextlib.nullcontext()
            )
        )
        buildrequires.main(["--spec", str(path)])
        readonly.assert_called_once()
        self.assertIn("example-compiler", json.loads(Path(spec["out"]).read_text()))

    def test_unparsable_spec_fails(self) -> None:
        self.spec_file.write_text("Name: example\n%if\n")
        with self.assertRaisesRegex(SystemExit, "rpmbuild -bd failed"):
            buildrequires.query(self.specification())

    def test_missing_static_buildrequires_are_listed_before_generated_ones(self) -> None:
        # rpmbuild -bd checks the static BuildRequires before it runs %generate_buildrequires. The script can
        # need a static BuildRequires, such as the package of its macros. With a static BuildRequires
        # missing, the query lists only the static ones.
        self.spec_file.write_text(
            SPEC.replace("%description", "%generate_buildrequires\necho generated-devel\n\n%description")
        )
        found = buildrequires.query(self.specification())
        self.assertIn("example-compiler", found)
        self.assertNotIn("generated-devel", found)

    def test_generated_buildrequires_are_listed_once_the_static_ones_are_installed(self) -> None:
        # The box contains /usr/bin/rpmbuild, so the static BuildRequires is installed. rpmbuild -bd then
        # runs %generate_buildrequires, and the query lists what the script reports.
        self.spec_file.write_text(GENERATOR_SPEC)
        self.assertEqual(buildrequires.query(self.specification()), ["generated-devel"])

    def test_an_installed_spec_lists_nothing(self) -> None:
        # The query lists nothing when the buildroot contains every BuildRequires.
        self.spec_file.write_text(
            GENERATOR_SPEC.replace("%generate_buildrequires\necho generated-devel\n", "")
        )
        self.assertEqual(buildrequires.query(self.specification()), [])

    def test_an_in_place_spec_needs_no_source_archive(self) -> None:
        # The source tree of an in-place build replaces the Source archive of the spec, and the build never
        # reads it. Create an in-place spec whose Source0 and Patch0 aren't in the checkout, and check that
        # the query lists its BuildRequires.
        checkout = self.scratch / "checkout"
        checkout.mkdir()
        (checkout / "extra-buildrequires").write_text("source-devel\n")
        (checkout / "example.spec").write_text(
            SPEC.replace(
                "License: MPL-2.0\n", "License: MPL-2.0\nSource0: example-1.tar.gz\nPatch0: fix.patch\n"
            )
        )
        found = buildrequires.query(
            self.specification(spec_file=checkout / "example.spec", sources=[], source_tree=checkout)
        )
        self.assertIn("example-compiler", found)

    def test_script_output_does_not_add_buildrequires(self) -> None:
        # %generate_buildrequires writes to the same stderr as rpmbuild. A line of the script that looks like
        # a missing BuildRequires, or that isn't UTF-8, must not change the result. Only the lines after
        # rpmbuild's last header count.
        self.spec_file.write_text(
            GENERATOR_SPEC.replace(
                "echo generated-devel\n",
                "printf 'error: Failed build dependencies:\\n' >&2\n"
                "printf '\\tbogus-devel is needed by example\\n\\377\\n' >&2\n"
                "echo generated-devel\n",
            )
        )
        self.assertEqual(buildrequires.query(self.specification()), ["generated-devel"])

    def test_an_installed_buildconflicts_fails(self) -> None:
        # rpmbuild exits with 11 for an installed BuildConflicts as well. The box contains rpm-build, so a
        # spec that conflicts with it fails and names the conflict.
        self.spec_file.write_text(
            GENERATOR_SPEC.replace("BuildRequires: /usr/bin/rpmbuild\n", "BuildConflicts: rpm-build\n")
        )
        with self.assertRaisesRegex(SystemExit, "BuildConflicts.*rpm-build conflicts with"):
            buildrequires.query(self.specification())


if __name__ == "__main__":
    unittest.main()
