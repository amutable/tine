# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Bootstrap a root from a package system's own archives, before its package manager exists.

Every package system's bootstrap extractor answers the same request: a set of packages, or
directories of them, and a root to lay them down in. Which archive format that means opening is
the only part that differs, so the expansion, the ordering, the capture and the invocation happen
here and a driver is left with one package at a time.
"""

import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import specs
from util import fail

import rootfs
import storage


class Spec(TypedDict):
    out: str
    # Packages, or directories of them.
    packages: list[str]


def expand(paths: list[str]) -> list[Path]:
    """Expand any directory argument to the (sorted) files it contains."""
    found: list[Path] = []
    for path in (Path(package) for package in paths):
        found += sorted(path.iterdir()) if path.is_dir() else [path]
    return found


def run(prog: str, unpack: Callable[[Path, Path], int], argv: list[str] | None = None) -> None:
    """Unpack everything this invocation names into the root it names.

    The callback receives one package and the destination, and returns how many entries it wrote.
    """
    spec = specs.parse(Spec, prog, argv)
    dest = Path(spec["out"])
    packages = expand(spec["packages"])
    if not packages:
        fail(f"{prog}: no packages to extract")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
        tree, snapshot = Path(scratch) / "root", Path(scratch) / "snapshot"
        tree.mkdir()
        total = sum(unpack(package, tree) for package in packages)
        # The extract action doesn't run as root, and packages install files such as /etc/gshadow with
        # mode 0000. Capture reads every file, and the Buck tree keeps only the executable bit anyway.
        for directory, directories, files in tree.walk():
            for name in directories + files:
                path = directory / name
                st = path.lstat()
                if stat.S_ISDIR(st.st_mode):
                    path.chmod(0o700)
                elif not stat.S_ISLNK(st.st_mode):
                    path.chmod(stat.S_IMODE(st.st_mode) | stat.S_IRUSR)
        # stage1 is a box layer, so it is written from a snapshot like every other box layer. Buck
        # stores no mtimes, so the epoch the capture clamps them to doesn't matter.
        storage.capture(storage.Root(tree), snapshot, epoch=0)
        rootfs.write_buck_tree(storage.Snapshot([snapshot]), dest)
    print(f"extracted {total} entries from {len(packages)} package(s) into {dest}", file=sys.stderr)
