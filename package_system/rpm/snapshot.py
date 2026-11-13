# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Pin a repository's build metadata and authoritative RPM inventory.

The snapshot contains filtered repomd, pinned streams, and a pkgid-keyed package index.
"""

import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import Protocol, TypedDict

from util import MAGIC, decompressor, fail

import snapshotter
from href import relative_href
from snapshotter import MetadataFile, PackageEntry, RepositoryMetadata

_REPOMD_NS = "http://linux.duke.edu/metadata/repo"
_PRIMARY_NS = "http://linux.duke.edu/metadata/common"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
# Keep only streams needed for dependencies, path providers, and package groups.
_REQUIRED_STREAMS = ("primary", "filelists")
_OPTIONAL_STREAMS = ("group",)
_KEPT_STREAMS = frozenset(_REQUIRED_STREAMS + _OPTIONAL_STREAMS)


class RepositorySnapshot(TypedDict):
    metadata: RepositoryMetadata
    packages: dict[str, PackageEntry]


class BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _location_href(rid: str, what: str, location: ET.Element | None) -> str:
    """Read a location that is relative to the repository root."""
    href = location.get("href") if location is not None else None
    location_base = (
        None if location is None else (location.get("base") or location.get(f"{{{_XML_NS}}}base"))
    )
    if location_base is not None:
        fail(f"{rid}: {what} has unsupported location {href!r}")
    return relative_href(rid, what, href)


def _metadata_int(rid: str, what: str, value: str | None, *, minimum: int) -> int:
    """Parse one bounded metadata integer with a useful repository-scoped error."""
    try:
        number = int(value) if value is not None else minimum - 1
    except ValueError:
        number = minimum - 1
    if number < minimum:
        fail(f"{rid}: {what} has invalid size/count {value!r}")
    return number


def _parse_primary(rid: str, source: BinaryReader) -> dict[str, PackageEntry]:
    """Stream a primary XML file into the snapshot's compact pkgid-keyed package map."""
    events = ET.iterparse(source, events=("start", "end"))
    _, root = next(events)
    if root.tag != f"{{{_PRIMARY_NS}}}metadata":
        fail(f"{rid}: primary metadata has unexpected root {root.tag!r}")
    declared = root.get("packages")
    if declared is None:
        fail(f"{rid}: primary metadata root lacks its package count")
    expected = _metadata_int(rid, "primary metadata package count", declared, minimum=0)

    packages: dict[str, PackageEntry] = {}
    count = 0
    package_tag = f"{{{_PRIMARY_NS}}}package"
    for event, element in events:
        if event != "end" or element.tag != package_tag:
            continue

        count += 1
        checksum = element.find(f"{{{_PRIMARY_NS}}}checksum")
        location = element.find(f"{{{_PRIMARY_NS}}}location")
        size = element.find(f"{{{_PRIMARY_NS}}}size")
        if checksum is None or checksum.text is None:
            fail(f"{rid}: primary package {count} lacks a checksum")
        algorithm = checksum.get("type")
        if algorithm not in snapshotter.DIGEST_LENGTHS or checksum.get("pkgid") != "YES":
            fail(f"{rid}: primary package {count} does not have a sha256 or sha512 pkgid")
        pkgid = snapshotter.checksum(rid, f"primary package {count} pkgid", checksum.text, algorithm)

        href = _location_href(rid, f"primary package {pkgid}", location)

        package_size = size.get("package") if size is not None else None
        download_size = _metadata_int(rid, f"primary package {pkgid}", package_size, minimum=1)
        snapshotter.add_package(packages, rid, pkgid, PackageEntry(location=href, size=download_size))
        element.clear()

    if count != expected:
        fail(f"{rid}: primary metadata declared {expected} packages but contained {count}")
    return packages


def _load_package_index(rid: str, stream: MetadataFile) -> dict[str, PackageEntry]:
    """Download, verify, decompress, and parse the pinned primary stream."""
    with tempfile.TemporaryFile("w+b") as compressed:
        snapshotter.download(
            rid,
            "primary stream",
            stream["url"],
            compressed,
            size=int(stream["size"]),
            sha256=stream.get("sha256"),
            sha512=stream.get("sha512"),
        )
        compressed.seek(0)
        magic = compressed.read(MAGIC)
        compressed.seek(0)
        open_compressed = decompressor(magic)
        with ExitStack() as stack:
            if open_compressed is not None:
                source = stack.enter_context(open_compressed(compressed))
            elif magic.lstrip().startswith(b"<"):
                source = compressed
            else:
                fail(f"{rid}: unsupported primary compression (magic {magic.hex()})")
            return _parse_primary(rid, source)


def _repository_stream(
    rid: str,
    baseurl: str,
    stream_type: str,
    data: ET.Element,
) -> MetadataFile:
    """Decode and validate one retained repomd data record."""
    location = data.find(f"{{{_REPOMD_NS}}}location")
    # Fedora states a sha256 for each stream, and openSUSE states a sha512.
    checksum = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha256']")
    if checksum is None:
        checksum = data.find(f"{{{_REPOMD_NS}}}checksum[@type='sha512']")
    size = data.find(f"{{{_REPOMD_NS}}}size")
    if (
        location is None
        or location.get("href") is None
        or checksum is None
        or checksum.text is None
        or size is None
        or size.text is None
    ):
        fail(f"{rid}: {stream_type} record lacks a location, sha256 or sha512 checksum, or size")

    href = _location_href(rid, f"{stream_type} stream", location)
    stream = MetadataFile(
        out=f"repodata/{PurePosixPath(href).name}",
        url=baseurl + href,
        # Recording the compressed size avoids an unpinned HEAD request later.
        size=_metadata_int(rid, f"{stream_type} stream", size.text, minimum=1),
    )
    if checksum.get("type") == "sha512":
        stream["sha512"] = snapshotter.checksum(rid, f"{stream_type} stream", checksum.text, "sha512")
    else:
        stream["sha256"] = snapshotter.checksum(rid, f"{stream_type} stream", checksum.text)
    return stream


def snapshot_repodata(rid: str, baseurl: str) -> RepositorySnapshot:
    """Pin one repo's build-time repodata; see the module docstring for the shape."""
    print(f"{rid}: snapshotting repodata…", file=sys.stderr)
    base = baseurl.rstrip("/") + "/"
    with tempfile.TemporaryFile("w+b") as stream:
        snapshotter.download(rid, "repomd.xml", base + "repodata/repomd.xml", stream)
        stream.seek(0)
        repomd = stream.read()

    ET.register_namespace("", _REPOMD_NS)  # Preserve the default namespace.
    root = ET.fromstring(repomd)

    kept: set[str] = set()
    streams: list[MetadataFile] = []
    outputs: set[str] = set()
    primary: MetadataFile | None = None
    # The filtered repomd becomes the exact local repository view consumed by libdnf5.
    for data in list(root.findall(f"{{{_REPOMD_NS}}}data")):
        stream_type = data.get("type")
        if stream_type not in _KEPT_STREAMS:
            root.remove(data)
            continue
        if stream_type in kept:
            fail(f"{rid}: repomd.xml contains duplicate {stream_type!r} streams")
        kept.add(stream_type)

        stream = _repository_stream(rid, base, stream_type, data)
        if stream["out"] in outputs:
            fail(f"{rid}: repomd.xml streams share output basename {stream['out']!r}")
        outputs.add(stream["out"])
        streams.append(stream)
        if stream_type == "primary":
            primary = stream

    if missing := [stream for stream in _REQUIRED_STREAMS if stream not in kept]:
        fail(f"{rid}: repomd.xml missing {missing}")
    if primary is None:
        fail(f"{rid}: repomd.xml has no primary stream")

    filtered = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return {
        # The filtered repomd is the exact local repository view libdnf5 consumes, so it travels
        # as bytes rather than as something to fetch again.
        "metadata": RepositoryMetadata(files=streams, inline={"repodata/repomd.xml": filtered}),
        "packages": _load_package_index(rid, primary),
    }


def main(argv: list[str] | None = None) -> None:
    snapshotter.run(
        "snapshot", snapshotter.Spec, lambda spec: snapshot_repodata(spec["id"], spec["baseurl"]), argv
    )


if __name__ == "__main__":
    main()
