# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Stage an RPM spec in an rpmbuild tree, and assemble the arguments that rpmbuild parses it with."""

from pathlib import Path
from typing import NamedTuple, TypedDict

import util

import builder

# A persistent build directory means a local iteration build. Such a build skips optimization, debug
# packages and payload compression, which only a release needs.
INCREMENTAL_OPTIONS = [
    "--without", "lto",
    "--undefine", "_lto_cflags",
    "--undefine", "_annotated_build",
    "--define", "debug_package %{nil}",
    "--define", "_binary_payload w.ufdio",
    "--define", "_source_payload w.ufdio",
]  # fmt: skip


class Parse(TypedDict):
    """The fields of a driver spec that select how rpm parses the RPM spec."""

    spec_file: str
    # `sources` lists the Source and Patch files, and directories whose top-level files are Source and Patch
    # files.
    sources: list[str]
    dist: str
    source_date_epoch: int
    release: str
    rpmbuild_options: list[str]
    # Extra switches for a build against a source tree, after the common ones.
    in_place_rpmbuild_options: list[str]
    # `incremental` is True for a dev build, which keeps its build directory and adds INCREMENTAL_OPTIONS.
    incremental: bool


class Staged(NamedTuple):
    # `spec` is the path of the frozen spec in the chroot.
    spec: Path
    # `sourcedir` is the %_sourcedir of a spec in the source tree, or None for the default SOURCES.
    sourcedir: Path | None
    # `cwd` is the directory that rpmbuild runs in, or None for the root of the chroot.
    cwd: Path | None


def reject_symlinks(path: Path, what: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            util.fail(f"rpmbuild: {what} is a symlink: {component}")


def source_files(paths: list[str]) -> list[Path]:
    """Return the Source and Patch files of `paths`, with each directory replaced by its top-level files.

    A dist-git repository contains the sources at its top level. Its subdirectories, such as tests/,
    contain no sources.
    """
    files: list[Path] = []
    for path in (Path(path) for path in paths):
        # The copy into SOURCES follows symlinks, and it runs outside the chroot where the project is
        # readable. A symlink in a fetched or unpacked tree, or a sub-target of one, would put a file of
        # the project into the package.
        reject_symlinks(path, "the source")
        if not path.is_dir():
            files.append(path)
            continue
        if links := sorted(file.name for file in path.iterdir() if file.is_symlink()):
            util.fail(f"rpmbuild: {path} contains symlinks at its top level: {', '.join(links)}")
        files += sorted(file for file in path.iterdir() if file.is_file())
    return files


def stage(topdir: Path, parse: Parse, source_tree: str | None) -> Staged:
    """Stage the spec and the sources of `parse` in the rpmbuild tree `topdir`.

    A source tree has to be mounted at `topdir/CHECKOUT` already.
    """
    for d in ("SOURCES", "SPECS", "BUILDROOT", "RPMS", "SRPMS"):
        (topdir / d).mkdir(parents=True)
    # readonly_project() already created BUILD if it mounted the persistent build directory there.
    (topdir / builder.BUILD).mkdir(exist_ok=True)
    checkout = topdir / builder.CHECKOUT
    spec_file = Path(parse["spec_file"])
    if source_tree is not None and spec_file.is_relative_to(source_tree):
        relative_spec = spec_file.relative_to(source_tree)
        if ".." in relative_spec.parts:
            util.fail(f"rpmbuild: RPM spec must stay within the source tree: {relative_spec}")
        staged_spec = checkout / relative_spec
        # The spec is frozen before the chroot. A spec symlink that escapes the checkout would otherwise
        # make the freeze write to a file of the outer sandbox.
        if not staged_spec.resolve().is_relative_to(checkout.resolve()):
            util.fail(f"rpmbuild: RPM spec symlink escapes the source tree: {relative_spec}")
        source_spec = staged_spec
        chroot_spec = builder.CHROOT_TOPDIR / builder.CHECKOUT / relative_spec
        sourcedir = chroot_spec.parent
    else:
        # The spec is read outside the chroot. A spec symlink in a fetched tree would read a file of the
        # project.
        reject_symlinks(spec_file, "RPM spec")
        source_spec = spec_file
        staged_spec = topdir / "SPECS" / spec_file.name
        chroot_spec = builder.CHROOT_TOPDIR / "SPECS" / spec_file.name
        sourcedir = None
        for s in source_files(parse["sources"]):
            # A spec may modify SOURCES, so it must not share the source artifact's inode.
            util.clone_file(s, topdir / "SOURCES" / s.name)

    if not source_spec.is_file():
        util.fail(f"rpmbuild: RPM spec does not exist: {spec_file}")

    # The frozen rpmautospec macros let the spec parse without Git or rpmautospec. An in-place spec stays
    # beside its auxiliary files in the disposable tree, so that its relative includes still resolve.
    staged_spec.write_text(
        f"%global autorelease {parse['release']}%{{?dist}}\n%global autochangelog %{{nil}}\n"
        + source_spec.read_text()
    )
    cwd = builder.CHROOT_TOPDIR / builder.CHECKOUT if source_tree is not None else None
    return Staged(chroot_spec, sourcedir, cwd)


def environment(source_date_epoch: int) -> dict[str, str]:
    return {"HOME": str(builder.CHROOT_TOPDIR), "SOURCE_DATE_EPOCH": str(source_date_epoch)}


def arguments(staged: Staged, parse: Parse) -> list[str]:
    """Return the macro definitions and options that rpmbuild parses the spec `staged` with."""
    arguments: list[str] = [
        "--define", f"_topdir {builder.CHROOT_TOPDIR}",
        "--define", f"dist {parse['dist']}",
        "--define", "_buildhost reproducible",
        # rpm otherwise ignores SOURCE_DATE_EPOCH for the BUILDTIME header.
        "--define", "use_source_date_epoch_as_buildtime 1",
    ]  # fmt: skip
    if staged.sourcedir is not None:
        arguments += ["--define", f"_sourcedir {staged.sourcedir}"]
    if parse["incremental"]:
        arguments += ["--define", f"_vpath_builddir {builder.CHROOT_TOPDIR / builder.BUILD}"]
    if staged.cwd is not None:
        # rpm runs an in-place build in %{builddir}/%{NAME}-%{VERSION}, a symlink to the checkout.
        # find-debuginfo collects the sources under the path of the symlink only. A compiler that ninja
        # or make -C starts in a subdirectory records the real path of the checkout instead. The
        # debugsource package then has no files, and rpmbuild fails. rpm expands the flags when it parses
        # the spec, before it defines %{buildsubdir}. The map therefore names the symlink with the macros
        # that rpm uses to create it. Fedora appends the _distro_extra_* macros to %{build_cflags},
        # %{build_cxxflags} and %{build_fflags} only. A compiler that a spec runs with %{optflags} or
        # $RPM_OPT_FLAGS still records the real path. rustc records the real path as well.
        prefix_map = f"-fdebug-prefix-map={staged.cwd}=%{{builddir}}/%{{NAME}}-%{{VERSION}}"
        for lang in ("cflags", "cxxflags", "fflags"):
            arguments += ["--define", f"_distro_extra_{lang} {prefix_map}"]
    arguments += parse["rpmbuild_options"] + (INCREMENTAL_OPTIONS if parse["incremental"] else [])
    return arguments + parse["in_place_rpmbuild_options"]
