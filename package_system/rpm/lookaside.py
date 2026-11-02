#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""List the source archives that the `sources` file of a dist-git repository names, with their URLs.

Fedora and CentOS keep the source archives of a package out of its dist-git repository. The `sources` file
names each archive and its digest, and a lookaside cache serves the archive at
`<base>/<package>/<file>/sha512/<digest>/<file>`.
"""

import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import TypedDict

import specs
import util


class Archive(TypedDict):
    name: str
    sha512: str
    url: str


class Spec(TypedDict):
    # `base` is the URL of the lookaside cache.
    base: str
    package: str
    sources: str
    out: str


def archives(spec: Spec) -> list[Archive]:
    """Return the archives that the `sources` file of `spec` lists."""
    sources = Path(spec["sources"])
    # A symlink in a fetched tree can point at a file of the project. The download actions would send the
    # lines of that file to the lookaside cache.
    util.reject_symlinks(sources, "lookaside: the sources file")
    if not sources.is_file():
        util.fail(f"lookaside: {sources} is not a regular file")
    try:
        text = sources.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        util.fail(f"lookaside: {sources} is not UTF-8: {error}")
    base = spec["base"].rstrip("/")
    package = urllib.parse.quote(spec["package"])
    found: dict[str, Archive] = {}
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if re.fullmatch(r"[0-9a-fA-F]{32}\s+.+", line):
            # Buck's downloads check SHA-1, SHA-256 or SHA-512, but not MD5.
            util.fail(
                f"lookaside: {sources} lists an MD5 digest: {line!r}. A packager of the package can "
                "upload the archive again with `fedpkg new-sources`, which lists its SHA-512 digest."
            )
        match = re.fullmatch(r"SHA512 \((.+)\) = ([0-9a-fA-F]{128})", line)
        if match is None:
            util.fail(f"lookaside: cannot parse the line {line!r} of {sources}")
        name, digest = match.groups()
        # The name becomes a file name in the output directory and a component of the URL.
        if "/" in name or name in (".", ".."):
            util.fail(f"lookaside: {sources} names an archive that is not a file name: {name!r}")
        # The build declares an output named after each archive. Buck fails when two outputs have one name.
        if name in found:
            util.fail(f"lookaside: {sources} lists {name} twice")
        digest = digest.lower()
        quoted = urllib.parse.quote(name)
        url = f"{base}/{package}/{quoted}/sha512/{digest}/{quoted}"
        found[name] = Archive(name=name, sha512=digest, url=url)
    return list(found.values())


def main(argv: list[str] | None = None) -> None:
    """Parse an invocation and write the archives of its `sources` file as JSON."""
    spec = specs.parse(Spec, "lookaside", argv)
    found = archives(spec)
    Path(spec["out"]).write_text(json.dumps(found, indent=2) + "\n")
    print(f"lookaside: {spec['sources']} lists {len(found)} archives", file=sys.stderr)


if __name__ == "__main__":
    main()
