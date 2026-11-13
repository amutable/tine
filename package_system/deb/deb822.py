# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Small, strict readers for Debian's Deb822 metadata."""

import io
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, NamedTuple, cast

import util
from util import MAGIC, decompressor

import snapshotter
from href import relative_href

# The file names that a mirror serves a Packages index under, most preferred first.
INDEX = "Packages"
INDEX_NAMES = (f"{INDEX}.xz", f"{INDEX}.gz", INDEX)
# A mirror serves each suite in the directory `dists/<suite>`.
SUITES = "dists"


def index_directory(component: str, arch: str) -> str:
    """Return the directory, relative to the suite, of the index of a component and architecture."""
    return f"{component}/binary-{arch}"


def package_index(directory: Path, what: str) -> Path:
    """Return the single Packages index of a materialized repository.

    The index is at the path that the mirror serves it under. A mirror serves one index in several
    compressions, but a materialized repository must hold exactly one file.
    """
    if (directory / SUITES).is_dir():
        served = f"{SUITES}/*/{index_directory('*', '*')}/"
        found = [path for name in INDEX_NAMES for path in directory.glob(f"{served}/{name}")]
    else:
        found = [path for name in INDEX_NAMES for path in directory.glob(name)]
    if len(found) != 1:
        util.fail(f"{what}: expected exactly one Packages stream in {directory}, found {len(found)}")
    return found[0]


def suite_directory(index: Path) -> Path:
    """Return the suite directory of an index that `package_index()` found.

    For a flat repository, returns the repo root.
    """
    if index.parents[3].name == SUITES:
        return index.parents[2]
    else:
        # If there is no "dists", assume the repository is flat.
        return index.parents[0]


def stanzas(source: Iterable[str]) -> Iterator[dict[str, str]]:
    """Yield Deb822 stanzas with lowercase field names and unfolded continuation lines.

    `source` is any iterable of lines, so a caller can pass an open file or the lines of a string.
    """
    stanza: dict[str, str] = {}
    current: str | None = None
    for number, raw_line in enumerate(source, 1):
        # APT strips trailing whitespace from a line. Strip it here too, so that this reader
        # accepts the same stanzas as APT.
        line = raw_line.rstrip(" \t\r\n")
        if not line:
            if stanza:
                yield stanza
                stanza = {}
                current = None
            continue
        if line[0] in " \t":
            if current is None:
                util.fail(f"Deb822 line {number}: continuation without a field")
            stanza[current] += "\n" + line[1:]
            continue

        name, separator, value = line.partition(":")
        # Debian Policy allows printable ASCII except the colon in a field name. The name must
        # not start with `#` or `-`: `#` starts a comment, and `-` starts an armor line.
        if (
            not separator
            or not name
            or name[0] in "#-"
            or any(not "!" <= character <= "~" for character in name)
        ):
            util.fail(f"Deb822 line {number}: invalid field {line!r}")
        current = name.lower()
        if current in stanza:
            util.fail(f"Deb822 line {number}: duplicate field {name!r}")
        stanza[current] = value.lstrip(" \t")

    if stanza:
        yield stanza


def integer(value: str, what: str, *, minimum: int) -> int:
    """Parse a decimal number from Debian metadata.

    `int()` alone also accepts a sign, surrounding whitespace, non-ASCII digits and `1_000`. APT
    reads such a value differently or not at all, so this function refuses it.
    """
    if not value.isascii() or not value.isdigit():
        util.fail(f"{what} is not a decimal number: {value!r}")
    number = int(value)
    if number < minimum:
        util.fail(f"{what} is {number}, below the minimum {minimum}")
    return number


def required(stanza: dict[str, str], field: str, what: str) -> str:
    """Return a field of a stanza, and fail if the field is missing or empty."""
    value = stanza.get(field)
    if not value:
        util.fail(f"{what}: missing {field}")
    return value


class Package(NamedTuple):
    """The fields of one package in a Packages index, validated by `packages()`."""

    name: str
    filename: str
    size: int
    sha256: str


@contextmanager
def open_text(path: Path) -> Generator[Iterable[str]]:
    """Open a Deb822 file as text, and detect its compression from its first bytes.

    A byte that is not UTF-8 raises while the caller iterates inside the `with` block. This function
    catches the error there and fails with the path of the file.
    """
    with path.open("rb") as raw:
        magic = raw.read(MAGIC)
        raw.seek(0)
        opener = decompressor(magic)
        if opener is None:
            # A field name starts with a letter or a digit, so any other first byte means an
            # unknown compression. Leave an empty file and leading whitespace to the parser.
            head = magic.lstrip()
            if head and not head[:1].isalnum():
                util.fail(f"{path}: unsupported compression (magic {magic.hex()})")
        with raw if opener is None else opener(raw) as stream:
            with io.TextIOWrapper(cast(BinaryIO, stream), encoding="utf-8") as text:
                try:
                    yield text
                except UnicodeDecodeError as error:
                    util.fail(f"{path}: is not UTF-8: {error}")


def packages(index: Path, rid: str) -> Iterator[Package]:
    """Yield every package a Packages index describes."""
    with open_text(index) as source:
        for position, stanza in enumerate(stanzas(source), 1):
            where = f"{rid}: {index.name} record {position}"
            name = required(stanza, "package", where)
            filename = relative_href(rid, f"package {name}", stanza.get("filename"))
            if not filename.endswith(".deb"):
                util.fail(f"{rid}: package {name} is not a deb: {filename!r}")
            yield Package(
                name,
                filename,
                integer(required(stanza, "size", where), f"{where} Size", minimum=1),
                snapshotter.checksum(rid, f"package {name} SHA256", stanza.get("sha256")),
            )
