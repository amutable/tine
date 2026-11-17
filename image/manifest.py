#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Export the finalized image's UAPI.16 listing."""

import sys
from pathlib import Path

import specs

import finalize
import storage
import uapi16


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "manifest", argv)

    out = Path(spec["out"])
    written = uapi16.write_listing(storage.Snapshot(spec["lower"]).objects, out)
    print(f"manifest: wrote {written} objects -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
