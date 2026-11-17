#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Copy the dpkg database out of a logical image as a separate, trimmed artifact.

The image does not ship the database, and SBOM tools and security scanners need it. The dpkg
database is the file `status`, which describes every installed package, and the directory `info`
with several files per package. The output is a reproducible tar of `status` and `info`.

From `info`, only the `.list` files are kept, which name the files that a package owns. The
maintainer scripts are code of the package and describe nothing. The `.md5sums` files are large,
and only dpkg reads them, to verify an installed tree.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import debfile
import specs
import util

import finalize
import tar


class Spec(finalize.ImageSpec):
    out: str


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "pkgdb", argv)

    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    out = Path(spec["out"])
    with finalize.image(spec) as tree:
        source = tree / debfile.ADMINDIR
        status = source / "status"
        if not status.is_file():
            util.fail(f"no dpkg database at {source}; the image has no installed packages")
        listings = sorted((source / "info").glob("*.list"))
        if not listings:
            util.fail(f"no dpkg file lists under {source}")
        # The scratch directory is on the filesystem of the output, so that the copies can be
        # reflinks. It is removed before the action ends, so Buck only sees the finished file.
        with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
            trimmed = Path(scratch) / "dpkg"
            (trimmed / "info").mkdir(parents=True)
            shutil.copy2(status, trimmed / "status")
            for listing in listings:
                shutil.copy2(listing, trimmed / "info" / listing.name)
            packed = Path(scratch) / out.name
            tar.pack_tree(trimmed, packed, epoch)
            util.compress_zstd(packed, out)
    print(f"pkgdb: captured {len(listings)} package entries -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
