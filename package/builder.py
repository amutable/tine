# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run a native package build in action scratch space, with the project read-only.

The build drivers of the package systems differ only in the tool that builds the packages. This module
sets up their scratch tree and mounts, and removes the tree after a successful build.
"""

import shutil
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TypedDict

import util

import rootfs
import storage

# The source tree is mounted at topdir/CHECKOUT, and the persistent build directory at topdir/BUILD.
CHECKOUT = "CHECKOUT"
BUILD = "BUILD"
CHROOT_TOPDIR = Path("/build")


class Spec(TypedDict):
    # `build_dir` is a build directory that Buck keeps between runs, or None for a clean build.
    build_dir: str | None
    # `lower` lists the snapshot stores of the buildroot, lowest first.
    lower: list[str]
    out: str
    # `source_tree` is a prepared source tree to build in place, or None to build from the declared sources.
    source_tree: str | None
    # The declared binary subpackages. The driver copies each one into `subpackages_out`.
    subpackages: list[str]
    subpackages_out: str


def run(spec: Spec, build: Callable[[Path, Path, Path], int]) -> int:
    """Run `build` in a scratch tree and return its exit status.

    `build` receives topdir and the empty mount points of `out` and `subpackages_out`. `build_dir` is mounted
    at `topdir/BUILD`, and `source_tree` at `topdir/CHECKOUT` through an overlay that discards writes.
    """
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space. Buck clears
    # the scratch space before each execution, so the fixed names below are always free.
    scratch = Path(tempfile.gettempdir())
    topdir = scratch / "topdir"
    topdir.mkdir()
    # The outputs are mounted next to topdir and not inside it. capture_on_exit() rewrites topdir, and
    # topdir is removed after a successful build.
    out, subpackages = scratch / "packages", scratch / "subpackages"
    outputs = {Path(spec["out"]): out, Path(spec["subpackages_out"]): subpackages}
    if spec["build_dir"] is not None:
        # buildroot() binds topdir into the chroot recursively, so the bind includes this mount. Without
        # the recursion, the build would write into the empty directory under the mount.
        outputs[Path(spec["build_dir"])] = topdir / BUILD
    source_tree = spec["source_tree"]
    with rootfs.readonly_project(Path.cwd(), outputs):
        util.remove_previous_binaries(out)
        util.remove_previous_binaries(subpackages)
        with (
            # Buck must be able to remove topdir and the persistent build directory after a failed build.
            # The with statement exits its contexts in reverse order, so capture_on_exit() runs after
            # source_overlay() has unmounted CHECKOUT. Otherwise capture() would walk the merged checkout
            # and copy up every directory whose mode it changes.
            rootfs.capture_on_exit(topdir),
            rootfs.source_overlay(Path(source_tree), topdir / CHECKOUT)
            if source_tree is not None
            else nullcontext(),
        ):
            rc = build(topdir, out, subpackages)
    if rc == 0:
        shutil.rmtree(topdir)
    return rc


@contextmanager
def buildroot(lower: list[str], topdir: Path) -> Generator[Path]:
    """Chroot into the buildroot that the snapshot stores `lower` restore, with topdir at `CHROOT_TOPDIR`."""
    with (
        storage.root(storage.Snapshot(lower)) as restored,
        rootfs.rootfs(
            "/buildroot", bind=restored.path, binds=[(topdir, CHROOT_TOPDIR)], apivfs=True, chroot=True
        ) as root,
    ):
        yield root
