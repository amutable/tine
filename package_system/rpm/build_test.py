# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the RPM build driver."""

import contextlib
import errno
import io
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast, override
from unittest import mock

import build


class BuildRpm(unittest.TestCase):
    """Exercise the archive and prepared-source-tree build modes."""

    @override
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="tine-rpm-build-test-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.scratch)
        self.spec_file = self.scratch / "example.spec"
        self.spec_file.write_text("Name: example\n")
        # Buck runs the driver in the project. The declared outputs are in the project, and scratch is
        # outside it.
        self.project = self.scratch / "project"
        self.project.mkdir()
        self.auxiliary = self.scratch / "auxiliary"
        self.auxiliary.write_text("packaging input\n")

    def specification(
        self,
        source_tree: Path | None = None,
        *,
        build_dir: Path | None = None,
        spec_file: Path | None = None,
        subpackages: list[str] | None = None,
        sources: list[Path] | None = None,
        strict_subpackages: bool = True,
    ) -> build.Spec:
        """Return a complete driver spec for one test build."""
        return build.Spec(
            build_dir=str(build_dir) if build_dir is not None else None,
            lower=["buildroot"],
            spec_file=str(spec_file or self.spec_file),
            sources=[str(source) for source in sources] if sources is not None else [str(self.auxiliary)],
            dist=".test",
            # rpm_package keeps the build directory of every dev build, and only of a dev build.
            incremental=build_dir is not None,
            source_date_epoch=1234567890,
            release="7",
            out=str(self.project / "out"),
            subpackages=subpackages or [],
            subpackages_out=str(self.project / "subpackages-out"),
            # The rule passes the in-place options only to a build with a source tree.
            in_place_rpmbuild_options=["--define", "local_option yes"] if source_tree is not None else [],
            rpmbuild_options=["--define", "common_option yes"],
            source_tree=str(source_tree) if source_tree is not None else None,
            strict_subpackages=strict_subpackages,
        )

    def invoke(
        self, spec: build.Spec, inspect: Callable[[Path], None] | None = None
    ) -> tuple[list[str], Path | None, dict[str, str], Path, list[tuple[str | Path, str | Path]]]:
        """Run the driver with its isolation and rpmbuild process observed."""
        topdir = self.scratch / "topdir"
        completed = subprocess.CompletedProcess[str]([], 0)

        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            # The project is read-only while rpmbuild runs, for a relative path as well.
            for path in (self.project / "probe", Path("probe")):
                with self.assertRaises(OSError) as caught:
                    path.touch()
                self.assertEqual(caught.exception.errno, errno.EROFS)
            if inspect is not None:
                inspect(topdir)
            return completed

        with (
            contextlib.chdir(self.project),
            mock.patch.object(
                build.builder.rootfs, "rootfs", return_value=contextlib.nullcontext()
            ) as mounted,
            mock.patch.object(build.subprocess, "run", side_effect=run) as process,
            # Patch the module wrapper first so TemporaryDirectory still uses the real rmtree.
            mock.patch.object(build.builder, "shutil", mock.Mock(wraps=shutil)),
            mock.patch.object(build.builder.shutil, "rmtree") as remove,
            mock.patch.object(tempfile, "tempdir", str(self.scratch)),
        ):
            self.assertEqual(build.build_rpm(spec), 0)
        remove.assert_called_once_with(topdir)
        (self.project / "probe").touch()
        command = cast(list[str], process.call_args.args[0])
        cwd = cast(Path | None, process.call_args.kwargs["cwd"])
        environment = cast(dict[str, str], process.call_args.kwargs["env"])
        binds = cast(list[tuple[str | Path, str | Path]], mounted.call_args.kwargs["binds"])
        return command, cwd, environment, topdir, binds

    def test_archive_build_runs_prep_and_produces_an_srpm(self) -> None:
        command, cwd, environment, topdir, binds = self.invoke(self.specification())

        self.assertIn("-ba", command)
        self.assertNotIn("--build-in-place", command)
        self.assertNotIn("local_option yes", command)
        self.assertNotIn("_binary_payload w.ufdio", command)
        self.assertNotIn("_source_payload w.ufdio", command)
        self.assertIsNone(cwd)
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], "1234567890")
        self.assertNotIn("_annotated_build", command)
        self.assertEqual(binds, [(topdir, Path("/build"))])
        self.assertEqual((topdir / "SOURCES" / "auxiliary").read_text(), "packaging input\n")
        frozen = (topdir / "SPECS" / "example.spec").read_text()
        self.assertTrue(frozen.startswith("%global autorelease 7%{?dist}\n%global autochangelog %{nil}\n"))

    def test_source_tree_builds_in_place_from_a_disposable_overlay(self) -> None:
        source = self.scratch / "checkout"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "payload").write_text("local checkout\n")
        (source / "nested" / "parent").symlink_to("..")

        def inspect(topdir: Path) -> None:
            self.assertEqual((topdir / "CHECKOUT" / "nested" / "payload").read_text(), "local checkout\n")
            self.assertEqual((topdir / "CHECKOUT" / "nested" / "parent").readlink(), Path(".."))

        command, cwd, _environment, topdir, binds = self.invoke(self.specification(source), inspect)

        self.assertIn("-bb", command)
        self.assertIn("--noprep", command)
        self.assertIn("--build-in-place", command)
        prefix_map = "-fdebug-prefix-map=/build/CHECKOUT=%{builddir}/%{NAME}-%{VERSION}"
        for lang in ("cflags", "cxxflags", "fflags"):
            self.assertIn(f"_distro_extra_{lang} {prefix_map}", command)
        self.assertIn("common_option yes", command)
        self.assertIn("local_option yes", command)
        self.assertNotIn("_binary_payload w.ufdio", command)
        self.assertNotIn("_source_payload w.ufdio", command)
        self.assertNotIn("_lto_cflags", command)
        self.assertNotIn("_annotated_build", command)
        self.assertNotIn("debug_package %{nil}", command)
        self.assertNotIn("-ba", command)
        self.assertEqual(command[-1], "/build/SPECS/example.spec")
        self.assertEqual(binds, [(topdir, Path("/build"))])
        self.assertEqual(cwd, Path("/build/CHECKOUT"))
        self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])
        self.assertEqual((topdir / "SOURCES" / "auxiliary").read_text(), "packaging input\n")
        self.assertEqual((source / "nested" / "payload").read_text(), "local checkout\n")

    def test_in_place_spec_and_extras_come_from_the_source_tree(self) -> None:
        source = self.scratch / "checkout"
        packaging = source / "packaging" / "fedora"
        packaging.mkdir(parents=True)
        local_spec = packaging / "local.spec"
        local_spec.write_text("Name: from-checkout\nSource1: helper\n")
        (packaging / "helper").write_text("checkout helper\n")

        specification = self.specification(source, spec_file=local_spec)

        def inspect(topdir: Path) -> None:
            frozen = (topdir / "SPECS" / "local.spec").read_text()
            self.assertTrue(
                frozen.startswith("%global autorelease 7%{?dist}\n%global autochangelog %{nil}\n")
            )
            self.assertIn("Name: from-checkout\n", frozen)
            self.assertEqual(
                (topdir / "CHECKOUT" / "packaging" / "fedora" / "local.spec").read_text(),
                "Name: from-checkout\nSource1: helper\n",
            )
            self.assertEqual(
                (topdir / "CHECKOUT" / "packaging" / "fedora" / "helper").read_text(),
                "checkout helper\n",
            )

        command, cwd, _environment, topdir, _binds = self.invoke(specification, inspect)

        self.assertEqual(command[-1], "/build/SPECS/local.spec")
        self.assertIn("_sourcedir /build/CHECKOUT/packaging/fedora", command)
        self.assertEqual(cwd, Path("/build/CHECKOUT"))
        self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])
        self.assertFalse((topdir / "SOURCES" / "auxiliary").exists())
        self.assertEqual(local_spec.read_text(), "Name: from-checkout\nSource1: helper\n")

    def pinned_packaging(self) -> tuple[Path, Path]:
        """Return a checkout and a dist-git tree with a spec, a patch and a directory of tests."""
        source = self.scratch / "checkout"
        source.mkdir()
        packaging = self.project / "packaging"
        (packaging / "tests").mkdir(parents=True)
        (packaging / "example.spec").write_text("Name: pinned\nPatch0: fix.patch\n")
        (packaging / "fix.patch").write_text("pinned patch\n")
        (packaging / "tests" / "main.fmf").write_text("summary: test\n")
        return source, packaging

    def test_pinned_packaging_replaces_the_declared_spec_and_sources(self) -> None:
        source, packaging = self.pinned_packaging()
        specification = self.specification(
            source, spec_file=packaging / "example.spec", sources=[packaging], strict_subpackages=False
        )

        command, _cwd, _environment, topdir, _binds = self.invoke(specification)

        self.assertEqual(command[-1], "/build/SPECS/example.spec")
        self.assertFalse([argument for argument in command if argument.startswith("_sourcedir")])
        self.assertIn("Name: pinned\n", (topdir / "SPECS" / "example.spec").read_text())
        self.assertEqual(
            sorted(path.name for path in (topdir / "SOURCES").iterdir()), ["example.spec", "fix.patch"]
        )
        self.assertEqual((topdir / "SOURCES" / "fix.patch").read_text(), "pinned patch\n")

    def produce(self, *names: str) -> Callable[[Path], None]:
        def produce(topdir: Path) -> None:
            built = topdir / "RPMS/x86_64"
            built.mkdir(parents=True)
            for name in names:
                (built / f"{name}-1-7.test.x86_64.rpm").write_text(f"{name}\n")

        return produce

    def test_pinned_packaging_reports_a_subpackage_mismatch_without_failing(self) -> None:
        source, packaging = self.pinned_packaging()
        specification = self.specification(
            source,
            spec_file=packaging / "example.spec",
            sources=[packaging],
            strict_subpackages=False,
            subpackages=["example", "example-imds"],
        )

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.invoke(specification, self.produce("example", "example-ukify"))

        self.assertIn("declared but not produced: ['example-imds']", stderr.getvalue())
        self.assertIn("produced but not declared: ['example-ukify-1-7.test.x86_64.rpm']", stderr.getvalue())
        subpackages = self.project / "subpackages-out"
        self.assertEqual(sorted(path.name for path in subpackages.iterdir()), ["example.rpm"])
        self.assertEqual((subpackages / "example.rpm").read_text(), "example\n")

    def test_imported_packaging_fails_on_a_subpackage_mismatch(self) -> None:
        specification = self.specification(None, subpackages=["example", "example-imds"])
        with self.assertRaisesRegex(SystemExit, r"declared but not produced: \['example-imds'\]"):
            self.invoke(specification, self.produce("example"))

    def test_spec_path_cannot_escape_the_source_tree(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        spec = self.specification(source, spec_file=source / ".." / self.spec_file.name)
        with (
            contextlib.chdir(self.project),
            mock.patch.object(tempfile, "tempdir", str(self.scratch)),
            self.assertRaisesRegex(SystemExit, "RPM spec must stay within the source tree"),
        ):
            build.build_rpm(spec)
        self.assertEqual(self.spec_file.read_text(), "Name: example\n")

    def test_missing_spec_has_a_useful_error(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        for spec_file in (source / "missing.spec", self.scratch / "missing.spec"):
            with self.subTest(spec_file=spec_file):
                spec = self.specification(source, spec_file=spec_file)
                case = tempfile.mkdtemp(dir=self.scratch)
                with (
                    contextlib.chdir(self.project),
                    mock.patch.object(tempfile, "tempdir", case),
                    self.assertRaisesRegex(SystemExit, "RPM spec does not exist: .*missing.spec"),
                ):
                    build.build_rpm(spec)

    def test_packages_cross_into_the_mounted_outputs(self) -> None:
        def produce(topdir: Path) -> None:
            built = topdir / "RPMS/x86_64"
            built.mkdir(parents=True)
            (built / "example-1-7.test.x86_64.rpm").write_text("package\n")

        self.invoke(self.specification(subpackages=["example"]), produce)

        out = self.project / "out"
        self.assertEqual((out / "example-1-7.test.x86_64.rpm").read_text(), "package\n")
        self.assertEqual((self.project / "subpackages-out/example.rpm").read_text(), "package\n")

    def test_dev_source_keeps_build_state_and_uses_the_incremental_profile(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        build_dir = self.project / "incremental"
        build_dir.mkdir()
        (build_dir / "cached-object").write_text("keep\n")
        out = self.project / "out"
        out.mkdir()
        (out / "stale.rpm").write_text("old\n")
        subpackages = self.project / "subpackages-out"
        subpackages.mkdir()
        (subpackages / "stale.rpm").write_text("old\n")

        command, _cwd, _environment, topdir, binds = self.invoke(
            self.specification(source, build_dir=build_dir)
        )

        self.assertIn("lto", command)
        self.assertIn("_lto_cflags", command)
        self.assertIn("_annotated_build", command)
        self.assertIn("debug_package %{nil}", command)
        self.assertIn("_binary_payload w.ufdio", command)
        self.assertIn("_source_payload w.ufdio", command)
        self.assertIn("_vpath_builddir /build/BUILD", command)
        self.assertIn("local_option yes", command)
        self.assertLess(command.index("debug_package %{nil}"), command.index("local_option yes"))
        self.assertEqual(binds, [(topdir, Path("/build"))])
        self.assertEqual((build_dir / "cached-object").read_text(), "keep\n")
        self.assertFalse((out / "stale.rpm").exists())
        self.assertFalse((subpackages / "stale.rpm").exists())

    def test_dev_archive_uses_the_incremental_profile(self) -> None:
        build_dir = self.project / "incremental"

        command, cwd, _environment, topdir, binds = self.invoke(self.specification(build_dir=build_dir))

        self.assertIn("-ba", command)
        self.assertNotIn("--build-in-place", command)
        self.assertIn("_lto_cflags", command)
        self.assertIn("_annotated_build", command)
        self.assertIn("debug_package %{nil}", command)
        self.assertIn("_binary_payload w.ufdio", command)
        self.assertIn("_source_payload w.ufdio", command)
        self.assertIn("_vpath_builddir /build/BUILD", command)
        self.assertNotIn("local_option yes", command)
        self.assertIsNone(cwd)
        self.assertEqual(binds, [(topdir, Path("/build"))])

    def test_source_writes_are_discarded_and_build_state_survives_failure(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        (source / "original").write_text("checkout\n")
        build_dir = self.project / "persistent"
        spec = self.specification(source, build_dir=build_dir)
        for index, outcome in enumerate((0, 1, RuntimeError("interrupted"), 0)):
            with self.subTest(outcome=outcome):
                case = self.scratch / str(index)
                case.mkdir()
                topdir = case / "topdir"

                def run(
                    *_args: object,
                    stage: Path = topdir,
                    iteration: int = index,
                    result: int | RuntimeError = outcome,
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    merged = stage / "CHECKOUT"
                    self.assertFalse((merged / "generated").exists())
                    (merged / "original").unlink()
                    (merged / "generated").write_text("discard\n")
                    state = stage / "BUILD" / "state"
                    self.assertEqual(state.read_text() if state.exists() else "0", str(iteration))
                    state.write_text(str(iteration + 1))
                    if isinstance(result, RuntimeError):
                        raise result
                    return subprocess.CompletedProcess([], result)

                with (
                    contextlib.chdir(self.project),
                    mock.patch.object(build.builder.rootfs, "rootfs", return_value=contextlib.nullcontext()),
                    mock.patch.object(build.subprocess, "run", side_effect=run),
                    mock.patch.object(tempfile, "tempdir", str(case)),
                ):
                    if isinstance(outcome, RuntimeError):
                        with self.assertRaisesRegex(RuntimeError, "interrupted"):
                            build.build_rpm(spec)
                    else:
                        self.assertEqual(build.build_rpm(spec), outcome)
                self.assertEqual((source / "original").read_text(), "checkout\n")
                self.assertFalse((source / "generated").exists())
                if outcome == 0:
                    self.assertFalse(topdir.exists())
                else:
                    self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])

    def test_spec_symlink_cannot_escape_the_staged_checkout(self) -> None:
        source = self.scratch / "checkout"
        source.mkdir()
        (source / "escape.spec").symlink_to(self.spec_file)
        spec = self.specification(source, spec_file=source / "escape.spec")
        topdir = self.scratch / "topdir"
        with (
            contextlib.chdir(self.project),
            mock.patch.object(tempfile, "tempdir", str(self.scratch)),
            self.assertRaisesRegex(SystemExit, "spec symlink escapes"),
        ):
            build.build_rpm(spec)
        self.assertEqual(self.spec_file.read_text(), "Name: example\n")
        self.assertEqual(list((topdir / "CHECKOUT").iterdir()), [])

    def test_capture_makes_build_trees_removable_after_unmount(self) -> None:
        for persistent in (False, True):
            for outcome in (0, 1, RuntimeError("rpmbuild interrupted")):
                with self.subTest(persistent=persistent, outcome=outcome):
                    case = Path(tempfile.mkdtemp(dir=self.scratch))
                    topdir = case / "topdir"
                    # The persistent build directory is an output, so it is in the project. rpmbuild
                    # writes to it through its mount at topdir/BUILD while the project is read-only.
                    build_dir = Path(tempfile.mkdtemp(dir=self.project)) / "state" if persistent else None
                    directories = [topdir / "BUILDROOT" / "usr"]
                    if build_dir is not None:
                        directories.append(topdir / "BUILD" / "installed")
                    # The build directory content is at `build_dir` again after the unmount.
                    captured = list(directories)
                    if build_dir is not None:
                        captured[-1] = build_dir / "installed"

                    def run(
                        *_args: object,
                        trees: list[Path] = directories,
                        result: int | RuntimeError = outcome,
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        for directory in trees:
                            directory.mkdir(parents=True)
                            (directory / "payload").write_text("keep\n")
                            directory.chmod(0o555)
                        if isinstance(result, RuntimeError):
                            raise result
                        return subprocess.CompletedProcess([], result)

                    @contextlib.contextmanager
                    def mounted(trees: list[Path] = directories) -> Generator[None]:
                        try:
                            yield
                        finally:
                            for directory in trees:
                                self.assertEqual(directory.stat().st_mode & 0o777, 0o555)

                    with (
                        contextlib.chdir(self.project),
                        mock.patch.object(build.builder.rootfs, "rootfs", return_value=mounted()),
                        mock.patch.object(build.subprocess, "run", side_effect=run),
                        mock.patch.object(build.builder.shutil, "rmtree") as remove,
                        mock.patch.object(tempfile, "tempdir", str(case)),
                    ):
                        spec = self.specification(build_dir=build_dir)
                        if isinstance(outcome, RuntimeError):
                            with self.assertRaises(RuntimeError) as raised:
                                build.build_rpm(spec)
                            self.assertIs(raised.exception, outcome)
                        else:
                            self.assertEqual(build.build_rpm(spec), outcome)

                    for directory in captured:
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                        self.assertEqual((directory / "payload").read_text(), "keep\n")
                    if outcome == 0:
                        remove.assert_called_once_with(topdir)
                    else:
                        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
