#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# One image's UAPI.16 file manifest, checked against the format's own rules and against what the
# image is known to ship. The driver's unit suite covers how each inode type is encoded; this
# asserts that the artifact a real build produced is a well-formed sequence describing that tree.

import base64
import json
import sys
from pathlib import Path
from typing import Any


def decode(data: bytes) -> list[dict[str, Any]]:
    """Split an RFC7464 JSON-SEQ stream the way a consumer has to."""
    head, separator, rest = data.partition(b"\x1e")
    assert separator, "no record separator: not a JSON-SEQ stream"
    assert head == b"", f"the stream begins with {head!r} rather than a record"
    objects = []
    for record in rest.split(b"\x1e"):
        assert record.endswith(b"\n"), f"record not terminated by a line feed: {record[:60]!r}"
        objects.append(json.loads(record.decode("utf-8")))
    return objects


objects = decode(Path(sys.argv[1]).read_bytes())
assert objects[-1] == {"mediaType": "application/vnd.uapi.16.trailer"}, "missing trailer"
root, files = objects[0], objects[1:-1]
assert root.get("mediaType") == "application/vnd.uapi.16.manifest" and "name" not in root, root
assert files, "the manifest lists no files at all"

names = [obj["name"] for obj in files]
assert len(set(names)) == len(names), "a name is listed twice"

# A directory's contents follow it immediately, so the parent of every entry is the innermost
# directory still open above it, and the top level is reached with none open at all.
open_dirs: list[str] = []
for obj in files:
    parent = obj["name"].rpartition("/")[0]
    while open_dirs and open_dirs[-1] != parent:
        open_dirs.pop()
    innermost = open_dirs[-1] if open_dirs else ""
    assert innermost == parent, f"{obj['name']} does not follow its parent directory"
    if obj["type"] == "dir":
        open_dirs.append(obj["name"])

by_name = {obj["name"]: obj for obj in files}

# What this image was told to install, listed as the regular file it is.
bash = by_name["usr/bin/bash"]
assert bash["type"] == "reg", bash
assert bash["size"] > 0, bash
assert len(bash["sha256"]) == 64, bash
assert bash["mode"] & 0o111, bash

# The usr-merge symlink the distribution ships, carrying its target inline.
link = by_name["bin"]
assert link["type"] == "lnk", link
assert base64.b64decode(link["contents"][0]["literal"]) == b"usr/bin", link
assert link["size"] == len("usr/bin"), link

directory = by_name["usr/bin"]
assert directory["type"] == "dir", directory
assert "sha256" not in directory and "size" not in directory, directory

print(f"manifest: {len(files)} entries, {sum(obj['type'] == 'reg' for obj in files)} regular files")
