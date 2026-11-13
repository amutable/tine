#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Pin the Packages index of a Debian repository and the packages that the index describes.

A Debian archive has a chain of checksums. The `Release` of a suite lists the checksum of every
index, and each index lists the checksum of every package. The snapshot follows the chain once, at
refresh time. It pins the one index that a solve reads, and it writes an inventory of the packages.
Both pinned files keep the path that the mirror serves them under. `verify.py` checks the signed
`Release` against these paths: the suite must match the directory, and the `Release` must list the
index at its path.

The snapshot pins the signed `InRelease` next to the index but does not verify the signature.
Verification needs sqv, and the host only provides a pinned Buck and a pinned Python. So the
snapshot reads the unsigned `Release` from the same directory. At build time, `verify.py` follows
the chain again from the pinned `InRelease`, and fails if that does not list the pinned index.
"""

import io
import tempfile
from collections.abc import Mapping
from pathlib import Path

import deb822
import release
import util

import snapshotter
from href import relative_href
from snapshotter import MetadataFile, PackageEntry, RepositoryMetadata


class Spec(snapshotter.Spec):
    suite: str
    component: str


def read_release(rid: str, base: str, suite: str) -> tuple[MetadataFile, dict[str, str]]:
    """Return the pin of the signed `InRelease` and the stanza of the unsigned `Release`."""
    url = f"{base}/{release.INRELEASE}"
    with io.BytesIO() as raw:
        sha256, size = snapshotter.download(rid, release.INRELEASE, url, raw)
    out = f"{deb822.SUITES}/{suite}/{release.INRELEASE}" if suite else release.INRELEASE
    signed = MetadataFile(out=out, url=url, sha256=sha256, size=size)
    with io.BytesIO() as raw:
        snapshotter.download(rid, release.RELEASE, f"{base}/{release.RELEASE}", raw)
        return signed, release.stanza(f"{rid}: {release.RELEASE}", raw.getvalue())


def _offered(rid: str, stanza: dict[str, str], field: str, wanted: str) -> list[str]:
    """Fail unless a field of the Release lists `wanted`, and return the values of the field."""
    offered = deb822.required(stanza, field.lower(), f"{rid}: Release").split()
    if wanted not in offered:
        util.fail(f"{rid}: Release offers {field} {' '.join(offered)}, not {wanted!r}")
    return offered


def _indexed_together(rid: str, stanza: dict[str, str], architectures: list[str]) -> None:
    """Fail unless the index of an architecture also describes the packages of architecture `all`.

    A suite that lists `all` in `Architectures` keeps these packages in a separate index. With the
    field `No-Support-for-Architecture-all: Packages`, the index of each architecture repeats them.
    Debian sets the field. Without it, the one pinned index would lack every package of
    architecture `all`.
    """
    together = stanza.get("no-support-for-architecture-all", "").split()
    if "all" in architectures and deb822.INDEX not in together:
        util.fail(f"{rid}: packages of architecture all are indexed apart, which is not supported")


def package_index(
    rid: str, stanza: dict[str, str], base: str, suite: str, component: str, arch: str
) -> MetadataFile:
    """Return the pin of the most preferred index that the Release lists.

    With `Acquire-By-Hash`, a mirror also serves an index at a path that contains its checksum. A
    live mirror keeps this path after the suite changes, while the plain path then serves a newer
    index. The URL uses the checksum path whenever the Release offers it.
    """
    stated = release.stated(rid, stanza)
    directory = f"{deb822.index_directory(component, arch)}/" if suite else ""
    name = next((name for name in deb822.INDEX_NAMES if f"{directory}{name}" in stated), None)
    if name is None:
        util.fail(f"{rid}: Release states no {directory}Packages index")
    sha256, size = stated[f"{directory}{name}"]
    if size <= 0:
        util.fail(f"{rid}: Release states an empty {directory}{name}")
    by_hash = stanza.get("acquire-by-hash") == "yes"
    path = f"{directory}by-hash/SHA256/{sha256}" if by_hash else f"{directory}{name}"
    out = f"{deb822.SUITES}/{suite}/{directory}{name}" if suite else name
    return MetadataFile(out=out, url=f"{base}/{path}", sha256=sha256, size=size)


def inventory(rid: str, index: Path) -> dict[str, PackageEntry]:
    """Return the packages that the pinned index describes, keyed by checksum.

    The `Filename` of a package is relative to the root of the archive and not to the suite. A
    transaction therefore builds its download URLs from the base URL of the archive.
    """
    packages: dict[str, PackageEntry] = {}
    for package in deb822.packages(index, rid):
        entry = PackageEntry(location=package.filename, size=package.size)
        snapshotter.add_package(packages, rid, package.sha256, entry)
    return packages


def _repository_base(rid: str, baseurl: str, suite: str) -> tuple[str, str]:
    if suite.endswith("/"):
        # A suite ending in '/' is a path to a flat repo, not a suite.
        suite_rstripped = suite.rstrip("/")
        if suite_rstripped:
            relative_href(rid, "suite", suite_rstripped)
            return (f"{baseurl}/{suite_rstripped}", "")
        else:
            return (baseurl, "")
    else:
        # A standard repository stores the suite in `dists/<suite>`, so the name must be a single
        # path component.
        if "/" in relative_href(rid, "suite", suite):
            util.fail(f"{rid}: a suite in a directory below another is not supported: {suite!r}")
        return (f"{baseurl}/{deb822.SUITES}/{suite}", suite)


def snapshot_repository(spec: Spec) -> Mapping[str, object]:
    """Pin the index of one component of a suite and the packages that the index describes."""
    rid, component, arch = spec["id"], spec["component"], spec["arch"]
    base, suite = _repository_base(rid, spec["baseurl"].rstrip("/"), spec["suite"])
    signed, stanza = read_release(rid, base, suite)
    if suite:
        release.named(rid, stanza, suite)
        _indexed_together(rid, stanza, _offered(rid, stanza, "Architectures", arch))
        _offered(rid, stanza, "Components", component)
    stream = package_index(rid, stanza, base, suite, component, arch)

    with tempfile.TemporaryDirectory() as scratch:
        index = Path(scratch) / Path(stream["out"]).name
        with index.open("wb") as raw:
            snapshotter.download(
                rid, "Packages", stream["url"], raw, size=stream["size"], sha256=stream["sha256"]
            )
        packages = inventory(rid, index)
    return {"metadata": RepositoryMetadata(files=[signed, stream], inline={}), "packages": packages}


def main(argv: list[str] | None = None) -> None:
    snapshotter.run("snapshot", Spec, snapshot_repository, argv)


if __name__ == "__main__":
    main()
