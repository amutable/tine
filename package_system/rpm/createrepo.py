#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Generate deterministic repodata for a local package repository.

Runs inside the box so the pinned createrepo_c produces the metadata.
"""

import os
import sys
from pathlib import Path
from typing import TypedDict

import createrepo_c as cr
import specs
import util


class Spec(TypedDict):
    # Individual rpms are published under their own name; a directory's *.rpm (excluding
    # .src.rpm) are published under <dir-index>/ so consumers can map them back to inputs.
    packages: list[str]
    packages_dirs: list[str]
    out: str


# The three metadata streams every repo carries, paired with their writer class.
_STREAMS = (
    ("primary", cr.PrimaryXmlFile),
    ("filelists", cr.FilelistsXmlFile),
    ("other", cr.OtherXmlFile),
)


def createrepo(entries: list[tuple[str, Path]], out: Path, revision: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    entries = sorted(entries)
    if not entries:
        util.fail("no packages given")

    # location_href is relative to the repository base URL.
    for href, rpm in entries:
        dst = out / href
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Hardlinked rather than symlinked, which would dangle once the repository is rebound into
        # a sandbox.
        util.clone_file(rpm, dst, allow_link=True)

    repodata = out / "repodata"
    repodata.mkdir(exist_ok=True)

    paths = {name: str(repodata / f"{name}.xml.gz") for name, _ in _STREAMS}
    writers = {name: cls(paths[name]) for name, cls in _STREAMS}
    for w in writers.values():
        w.set_num_of_pkgs(len(entries))
    for href, rpm in entries:
        pkg = cr.package_from_rpm(str(rpm))
        pkg.location_href = href
        for w in writers.values():
            w.add_pkg(pkg)
    for w in writers.values():
        w.close()

    # Pin the revision instead of using wall-clock time.
    repomd = cr.Repomd()
    repomd.set_revision(revision)
    for name, _ in _STREAMS:
        rec = cr.RepomdRecord(name, paths[name])
        rec.fill(cr.SHA256)  # ty: ignore[missing-attribute]  # SWIG dynamic constant
        rec.rename_file()  # standard <checksum>-<name>.xml.gz layout
        repomd.set_record(rec)
    (repodata / "repomd.xml").write_text(repomd.xml_dump())

    print(f"createrepo: {len(entries)} packages → {out}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "createrepo", argv)
    entries = [(Path(package).name, Path(package).absolute()) for package in spec["packages"]]
    for index, directory in enumerate(spec["packages_dirs"]):
        entries += [
            (f"{index}/{package.name}", package)
            for package in sorted(Path(directory).absolute().glob("*.rpm"))
            if not package.name.endswith(".src.rpm")
        ]
    # The fallback keeps standalone use possible.
    revision = os.environ.get("SOURCE_DATE_EPOCH", "0")
    createrepo(entries, Path(spec["out"]).absolute(), revision)


if __name__ == "__main__":
    main()
