#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Copy the rpm database out of a logical image as a separate, trimmed artifact.

Useful as a basis for SBOM creation and security scanners. The database is not shipped in
the image, so capture it as a separate artifact. rpm keeps one sqlite database, so the
output is a copy of that file.

The copy is trimmed to the ``Packages`` table alone: rpm's path/dependency lookup indexes
(Basenames, Providename, ...) are used only by rpm itself, dropping them roughly halves
the size.

The in-image database is already journal-parked at install time (install.py:parkdb); this
re-parks the copy so DROP/VACUUM leaves no -wal/-shm sidecar beside the declared output.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

import specs
from util import fail

import finalize

# Matches macros.py:DBPATH.
DBPATH = "usr/lib/sysimage/rpm/rpmdb.sqlite"


class Spec(finalize.ImageSpec):
    out: str


def _trim(db: Path) -> None:
    """Keep only the Packages table and compact the copy into a stable single file."""
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        # SQLite has no "DROP TABLE WHERE", so enumerate the tables rpm alone uses.
        doomed = [
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name != 'Packages' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for name in doomed:
            con.execute(f'DROP TABLE "{name}"')
        con.execute("VACUUM")
    finally:
        con.close()


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "pkgdb", argv)

    out = Path(spec["out"])
    with finalize.image(spec) as tree:
        src = tree / DBPATH
        if not src.exists():
            fail(f"no rpmdb at {src}; the image has no installed packages")
        shutil.copy2(src, out)
    _trim(out)
    print(f"pkgdb: captured Packages-only database -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
