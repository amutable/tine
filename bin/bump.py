# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Update pinned development-tool metadata.

Three kinds of pin are supported:

 - A release pin names a project's newest release and the artifact, digest and size of every
   platform's download.
 - A URL pin names the newest release in an index of releases, and the artifact, digest and size
   of every platform's download from a plain URL.
 - A git pin names a ref that the bumps track, and a current commit on it.
"""

import argparse
import functools
import hashlib
import http.client
import json
import os
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

from util import atomic_write_text, fail, urlopen

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
# The socket timeout catches a host that sends nothing. A host that keeps sending a byte now and then
# needs a limit on the whole transfer.
_DEADLINE = 30 * 60
# The go.dev list of every Go release is about 2 MiB.
_INDEX_LIMIT = 64 << 20


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{description} must be a non-empty string")
    return value


def _object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{description} must be an object")
    return cast(dict[str, Any], value)


def _integer(value: object, description: str) -> int:
    # bool is an int in python, and a JSON true here would be a corrupt pin rather than a size.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{description} must be a non-negative integer")
    return value


def _array(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{description} must be an array")
    return cast(list[object], value)


def _github_json(url: str) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "tine-bump",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _latest_release(repository: str) -> dict[str, Any]:
    releases = _array(
        _github_json(f"https://api.github.com/repos/{repository}/releases?per_page=100"),
        f"GitHub releases for {repository}",
    )
    published = []
    for index, raw_release in enumerate(releases):
        release = _object(raw_release, f"GitHub release {index}")
        if release.get("draft") is False and isinstance(release.get("published_at"), str):
            published.append(release)
    if not published:
        raise ValueError(f"GitHub reports no published releases for {repository}")
    return max(published, key=lambda release: release["published_at"])


def _latest_full_release(repository: str) -> dict[str, Any]:
    """Return the repository's latest full (non-prerelease, non-draft) release.

    `/releases/latest` is both lighter than scanning every release and the upstream's own idea of
    "stable". Repositories that publish only prereleases have no such release, so fall back to the scan.
    """
    try:
        return _object(
            _github_json(f"https://api.github.com/repos/{repository}/releases/latest"),
            f"latest release for {repository}",
        )
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return _latest_release(repository)


def _release_assets(release: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    return [
        _object(asset, f"asset {index} of {tag}")
        for index, asset in enumerate(_array(release.get("assets"), f"assets for {tag}"))
    ]


def _asset_digest(asset: dict[str, Any], name: str) -> str:
    digest = _string(asset.get("digest"), f"digest for release asset {name}")
    algorithm, separator, value = digest.partition(":")
    if algorithm != "sha256" or not separator or _SHA256.fullmatch(value) is None:
        raise ValueError(f"release asset {name} has invalid digest {digest!r}")
    return value


def _python_minor(path: Path | None) -> str:
    """The pinned CPython minor (e.g. "3.14"), read only for a CPython pin.

    Read lazily, because a project that pins no CPython has no reason to have the configuration that
    this reads. Every other artifact matches its successor without it.
    """
    if path is None:
        raise ValueError("a CPython pin needs a ty.toml or pyproject.toml that sets the Python version")
    table = _object(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    keys = ("tool", "ty", "environment") if path.name == "pyproject.toml" else ("environment",)
    for key in keys:
        table = _object(table.get(key), f"{key} table in {path}")
    return _string(table.get("python-version"), f"{'.'.join(keys)}.python-version in {path}")


def _asset_regex(artifact: str, tag: str, python_minor: Callable[[], str]) -> re.Pattern[str]:
    """Match the successor of `artifact` across releases by wildcarding only its version parts.

    python-build-standalone artifacts carry both a CPython version and a date, so pin the minor (from
    the ty configuration) and the platform/variant while letting the patch and date float. Every
    other upstream embeds at most the release tag, so wildcard the tag (with and without a leading
    "v").
    """
    if artifact.startswith("cpython-"):
        match = re.match(r"cpython-\d+\.\d+\.\d+\+\d+-(.+)$", artifact)
        if match is None:
            raise ValueError(f"cannot parse CPython artifact {artifact!r}")
        return re.compile(rf"cpython-{re.escape(python_minor())}\.\d+\+\d+-{re.escape(match.group(1))}")
    pattern = re.escape(artifact)
    for token in {tag, tag.lstrip("v")}:
        if token:
            pattern = pattern.replace(re.escape(token), r"[^/]+")
    return re.compile(pattern)


def _version_key(release: str) -> list[int]:
    return [int(number) for number in re.findall(r"\d+", release)]


def _select_asset(assets: list[dict[str, Any]], description: str) -> dict[str, Any]:
    if not assets:
        raise ValueError(f"no release asset matches {description}")
    if len(assets) == 1:
        return assets[0]
    # Several patch builds can match (e.g. two CPython 3.14.x in one release); take the newest.
    return max(assets, key=lambda asset: _version_key(_string(asset.get("name"), "asset name")))


def _bump_tool(
    name: str, spec: dict[str, Any], python_minor: Callable[[], str], releases: dict[str, dict[str, Any]]
) -> tuple[str, str] | None:
    """Resolve the latest release for one tool and rewrite its per-platform pins.

    Returns the (old, new) release tags when anything changed, else None.

    Projects which only publish prereleases resolve through _latest_full_release's scan fallback;
    every other tool has a plain latest release. Version-independent artifact names match verbatim;
    those embedding a version (syft, python-build-standalone) match by _asset_regex.
    """
    repository = _string(spec.get("repository"), f"{name}.repository")
    platforms = _object(spec.get("platforms"), f"{name}.platforms")
    previous = _string(spec.get("release"), f"{name}.release")
    release = releases.get(repository)
    if release is None:
        release = _latest_full_release(repository)
        releases[repository] = release
    tag = _string(release.get("tag_name"), f"tag for {repository} latest release")
    assets = _release_assets(release, tag)
    changed = tag != previous
    for arch, raw_entry in platforms.items():
        entry = _object(raw_entry, f"{name} platform {arch}")
        artifact = _string(entry.get("artifact"), f"artifact for {name} platform {arch}")
        pattern = _asset_regex(artifact, previous, python_minor)
        matched = [asset for asset in assets if pattern.fullmatch(_string(asset.get("name"), "asset name"))]
        asset = _select_asset(matched, f"/{pattern.pattern}/ in {repository} {tag}")
        new_artifact = _string(asset.get("name"), "asset name")
        digest = _asset_digest(asset, new_artifact)
        # Pinned so that buck skips the HEAD request it would otherwise size the download with.
        size = _integer(asset.get("size"), f"size for release asset {new_artifact}")
        changed = (
            changed
            or entry.get("artifact") != new_artifact
            or entry.get("sha256") != digest
            or entry.get("size") != size
        )
        entry["artifact"] = new_artifact
        entry["sha256"] = digest
        entry["size"] = size
        _move_strip_prefix(entry, f"{name} platform {arch}", previous, tag)
    spec["release"] = tag
    print(f"{name}: updated {previous} -> {tag}" if changed else f"{name}: {tag} is up to date")
    return (previous, tag) if changed else None


def _chunks(url: str) -> Iterator[bytes]:
    """Yield the body of `url`, and fail when the transfer stalls or ends early."""
    deadline = time.monotonic() + _DEADLINE
    try:
        with urlopen(url, agent="tine-bump", timeout=30) as response:
            expected = response.headers.get("Content-Length")
            received = 0
            while chunk := response.read(1 << 20):
                received += len(chunk)
                yield chunk
                if time.monotonic() > deadline:
                    raise ValueError(f"{url}: the download took longer than {_DEADLINE} seconds")
    except (OSError, http.client.HTTPException) as error:
        raise ValueError(f"{url}: {error}") from error
    # read() returns an empty chunk instead of failing when the connection closes before the end of the
    # body. A digest of the truncated body would make Buck reject every later download.
    if expected is not None and received != int(expected):
        raise ValueError(f"{url}: received {received} of {expected} bytes")


def _read(url: str) -> str:
    body = bytearray()
    for chunk in _chunks(url):
        body += chunk
        if len(body) > _INDEX_LIMIT:
            raise ValueError(f"{url}: the index is larger than {_INDEX_LIMIT} bytes")
    return body.decode("utf-8")


def _download(url: str) -> tuple[str, int]:
    """Return the sha256 and the size of the file at `url`."""
    digest = hashlib.sha256()
    size = 0
    for chunk in _chunks(url):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _replace_release(text: str, previous: str, tag: str) -> str:
    """Replace each whole occurrence of release `previous` in `text` with `tag`.

    A release with a leading v, such as v1.2.3, is often spelled 1.2.3 in file names. Digits next to an
    occurrence belong to another number, so release 4 doesn't match the 4 in amd64.
    """
    pairs = [(previous, tag)]
    if previous.startswith("v") and tag.startswith("v"):
        pairs.append((previous[1:], tag[1:]))
    for old, new in pairs:
        pattern = rf"(?<![0-9])(?<![0-9]\.){re.escape(old)}(?![0-9])(?!\.[0-9])"
        replaced = re.sub(pattern, lambda _, new=new: new, text)
        if replaced != text:
            return replaced
    return text


def _move_strip_prefix(entry: dict[str, Any], description: str, previous: str, tag: str) -> None:
    """Move the release in the platform entry's `strip_prefix`, if any, from `previous` to `tag`."""
    if "strip_prefix" in entry:
        prefix = _string(entry["strip_prefix"], f"strip_prefix for {description}")
        entry["strip_prefix"] = _replace_release(prefix, previous, tag)


def _bump_url(name: str, spec: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve the newest release in the pin's index and rewrite its per-platform pins.

    Returns the (old, new) releases when anything changed, else None. A pin without `releases` keeps
    its release, and only gets the digests and sizes it is missing.
    """
    url = _string(spec.get("url"), f"{name}.url")
    platforms = _object(spec.get("platforms"), f"{name}.platforms")
    previous = _string(spec.get("release"), f"{name}.release")
    tag = previous
    if "releases" in spec:
        releases = _object(spec["releases"], f"{name}.releases")
        index = _string(releases.get("url"), f"{name}.releases.url")
        pattern = re.compile(_string(releases.get("pattern"), f"{name}.releases.pattern"))
        found = {match.group() for match in pattern.finditer(_read(index))}
        if not found:
            raise ValueError(f"{index} contains no release that matches /{pattern.pattern}/")
        # Two spellings of one version, such as v1.2.4 and 1.2.4, have the same key. The spelling itself
        # breaks the tie, so that the choice doesn't depend on the order of the set.
        tag = max(found, key=lambda release: (_version_key(release), release))
    moved = tag != previous
    changed = False
    for arch, raw_entry in platforms.items():
        entry = _object(raw_entry, f"{name} platform {arch}")
        artifact = _string(entry.get("artifact"), f"artifact for {name} platform {arch}")
        if not moved and "sha256" in entry and "size" in entry:
            continue
        new_artifact = _replace_release(artifact, previous, tag)
        # An index can be any page, so bump can't count on it to list digests. An artifact whose name
        # doesn't contain the release can still change with the release, so it is downloaded again too.
        digest, size = _download(f"{url}/{new_artifact}")
        changed = True
        entry["artifact"] = new_artifact
        entry["sha256"] = digest
        entry["size"] = size
        _move_strip_prefix(entry, f"{name} platform {arch}", previous, tag)
    spec["release"] = tag
    if not changed:
        print(f"{name}: {tag} is up to date")
        return None
    print(f"{name}: updated {previous} -> {tag}" if moved else f"{name}: pinned the digests of {tag}")
    return previous, tag


def _bump_ref(name: str, spec: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve a ref to the commit it points at now; the (old, new) pair when it moved, else None.

    Asking the repository rather than an API is what lets a private one resolve, through the same
    credentials that clone it.
    """
    repository = _string(spec.get("repository"), f"{name}.repository")
    ref = _string(spec.get("ref"), f"{name}.ref")
    previous = _string(spec.get("commit"), f"{name}.commit")
    # --exit-code: a ref that no longer exists is a failure rather than an empty answer. The
    # encoding is git's, not the locale's, which is what would decode a refname otherwise.
    listing = subprocess.run(
        ["git", "ls-remote", "--exit-code", repository, ref],
        check=True,
        # avoid running inside of the parent repo: that may already have an Authorization: header
        # from actions/checkout, and that would override any global git `.insteadOf` configuration
        cwd=Path("/"),
        stdout=subprocess.PIPE,
        encoding="utf-8",
    )
    matched = listing.stdout.split("\n")
    lines = [line for line in matched if line]
    if len(lines) != 1:
        raise ValueError(f"{ref} matches {len(lines)} refs in {repository}")
    commit = lines[0].split()[0]
    if not _COMMIT.fullmatch(commit):
        raise ValueError(f"{repository} {ref} resolved to {commit!r}")
    spec["commit"] = commit
    if commit == previous:
        print(f"{name}: {ref} is up to date")
        return None
    print(f"{name}: updated {previous[:12]} -> {commit[:12]}")
    return (previous[:12], commit[:12])


def _commit(path: Path, updates: list[tuple[str, str, str]]) -> None:
    """Commit the rewritten pins with a message itemizing each update.

    These are mechanical, machine-generated commits, so they are not signed off.
    """
    body = "\n".join(
        f"- {name}: {previous} → {new}" if previous != new else f"- {name}: new digests for {new}"
        for name, previous, new in updates
    )
    message = f"deps: bump pinned tool releases\n\n{body}\n"
    subprocess.run(
        ["git", "-C", str(path.parent), "commit", "--file=-", "--", path.name],
        input=message,
        encoding="utf-8",
        check=True,
    )


def _selected_names(args: argparse.Namespace, data: dict[str, Any]) -> list[str]:
    if args.all:
        return list(data)
    for name in args.tool:
        if name not in data:
            raise ValueError(f"unknown tool {name!r}; known tools: {', '.join(sorted(data))}")
    return list(args.tool)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tine bump",
        description="update the pins in the project's tine.lock.json to the newest upstream releases",
    )
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME",
        help="update a pinned tool by name, e.g. ruff or ty (repeatable)",
    )
    parser.add_argument("--all", action="store_true", help="update every pinned tool")
    parser.add_argument(
        "--commit", action="store_true", help="commit the updated pins with an itemized message"
    )
    args = parser.parse_args(argv)
    if not (args.tool or args.all):
        parser.error("select at least one tool to bump")
    return args


def main(path: Path, ty_config: Path | None, argv: list[str]) -> None:
    args = _parse_args(argv)
    try:
        original = path.read_text(encoding="utf-8")
        data = _object(json.loads(original), str(path))
        python_minor = functools.cache(lambda: _python_minor(ty_config))
        releases: dict[str, dict[str, Any]] = {}
        updates: list[tuple[str, str, str]] = []
        for name in _selected_names(args, data):
            spec = _object(data.get(name), f"{name} in {path}")
            # A git pin has a `ref`. A URL pin has a `url`, which its download URLs start with.
            # Every other pin is a GitHub release.
            if "ref" in spec:
                change = _bump_ref(name, spec)
            elif "url" in spec:
                change = _bump_url(name, spec)
            else:
                change = _bump_tool(name, spec, python_minor, releases)
            if change is not None:
                updates.append((name, *change))
        content = json.dumps(data, indent=2) + "\n"
        if content != original:
            atomic_write_text(path, content)
        if args.commit and updates:
            _commit(path, updates)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as error:
        fail(f"bump: {error}")
