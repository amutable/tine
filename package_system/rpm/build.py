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
import subprocess
import sys
from pathlib import Path

import specs
import util

import builder
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


class Spec(builder.Spec):
    """Buck's generated build invocation, distinct from the package's RPM spec."""

    spec_file: str
    sources: list[str]
    dist: str
    source_date_epoch: int
    release: str
    # Extra switches for a build against `source_tree`, in addition to the common ones below.
    in_place_rpmbuild_options: list[str]
    rpmbuild_options: list[str]
    # `packaging` is the dist-git tree at the commit that `source_tree` pins, or None. Its files replace
    # `sources`.
    packaging: str | None


def build_rpm(spec: Spec) -> int:
    """Build the RPMs described by `spec` in action scratch space."""
    return builder.run(spec, lambda topdir, out, subpackages: _build_rpm(spec, topdir, out, subpackages))


def _build_rpm(spec: Spec, topdir: Path, out: Path, subpackages: Path) -> int:
    for d in ("SOURCES", "SPECS", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True)
    # readonly_project() already created BUILD if it mounted the persistent build directory there.
    (topdir / builder.BUILD).mkdir(exist_ok=True)
    spec_file = Path(spec["spec_file"])
    source_tree = spec["source_tree"]
    checkout = topdir / builder.CHECKOUT
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
        chroot_spec = builder.CHROOT_TOPDIR / builder.CHECKOUT / relative_spec
        sourcedir = chroot_spec.parent
    else:
        source_spec = spec_file
        staged_spec = topdir / "SPECS" / spec_file.name
        chroot_spec = builder.CHROOT_TOPDIR / "SPECS" / spec_file.name
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
    env = os.environ | {
        "HOME": str(builder.CHROOT_TOPDIR),
        "SOURCE_DATE_EPOCH": str(spec["source_date_epoch"]),
    }
    with builder.buildroot(spec, topdir):
        defines = [
            "--define", f"_topdir {builder.CHROOT_TOPDIR}",
            "--define", f"dist {spec['dist']}",
            "--define", "_buildhost reproducible",
            # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
            "--define", "use_source_date_epoch_as_buildtime 1",
        ]  # fmt: skip
        if sourcedir is not None:
            defines += ["--define", f"_sourcedir {sourcedir}"]
        if incremental:
            defines += ["--define", f"_vpath_builddir {builder.CHROOT_TOPDIR / builder.BUILD}"]
        mode = ["-ba"]
        options = spec["rpmbuild_options"] + (_INCREMENTAL_RPMBUILD_OPTIONS if incremental else [])
        cwd = None
        if source_tree is not None:
            mode = ["-bb", "--noprep", "--build-in-place"]
            options += spec["in_place_rpmbuild_options"]
            cwd = builder.CHROOT_TOPDIR / builder.CHECKOUT
            # rpm runs an in-place build in %{builddir}/%{NAME}-%{VERSION}, a symlink to the checkout.
            # find-debuginfo collects the sources under the path of the symlink only. A compiler that ninja
            # or make -C starts in a subdirectory records the real path of the checkout instead. The
            # debugsource package then has no files, and rpmbuild fails. rpm expands the flags when it parses
            # the spec, before it defines %{buildsubdir}. The map therefore names the symlink with the macros
            # that rpm uses to create it. Fedora appends the _distro_extra_* macros to %{build_cflags},
            # %{build_cxxflags} and %{build_fflags} only. A compiler that a spec runs with %{optflags} or
            # $RPM_OPT_FLAGS still records the real path. rustc records the real path as well.
            prefix_map = f"-fdebug-prefix-map={cwd}=%{{builddir}}/%{{NAME}}-%{{VERSION}}"
            for lang in ("cflags", "cxxflags", "fflags"):
                defines += ["--define", f"_distro_extra_{lang} {prefix_map}"]
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
