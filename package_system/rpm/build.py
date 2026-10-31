#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build RPMs inside an assembled, pinned buildroot.

Sources and the RPM spec are staged in action scratch space. Buck keeps the
build directory between runs when the project's dev configuration requests it;
otherwise only the produced RPMs persist. The box sandbox already supplies
isolation around the chroot.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import TypedDict

import specs
import util

import rootfs
import subpackage

# A persistent build directory marks a local iteration build; skip the release-only costs of
# optimization, debug packaging, and payload compression.
_INCREMENTAL_RPMBUILD_OPTIONS = [
    "--without", "lto",
    "--undefine", "_lto_cflags",
    "--undefine", "_annotated_build",
    "--define", "debug_package %{nil}",
    "--define", "_binary_payload w.ufdio",
    "--define", "_source_payload w.ufdio",
]  # fmt: skip


class Spec(TypedDict):
    """Buck's generated build invocation, distinct from the package's RPM spec."""

    # A build directory that survives between runs, or None for a clean build.
    build_dir: str | None
    # A buildroot overlay layer stack (bottom..top); the merged stack is the buildroot.
    lower: list[str]
    spec_file: str
    sources: list[str]
    dist: str
    source_date_epoch: int
    release: str
    out: str
    # The declared binary subpackages, each emitted as `<name>.rpm` into `subpackages_out`.
    subpackages: list[str]
    subpackages_out: str
    # Extra switches for a build against `source_tree`, in addition to the common ones below.
    in_place_rpmbuild_options: list[str]
    rpmbuild_options: list[str]
    # A prepared source tree to build in place, or None to unpack the declared sources through %prep.
    source_tree: str | None
    # `packaging` is the dist-git tree at the commit that `source_tree` pins, or None. Its files replace
    # `sources`.
    packaging: str | None


def build_rpm(spec: Spec) -> int:
    """Build the RPMs described by `spec` in action scratch space."""
    if not spec["lower"]:
        util.fail("build_rpm: the buildroot stack cannot be empty")
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space. Buck clears
    # the scratch space before each execution, so the fixed names below are always free.
    scratch = Path(tempfile.gettempdir())
    topdir = scratch / "topdir"
    # The outputs are mounted next to topdir and not inside it. capture_on_exit() rewrites topdir, and
    # build_rpm() removes topdir after a successful build.
    out, subpackages = scratch / "rpms", scratch / "subpackages"
    with ExitStack() as stack:
        outputs = {Path(spec["out"]): out, Path(spec["subpackages_out"]): subpackages}
        if spec["build_dir"] is not None:
            # The bind of topdir into the buildroot is recursive, so it includes this mount. Without
            # the recursion, rpmbuild would write into the empty directory under the mount.
            outputs[Path(spec["build_dir"])] = topdir / "BUILD"
        stack.enter_context(rootfs.readonly_project(Path.cwd(), outputs))

        with (
            # Buck must be able to remove topdir and the persistent build directory after a failed
            # build. The with statement exits its contexts in reverse order, so capture_on_exit() runs
            # after source_overlay() has unmounted CHECKOUT. Otherwise capture() would walk the
            # merged checkout and copy up every directory whose mode it changes.
            rootfs.capture_on_exit(topdir),
            rootfs.source_overlay(Path(spec["source_tree"]), topdir / "CHECKOUT")
            if spec["source_tree"] is not None
            else nullcontext(),
        ):
            rc = _build_rpm(spec, topdir, out, subpackages)
    if rc == 0:
        shutil.rmtree(topdir)
    return rc


def _build_rpm(spec: Spec, topdir: Path, out: Path, subpackages: Path) -> int:
    for d in ("SOURCES", "SPECS", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True)
    # readonly_project() already created BUILD if it mounted the persistent build directory there.
    (topdir / "BUILD").mkdir(exist_ok=True)
    spec_file = Path(spec["spec_file"])
    source_tree = spec["source_tree"]
    checkout = topdir / "CHECKOUT"
    if source_tree is not None and spec_file.is_relative_to(source_tree):
        relative_spec = spec_file.relative_to(source_tree)
        if ".." in relative_spec.parts:
            util.fail(f"build_rpm: RPM spec must stay within the source tree: {relative_spec}")
        staged_spec = checkout / relative_spec
        # Freezing runs before chroot: an escaping spec symlink must not write into the checkout
        # through another path in the outer sandbox.
        if not staged_spec.resolve().is_relative_to(checkout.resolve()):
            util.fail(f"build_rpm: RPM spec symlink escapes the source tree: {relative_spec}")
        source_spec = staged_spec
        chroot_spec = Path("/build/CHECKOUT") / relative_spec
        sourcedir = chroot_spec.parent
    else:
        source_spec = spec_file
        staged_spec = topdir / "SPECS" / spec_file.name
        chroot_spec = Path("/build/SPECS") / spec_file.name
        sourcedir = None
        sources = [Path(src) for src in spec["sources"]]
        if spec["packaging"] is not None:
            # rpmbuild -bb does not check that Source and Patch files exist, but the spec's scriptlets
            # and macros read them from SOURCES. A dist-git repository contains them at its top level,
            # and its subdirectories, such as tests/, contain no sources.
            sources = sorted(path for path in Path(spec["packaging"]).iterdir() if path.is_file())
        for s in sources:
            # A spec may modify SOURCES, so it must not share the source artifact's inode.
            util.clone_file(s, topdir / "SOURCES" / s.name)

    if not source_spec.is_file():
        util.fail(f"build_rpm: RPM spec does not exist: {spec_file}")

    # Freeze rpmautospec macros so builds need neither Git nor rpmautospec. Keep an in-place spec beside
    # its auxiliary files in the disposable tree, preserving relative includes as well.
    frozen = (
        f"%global autorelease {spec['release']}%{{?dist}}\n%global autochangelog %{{nil}}\n"
    ) + source_spec.read_text()
    staged_spec.write_text(frozen)

    incremental = spec["build_dir"] is not None

    # The ephemeral upper discards buildroot writes; use the package-specific epoch.
    env = os.environ | {"HOME": "/build", "SOURCE_DATE_EPOCH": str(spec["source_date_epoch"])}
    with rootfs.rootfs(
        "/buildroot",
        lowers=spec["lower"],
        binds=[(topdir, "/build")],
        apivfs=True,
        chroot=True,
    ):
        defines = [
            "--define", "_topdir /build",
            "--define", f"dist {spec['dist']}",
            "--define", "_buildhost reproducible",
            # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
            "--define", "use_source_date_epoch_as_buildtime 1",
        ]  # fmt: skip
        if sourcedir is not None:
            defines += ["--define", f"_sourcedir {sourcedir}"]
        if incremental:
            defines += ["--define", "_vpath_builddir /build/BUILD"]
        mode = ["-ba"]
        options = spec["rpmbuild_options"] + (_INCREMENTAL_RPMBUILD_OPTIONS if incremental else [])
        cwd = None
        if source_tree is not None:
            mode = ["-bb", "--noprep", "--build-in-place"]
            options += spec["in_place_rpmbuild_options"]
            cwd = Path("/build/CHECKOUT")
        rc = subprocess.run(
            [
                "/usr/bin/rpmbuild",
                *defines,
                *options,
                *mode,
                "--nocheck",
                "--noclean",
                str(chroot_spec),
            ],
            cwd=cwd,
            env=env,
        ).returncode
    if rc != 0:
        return rc

    # Collect binary packages and, for a regular archive build, the source package.
    # Buck keeps every output of an incremental action, not only its private build directory.
    for previous in [*out.iterdir(), *subpackages.iterdir()]:
        previous.unlink()
    produced: dict[str, Path] = {}  # basename -> path of each binary rpm
    for sub in ("RPMS", "SRPMS"):
        for f in sorted((topdir / sub).rglob("*.rpm")):
            util.clone_file(f, out / f.name, allow_link=True)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    source_output = "" if source_tree is not None else " + srpm"
    print(f"collected {len(produced)} binary rpms{source_output} into {spec['out']}", file=sys.stderr)

    if spec["subpackages"]:
        subpackage.emit(
            {name: subpackages / f"{name}.rpm" for name in spec["subpackages"]},
            produced,
            version=r"-[^-]+-[^-]+\.[^.]+\.rpm",
            # rpmbuild adds debuginfo and debugsource packages on its own.
            generated=r"-debug(info|source)-",
            strict=spec["packaging"] is None,
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse an invocation and build its RPMs."""
    return build_rpm(specs.parse(Spec, "build_rpm", argv))


if __name__ == "__main__":
    raise SystemExit(main())
