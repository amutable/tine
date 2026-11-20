#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check that two buildroots installed from the same inputs are the same tree.

Each argument holds the space-separated layers of one buildroot, lowest first. The two buildroots share
the base root, and each package installs its BuildRequires in deltas of its own. Each pair of deltas
must agree in everything that Buck hashes of a tree. On a mismatch, name the paths that differ, which
is what anyone hunting the cause needs.
"""

import hashlib
import sys
from pathlib import Path


def tree(root: Path) -> dict[str, tuple[object, ...]]:
    """Map each path below `root` to what Buck hashes of it."""
    result: dict[str, tuple[object, ...]] = {}
    for path in root.rglob("*"):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ("symlink", path.readlink())
        elif path.is_dir():
            result[name] = ("directory",)
        else:
            with path.open("rb") as content:
                digest = hashlib.file_digest(content, "sha256").hexdigest()
            result[name] = ("file", bool(path.stat().st_mode & 0o111), digest)
    return result


first, second = (argument.split() for argument in sys.argv[1:])
if len(first) != len(second):
    sys.exit(f"the buildroots have different layers: {first} and {second}")
differing = []
for layer_a, layer_b in zip(first, second, strict=True):
    # The shared base root is one artifact.
    if layer_a == layer_b:
        continue
    left, right = tree(Path(layer_a)), tree(Path(layer_b))
    if names := sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name)):
        differing.append(f"{layer_a} and {layer_b} differ in {', '.join(names)}")
if differing:
    sys.exit("\n".join(differing))
print(f"buildroot: {len(first)} identical layers")
