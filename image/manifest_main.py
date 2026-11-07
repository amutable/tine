#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Export the finalized image's UAPI.16 listing."""

import os
import sys
from pathlib import Path

import specs

import finalize
import uapi16


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "manifest", argv)

    out = Path(spec["out"])
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    with finalize.image(spec, program="manifest") as tree:
        written = uapi16.write(tree, out, epoch)
    print(f"manifest: wrote {written} objects -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
