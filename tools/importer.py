#!/usr/bin/env python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""RPM monorepo import and maintenance machinery.

See docs/design/packages.md for the design.

Dependencies: git, rpm, rpm-build (rpmspec). Everything else happens over REST.
`srpm` additionally needs dist-git-client to fetch sources.
"""

import argparse
import hashlib
import json
import logging
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xmlrpc.client
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict, cast, get_args


def repo_root(start: Path) -> Path | None:
    """The OS.git root: the Buck project `start` (this tool's file) belongs to.

    Like Buck, take the furthest ancestor with a .buckconfig: the tine cell has one of its own,
    and so does its checkout, with a .git (a directory in a plain clone, a file in a worktree or
    submodule). That finds the root wherever the cell sits: expanded from an external cell,
    vendored, or copied under buck-out. A new OS.git has no packages/ yet, so that cannot mark it.

    Returns None if that project has no .git, or is a standalone tine checkout, whose .buckconfig
    maps the tine cell to itself: import must stay quiet there; only the verbs need the root, and
    they exit with a message.
    """
    roots = [d for d in start.resolve().parents if (d / ".buckconfig").exists()]
    if not roots or not (roots[-1] / ".git").exists():
        return None
    if re.search(r"^\s*tine\s*=\s*\.\s*$", (roots[-1] / ".buckconfig").read_text(), re.MULTILINE):
        return None
    return roots[-1]


ROOT: Path | None = repo_root(Path(__file__))


def _root() -> Path:
    """The OS.git root after the CLI or test fixture has established one."""
    assert ROOT is not None
    return ROOT


# None in a standalone tine checkout; every verb needs it, so main() exits early when it is unset.
WORKTREE = _root() / ".upstream-rpm" if ROOT else None  # persistent checkout of the upstream-rpm branch

# Exit code for "conflicts committed with markers" (update/update-all): distinct from generic
# failures (1) and argparse usage errors (2), so that bot_pr.py --draft-exit can tell an expected
# conflict apart from a crash.
EXIT_CONFLICT = 3

# arches we build for -- srcpkg.json records each one's (arch-conditional) static BuildRequires.
Arch = Literal["x86_64", "aarch64"]
BuildRequiresArch = Arch | Literal["_all"]

# runtime constant
BUILD_ARCHES = cast(tuple[Arch, ...], get_args(Arch))
# per-branch curation metadata; underscore makes it not a valid package name and ignored
BRANCH_PROPERTIES = "_properties.json"


class DistroConfig(TypedDict):
    """Everything we need to import from one upstream distro."""

    dist_git: str  # clone-URL prefix; a package's clone URL is '<dist_git><packagename>.git'
    lookaside: str  # base; a source URL is '<lookaside>/<pkg>/<file>/<hashtype>/<hash>/<file>'
    koji_hub: str  # koji XML-RPC endpoint
    koji_pkgs: str  # built-rpm download base ('<koji_pkgs>/<n>/<v>/<r>/<arch>/<nvra>.rpm')
    dist: str  # regex matching this distro's release dist tag (r'\.fc\d+', r'\.el\d+')
    bodhi: NotRequired[str]  # Fedora-only; in CentOS every build is auto-published


class BranchProperties(TypedDict):
    buildroot: NotRequired[str]  # override the default //buildroots/<distro>:<branch> (overlay branches)
    buildroot_only_packages: NotRequired[list[str]]
    in_place_packaging: NotRequired[dict[str, str]]  # package -> checkout-relative file of per-distro pins
    in_place_rpmbuild_options: NotRequired[dict[str, list[str]]]
    in_place_specs: NotRequired[dict[str, str]]
    seed_only_packages: NotRequired[list[str]]  # source packages; see packages.md
    rpmbuild_options: NotRequired[dict[str, list[str]]]  # package -> --with/--without/--define


# curated distros we can import from
DISTROS: dict[str, DistroConfig] = {
    "fedora": {
        "dist_git": "https://src.fedoraproject.org/rpms/",
        "lookaside": "https://src.fedoraproject.org/repo/pkgs/rpms",
        "koji_hub": "https://koji.fedoraproject.org/kojihub",
        "koji_pkgs": "https://kojipkgs.fedoraproject.org/packages",
        "dist": r"\.fc\d+",
        "bodhi": "https://bodhi.fedoraproject.org/updates/",
    },
    "centos": {
        "dist_git": "https://gitlab.com/redhat/centos-stream/rpms/",
        "lookaside": "https://sources.stream.centos.org/sources/rpms",
        "koji_hub": "https://kojihub.stream.centos.org/kojihub",
        "koji_pkgs": "https://kojihub.stream.centos.org/kojifiles/packages",
        "dist": r"\.el\d+",
    },
}


# our %dist suffix; also the whole %dist for native packages (without upstream)
# FIXME: hardcoded; read this from some config file instead?
NATIVE_DIST = "aos"


class KojiBuild(TypedDict):
    """Subset of a koji build (listBuilds entry) we use."""

    build_id: int
    nvr: str
    name: str
    version: str
    release: str


class KojiRPM(TypedDict):
    """Subset of one koji listBuildRPMs() entry, identifying a single binary/src rpm."""

    name: str
    version: str
    release: str
    arch: str


class KojiTag(TypedDict):
    name: str


class BinaryMetadata(TypedDict):
    """rpm-computed relationships and file list of one binary package."""

    Requires: list[str]
    Recommends: list[str]
    Provides: list[str]
    Files: list[str]


class SourceMetadata(TypedDict):
    """One upstream source archive."""

    url: str
    # buck's http_file wants SHA256; `sources` has SHA512
    sha256sum: str
    # archive byte size, recorded for free at download; lets buck skip the HEAD probe for an
    # already-downloaded archive
    size: int


class SrcpkgMetadata(TypedDict):
    """Contents of packages/<srcpkg>.json."""

    # BuildRequires: the arch-common set (including what %generate_buildrequires resolved to)
    # under '_all', plus each build arch's conditional extras (key omitted when empty)
    build_requires: dict[BuildRequiresArch, list[str]]
    # a noarch rpm appears under every build arch that produces it
    binaries: dict[Arch, dict[str, BinaryMetadata]]
    sources: list[SourceMetadata]
    # project_build_fields() adds version, release, dist and source_date_epoch on the main branch
    # only. rpm.branch reads them because it can't derive them from the other fields.
    version: NotRequired[str]
    release: NotRequired[str]
    dist: NotRequired[str]
    source_date_epoch: NotRequired[int]


class RpmInfo(NamedTuple):
    """Identity of one local rpm file."""

    name: str
    # not Arch: a source rpm reports its (possibly foreign, e.g. koji's s390x) build-host arch, and
    # multilib binaries (glibc.i686) reach us too -- both outside BUILD_ARCHES. Callers narrow to
    # Arch only where they use it as a key, after an `arch in BUILD_ARCHES` guard.
    arch: str
    is_source: bool  # a .src.rpm reports its build arch, not 'src', so flag it explicitly


def git(*args: str, cwd: Path | None = None) -> str:
    logging.debug("git %s%s", " ".join(args), f"  (cwd={cwd})" if cwd else "")
    return subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, text=True).stdout


# transient overload, gateway, and rate-limit conditions; any other 4xx is a definitive answer
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
HTTP_RETRIES = 4
HTTP_BACKOFF = 2.0  # seconds before the first retry, doubled each time


def urlopen_retry(url: str) -> bytes:
    """Fetch a URL's full body, retrying transient failures with exponential back-off.

    Retries the RETRYABLE_STATUS server responses, a connection that never completed (URLError), and
    a connection dropped mid-transfer (reset or timeout). Any other HTTP status is a definitive
    answer that a retry cannot change, so it propagates immediately.
    """
    for attempt in range(HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(url) as response:
                return cast(bytes, response.read())
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS or attempt == HTTP_RETRIES:
                raise
            reason = f"HTTP {e.code}"
        except urllib.error.URLError as e:  # the request never reached a responding server
            if attempt == HTTP_RETRIES:
                raise
            reason = str(e.reason)
        except (ConnectionError, TimeoutError) as e:  # the connection dropped mid-body
            if attempt == HTTP_RETRIES:
                raise
            reason = str(e)
        delay = HTTP_BACKOFF * 2**attempt
        logging.warning(
            "%s failed (%s); retrying in %.0fs (%d/%d)", url, reason, delay, attempt + 1, HTTP_RETRIES
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def git_net(*args: str) -> str:
    """Run a git command with retrying failures with exponential back-off.

    git has no distinct exit code for a transport error: every persistent ("branch does not exist") or
    transient ("Connection reset by peer") fatal condition is 128.

    The reason reaches us only as free-form localized stderr text. Matching that is brittle, so instead
    retry *any* failure. Use this only for operations whose sole job is to reach the remote.

    clone removes the destination it created, so a retry needs no cleanup.
    """
    for attempt in range(HTTP_RETRIES):
        try:
            return git(*args)
        except subprocess.CalledProcessError as e:
            delay = HTTP_BACKOFF * 2**attempt
            logging.warning(
                "git %s failed (exit %d); retrying in %.0fs (%d/%d)",
                " ".join(args),
                e.returncode,
                delay,
                attempt + 1,
                HTTP_RETRIES,
            )
            time.sleep(delay)
    return git(*args)  # out of retries: this attempt's failure is the caller's


def ensure_branch() -> None:
    """Set up the local upstream-rpm branch from origin; no network, unlike ensure_worktree()"""
    if not git("branch", "--list", "upstream-rpm", cwd=_root()).strip():
        git("branch", "upstream-rpm", "origin/upstream-rpm", cwd=_root())


def init_origin_branch() -> None:
    """Create an empty upstream-rpm branch on origin, for a new OS.git without any imports yet."""
    tree = git("hash-object", "-w", "-t", "tree", "/dev/null", cwd=_root()).strip()
    commit = git("commit-tree", "-m", "Initialize upstream-rpm branch", tree, cwd=_root()).strip()
    logging.info("origin has no upstream-rpm branch; creating an empty one")
    git_net("-C", str(_root()), "push", "--quiet", "origin", f"{commit}:refs/heads/upstream-rpm")


def ensure_worktree() -> Path:
    """Return a checkout of the upstream-rpm branch, current with origin.

    Fetches the upstream-rpm branch and fast-forwards the local one. Creates the branch on origin if
    it does not exist yet.
    """
    worktree = WORKTREE
    assert worktree is not None
    refresh = "origin" in git("remote", cwd=_root()).split()
    if refresh:
        if not git_net("-C", str(_root()), "ls-remote", "origin", "refs/heads/upstream-rpm").strip():
            init_origin_branch()
        git_net("-C", str(_root()), "fetch", "--quiet", "origin", "upstream-rpm")
    ensure_branch()
    if not worktree.exists():
        git("worktree", "add", "--quiet", str(worktree), "upstream-rpm", cwd=_root())
    if refresh:
        # Local-only fast-forward of the branch and its checkout: ahead stays as it is,
        # diverged or dirty fails.
        git("merge", "--ff-only", "--quiet", "origin/upstream-rpm", cwd=worktree)
    return worktree


def is_package_entry(path: Path) -> bool:
    """True for a real package entry, False for branch-level files/dirs like _properties.json.

    rpm package names cannot start with `_`, so this marks a path as "not a package".
    """
    return not path.name.startswith("_")


def branch_properties(branchdir: Path) -> BranchProperties:
    """The branch's curation metadata from _properties.json, or {} when absent."""
    f = branchdir / BRANCH_PROPERTIES
    return cast(BranchProperties, json.loads(f.read_text())) if f.exists() else {}


def rpmq(rpmfile: Path, flag: str) -> list[str]:
    """Run an rpm query (`--requires`, `--provides`, `-l`, ...) on a local rpm.

    Returns sorted, de-duplicated lines with the noise `rpmlib(...)` deps dropped.
    """
    out = subprocess.run(
        ["rpm", "-qp", "--nosignature", flag, str(rpmfile)], check=True, stdout=subprocess.PIPE, text=True
    ).stdout
    return sorted({ln for ln in out.splitlines() if ln and not ln.startswith("rpmlib(")})


def fetch_rpm(destdir: Path, distro: str, build: KojiBuild, rpm: KojiRPM) -> Path:
    """Download one rpm of a koji build from the distro's koji into destdir; return its path."""
    fn = f"{rpm['name']}-{rpm['version']}-{rpm['release']}.{rpm['arch']}.rpm"
    base = DISTROS[distro]["koji_pkgs"]
    url = f"{base}/{build['name']}/{build['version']}/{build['release']}/{rpm['arch']}/{fn}"
    dest = destdir / fn
    logging.debug("fetch %s", url)
    dest.write_bytes(urlopen_retry(url))
    return dest


def branch_dist(distro: str, branch: str) -> str | None:
    """The exact dist tag a distro/branch's builds carry, or None for a rolling branch.

    E.g. `f44` -> '.fc44', `c10s` -> '.el10'; None means the newest build wins (Fedora rawhide).
    """
    if distro == "fedora":
        if branch == "rawhide":
            return None
        m = re.fullmatch(r"f(\d+)", branch)
        assert m, f"unsupported fedora branch: {branch}"
        return f".fc{m.group(1)}"
    if distro == "centos":  # CentOS Stream: c<N>s -> .el<N>
        m = re.fullmatch(r"c(\d+)s", branch)
        assert m, f"unsupported centos branch: {branch}"
        return f".el{m.group(1)}"
    raise AssertionError(f"unsupported distro: {distro}")


def find_build(
    koji: xmlrpc.client.ServerProxy, distro: str, packagename: str, url: str, sha: str, branch: str
) -> KojiBuild | None:
    """The koji build of this exact dist-git commit, or None if it was never built for `branch`.

    Found by its dist-git source (`git+<url>#<sha>`), so no NVR is reconstructed and the lookup
    *is* the commit-match check. A commit is built for several targets; we keep the distro's own
    dist-tagged builds (`.fcNN`/`.elNN`, which also drops CentOS `,draft_*` and other releases)
    and pick by branch: an exact dist for `f<N>`/`c<N>s`, or the newest for Fedora rawhide.
    """
    package_id = koji.getPackageID(packagename)
    builds = cast(
        list[KojiBuild], koji.listBuilds(package_id, None, None, None, 1, None, f"git+{url}#{sha}")
    )
    logging.debug(
        "koji: %s commit %s -> %s", packagename, sha[:12], [b["nvr"] for b in builds] or "no builds"
    )
    matches = [b for b in builds if re.search(DISTROS[distro]["dist"] + "$", b["release"])]
    if not matches:
        return None
    want = branch_dist(distro, branch)
    if want is None:  # Fedora rawhide: the build with the newest dist tag
        build = max(matches, key=lambda b: int(b["release"].rsplit(".fc", 1)[1]))
    else:
        exact = [b for b in matches if b["release"].endswith(want)]
        if not exact:
            return None  # built for other releases but not this branch
        assert len(exact) == 1, f"expected one {want} build, got {[b['nvr'] for b in exact]}"
        build = exact[0]
    logging.debug("koji: selected %s for %s/%s", build["nvr"], distro, branch)
    return build


def is_published(koji: xmlrpc.client.ServerProxy, distro: str, branch: str, build: KojiBuild) -> bool:
    """Whether a build has actually landed upstream (not a maintainer's private build).

    CentOS auto-publishes every koji build, Fedora rawhide auto-tags, and Hummingbird builds
    every commit -- there the build itself is the signal. A Fedora *branched* build counts
    if it is tagged into the release's compose/updates -- `f<N>`, `f<N>-updates`,
    `f<N>-updates-testing` (this is what catches mass rebuilds, which bypass bodhi) -- OR the
    maintainer has at least submitted a bodhi update for it (any status; we don't wait for it
    to reach stable).
    """
    if distro != "fedora" or branch == "rawhide":
        return True
    tags = {t["name"] for t in cast(list[KojiTag], koji.listTags(build["build_id"]))}
    if tags & {branch, f"{branch}-updates", f"{branch}-updates-testing"}:
        logging.debug("koji: %s tagged %s -> published", build["nvr"], sorted(tags))
        return True
    bodhi = DISTROS[distro].get("bodhi")
    assert bodhi, f"{distro} has no bodhi configured"
    updates = json.loads(urlopen_retry(f"{bodhi}?builds={build['nvr']}"))["updates"]
    logging.debug("bodhi: %s -> %d update(s)", build["nvr"], len(updates))
    return bool(updates)


def latest_published_commit(
    koji: xmlrpc.client.ServerProxy,
    clone: Path,
    distro: str,
    packagename: str,
    url: str,
    branch: str,
    since: str | None = None,
) -> str | None:
    """Newest first-parent commit on HEAD whose koji build exists and is published, else None.

    Restricted to commits after `since`, when given.
    """
    revs = f"{since}..HEAD" if since else "HEAD"
    for c in git("rev-list", "--first-parent", revs, cwd=clone).split():
        build = find_build(koji, distro, packagename, url, c, branch)
        if build and is_published(koji, distro, branch, build):
            return c
    return None


def rpm_info(rpmfile: Path) -> RpmInfo:
    """Identify a local rpm file (source packages are flagged via %{SOURCEPACKAGE})."""
    name, arch, src = subprocess.run(
        ["rpm", "-qp", "--nosignature", "--qf", "%{NAME} %{ARCH} %{SOURCEPACKAGE}", str(rpmfile)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.split()
    return RpmInfo(name, arch, src == "1")


def spec_defines(rpmfile: Path) -> dict[str, str]:
    """Macro definitions reproducing the distro context a build's spec was evaluated in.

    %dist and %fedora or %rhel+%centos are parsed off the rpm's Release dist tag (any rpm of
    the build works, they share the release; BuildRequires are commonly conditional on them);
    an .elN build of ours is CentOS Stream, which defines both %rhel and %centos. The
    %autorelease/%autochangelog fallbacks let rpmspec parse an unfrozen dist-git spec without
    rpmautospec installed -- BuildRequires never depend on the release, so their values don't
    matter.
    """
    release = subprocess.run(
        ["rpm", "-qp", "--nosignature", "--qf", "%{RELEASE}", str(rpmfile)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    if release.endswith("." + NATIVE_DIST):
        return {"dist": NATIVE_DIST}

    m = re.search(r"\.(fc|el)(\d+)", release)
    assert m, f"no dist tag in srpm release {release!r}"
    tag, number = cast(str, m.group(1)), cast(str, m.group(2))
    distro = {"fc": {"fedora": number}, "el": {"rhel": number, "centos": number}}
    return {"dist": m.group(), **distro[tag], "autorelease": "1%{?dist}", "autochangelog": "%nil"}


def spec_buildrequires(
    spec: Path, arch: str, defines: dict[str, str], options: list[str] | None = None
) -> list[str]:
    """Statically evaluate a spec's BuildRequires for one target arch.

    %_sourcedir is the spec's own directory: some specs read committed source files at parse
    time (e.g. filesystem's lua). `options` are extra rpmspec CLI options (--with/--without/
    --define). Returns sorted, de-duplicated packages with the `rpmlib(...)` noise dropped,
    like rpmq().
    """
    cmd = [
        "rpmspec",
        "--target",
        arch,
        "-q",
        "--buildrequires",
        "--define",
        f"_sourcedir {spec.parent.resolve()}",
        *(options or []),
        str(spec),
    ]
    for k, v in defines.items():
        cmd += ["--define", f"{k} {v}"]
    out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True).stdout
    return sorted({ln for ln in out.splitlines() if ln and not ln.startswith("rpmlib(")})


def spec_subpackages(
    spec: Path, arch: str, defines: dict[str, str], options: list[str] | None = None
) -> set[str]:
    """The binary package names an `arch` build of the spec actually produces.

    Evaluated like spec_buildrequires (%ifarch gates, generated %package names), and excluding
    the auto-generated debuginfo/debugsource. --builtrpms restricts this to subpackages that
    really build an rpm: one declared but left with no %files under this arch (kernel-doc, whose
    %files is gated behind a with_doc that %ifnarch noarch forces off for real arches) is a bare
    %package rpmspec still lists without --builtrpms, and would otherwise be recorded as an
    expected output no such build can produce.
    """
    cmd = [
        "rpmspec",
        "--target",
        arch,
        "-q",
        "--builtrpms",
        "--queryformat",
        "%{NAME}\n",
        "--define",
        f"_sourcedir {spec.parent.resolve()}",
        *(options or []),
        str(spec),
    ]
    for k, v in defines.items():
        cmd += ["--define", f"{k} {v}"]
    out = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True).stdout
    return {ln for ln in out.splitlines() if ln}


def metadata_from_rpms(
    rpmfiles: list[Path], spec: Path, rpmbuild_options: list[str] | None = None
) -> SrcpkgMetadata:
    """Build srcpkg.json metadata from local rpm files plus the dist-git spec.

    rpm computes SONAME/pathname deps and file lists at build time, so we query the built
    rpms directly. Binary rpms (excluding debuginfo/debugsource) supply per-package
    Requires/Recommends/Provides/Files. `rpmbuild_options` are extra rpmbuild CLI options the
    build was invoked with (the branch's rpmbuild_options curation, e.g. --with=gcc_basic); the
    spec is evaluated with the same options, on top of the distro context recovered from the
    rpms.

    `binaries` is keyed by the build arch that *produces* each rpm, not the rpm's own arch:
    an arch rpm goes under its header arch, a noarch rpm under every build arch whose spec
    evaluation produces its name. Interchangeable noarch (-doc and friends) lands in every
    bucket, while an arch-specific noarch package (glibc's `sysroot-<arch>-fcNN-glibc`) lands
    only in its own arch's. A noarch rpm no build arch produces (a foreign arch's) is
    dropped: we cannot build it, so it must not be expected from any build.

    BuildRequires are arch-conditional (%ifarch), so the static set is evaluated from the spec
    per build arch. The srpm's header carries the static set *plus* what %generate_buildrequires
    resolved to (see packages.md) -- but evaluated on whatever arch koji's srpm task ran on, so the
    dynamic part is recovered by subtracting the best-matching build arch's static set, and
    folded into the arch-common '_all'. A remainder without %generate_buildrequires in the spec
    (a leftover from a foreign srpm-task arch) would poison the other arches' buildroots, so it
    crashes instead.
    """
    meta: SrcpkgMetadata = {"build_requires": {}, "binaries": {}, "sources": []}
    assert rpmfiles, "no rpms given"
    defines = spec_defines(rpmfiles[0])
    options = rpmbuild_options or []
    produces = {a: spec_subpackages(spec, a, defines, options) for a in BUILD_ARCHES}
    for f in rpmfiles:
        name, arch, is_src = rpm_info(f)
        if is_src:
            static = {a: set(spec_buildrequires(spec, a, defines, options)) for a in BUILD_ARCHES}
            common = set.intersection(*static.values())
            header = set(rpmq(f, "--requires"))
            dynamic = min((header - static[a] for a in BUILD_ARCHES), key=len)
            uses_generate_br = bool(re.search(r"^%generate_buildrequires", spec.read_text(), re.M))
            assert not dynamic or uses_generate_br, (
                f"srpm header BuildRequires {sorted(dynamic)} not attributable to any arch of "
                f"{BUILD_ARCHES} and {spec.name} has no %generate_buildrequires"
            )
            brs: dict[BuildRequiresArch, list[str]] = {"_all": sorted(common | dynamic)}
            for a in BUILD_ARCHES:
                if extra := static[a] - common:
                    brs[a] = sorted(extra)
            meta["build_requires"] = brs
        elif name.endswith(("-debuginfo", "-debugsource")):
            continue
        elif arch in BUILD_ARCHES or arch == "noarch":
            built_on: list[Arch] = (
                [arch] if arch != "noarch" else [a for a in BUILD_ARCHES if name in produces[a]]
            )
            if not built_on:
                logging.info("%s: noarch %s is only built on foreign arches, dropping", spec.stem, name)
            binmeta: BinaryMetadata = {
                "Requires": rpmq(f, "--requires"),
                "Recommends": rpmq(f, "--recommends"),
                "Provides": rpmq(f, "--provides"),
                "Files": rpmq(f, "-l"),
            }
            for a in built_on:
                meta["binaries"].setdefault(a, {})[name] = binmeta
    return meta


def fetch_metadata_from_koji(
    koji: xmlrpc.client.ServerProxy, distro: str, build: KojiBuild, spec: Path
) -> SrcpkgMetadata:
    """Fetch the relevant rpms of a koji build and compute its srcpkg.json metadata."""
    rpms = cast(list[KojiRPM], koji.listBuildRPMs(build["build_id"]))
    logging.debug("metadata: %s has %d rpms", build["nvr"], len(rpms))
    # noarch is bucketed by producing arch (see metadata_from_rpms); the rest is per-build-arch.
    wanted = [
        r
        for r in rpms
        if r["arch"] == "src"
        or (
            r["arch"] in ("noarch", *BUILD_ARCHES) and not r["name"].endswith(("-debuginfo", "-debugsource"))
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        return metadata_from_rpms([fetch_rpm(tmpdir, distro, build, r) for r in wanted], spec)


def uses_autorelease(spec_text: str) -> bool:
    """Whether a spec uses rpmautospec's %autorelease (in any of its %{?...} spellings)."""
    return bool(re.search(r"%\{?\??autorelease", spec_text))


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def native_disttag(text: str) -> str:
    """Append our NATIVE_DIST suffix to every upstream %dist tag in a string, idempotently.

    E.g. '.fc44' -> '.fc44aos. Used to predict our NEVRs in dependency strings.
    """
    return re.sub(rf"\.(?:fc|el|hum)\d+(?!{NATIVE_DIST})", lambda m: m.group() + NATIVE_DIST, text)


def predict_dist(meta: SrcpkgMetadata) -> None:
    """Rewrite each %dist tag in a srcpkg.json's relation lists to its native form, in place.

    On import/update we copy upstream-rpm's generated metadata, but our rebuild changes the dist
    tag (see native_disttag), so every NEVR in the relations shifts with it. Predicting that here
    lets the build system resolve dependencies against the names it will actually see and removes
    most of the diff the post-build recompute would otherwise report. Files lists carry no dist
    tag, so they are left untouched.
    """
    meta["build_requires"] = {
        a: [native_disttag(s) for s in brs] for a, brs in meta["build_requires"].items()
    }
    for arch in meta["binaries"].values():
        for binmeta in arch.values():
            for key in ("Requires", "Recommends", "Provides"):
                binmeta[key] = [native_disttag(s) for s in binmeta[key]]


def parse_sources(sources_file: Path) -> list[tuple[str, str, str]]:
    """Parse a dist-git `sources` file into (hashtype, filename, hexdigest) tuples.

    Handles the current BSD form ('SHA512 (foo.tar.gz) = abc...') and the legacy
    '<md5>  foo.tar.gz' form. A package with no external archives has no file (-> [])."""
    entries: list[tuple[str, str, str]] = []
    for line in sources_file.read_text().splitlines() if sources_file.exists() else []:
        if not line.strip():
            continue
        if m := re.match(r"(\w+) \((.+)\) = ([0-9a-fA-F]+)$", line):
            entries.append((m.group(1), m.group(2), m.group(3)))
        else:
            digest, filename = line.split()
            entries.append(("md5", filename, digest))
    return entries


def compute_sources(sources_file: Path, distro: str, packagename: str) -> list[SourceMetadata]:
    """Resolve a `sources` file to the srcpkg.json `sources` list of {url, sha256sum, size} objects.

    buck's http_file needs each archive's SHA256, but the lookaside is keyed by SHA512. We
    also want the archive size to avoid an extra HEAD lookup.

    FIXME: we keep the upstream distro's lookaside URLs until we re-upload to our own cache.
    """
    sources: list[SourceMetadata] = []
    for hashtype, filename, hexdigest in parse_sources(sources_file):
        base = DISTROS[distro]["lookaside"]
        url = f"{base}/{packagename}/{filename}/{hashtype.lower()}/{hexdigest}/{filename}"
        logging.info("%s: fetching %s from the lookaside", packagename, filename)
        logging.debug("GET %s", url)
        data = urlopen_retry(url)
        # validate current sum
        actual = hashlib.new(hashtype, data).hexdigest()
        assert actual == hexdigest.lower(), f"{filename}: {hashtype} mismatch ({actual} != {hexdigest})"
        sources.append({"url": url, "sha256sum": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return sorted(sources, key=lambda s: s["url"])


def import_commits(
    koji: xmlrpc.client.ServerProxy,
    wt: Path,
    distro: str,
    packagename: str,
    branch: str,
    url: str,
    clone: Path,
    commits: list[str],
) -> None:
    """Import each upstream commit (oldest first) onto the upstream-rpm worktree.

    Per commit: mirror its dist-git tree into packages/<distro>/<branch>/<pkg>/, recompute
    the sibling .json from its koji build (unbuilt CI/test commits carry the previous
    metadata forward), and commit keeping the upstream author and message. An unbuilt commit
    that changes nothing we track is skipped entirely (see below).

    The source archives' SHA256 are part of that metadata, but recomputing them means
    downloading from the lookaside, so we only do it when the `sources` file actually changes
    -- the common rebuild/CI commit leaves it untouched and reuses the previous result.
    """
    rel = f"packages/{distro}/{branch}/{packagename}"
    pkgdir = wt / rel
    metafile = wt / f"{rel}.json"
    # Seed the sources state from the last imported commit, so update-upstreams continuing an
    # existing import only re-downloads when a new commit touches `sources`.
    prev_sources = (pkgdir / "sources").read_text() if (pkgdir / "sources").exists() else ""
    sources_meta = (
        cast(SrcpkgMetadata, json.loads(metafile.read_text()))["sources"] if metafile.exists() else []
    )
    for c in commits:
        git("checkout", "--quiet", c, cwd=clone)
        if pkgdir.exists():
            shutil.rmtree(pkgdir)
        shutil.copytree(clone, pkgdir, ignore=shutil.ignore_patterns(".git"))

        cur_sources = (pkgdir / "sources").read_text() if (pkgdir / "sources").exists() else ""
        if cur_sources != prev_sources:
            sources_meta = compute_sources(pkgdir / "sources", distro, packagename)
            prev_sources = cur_sources

        build = find_build(koji, distro, packagename, url, c, branch)
        if build:
            meta = fetch_metadata_from_koji(koji, distro, build, pkgdir / f"{packagename}.spec")
            meta["sources"] = sources_meta
            write_json(metafile, meta)

        # The metadata file only exists once the package has had its first build, so don't
        # demand it for the (possibly unbuilt) commits before that.
        paths = [rel] + ([f"{rel}.json"] if (wt / f"{rel}.json").exists() else [])
        # A pristine checkout holds only the upstream-tracked files (lookaside tarballs are never
        # downloaded here). Some packages ship a .gitignore that also names committed
        # files (e.g. p11-kit ignores its own trust-extract-compat and p11-kit-client.service).
        # Add --force, so git add does not silently drop them.
        git("add", "--force", *paths, cwd=wt)
        if not git("diff", "--cached", "--name-only", cwd=wt).strip():
            # An unbuilt no-op upstream commit (an %autorelease mass rebuild, a merge with no net
            # first-parent change): nothing we track changed and no build refreshed the json.
            # Nothing consumes such a commit -- on our side the release is not a commit count, it
            # lives in srcpkg.json -- so don't mirror it.
            logging.info("%s/%s/%s: skipping no-op commit %s", distro, branch, packagename, c[:12])
            continue
        # Keep the upstream commit's author and message; prefix the subject with distro/branch/pkg.
        msg = git("log", "-1", "--format=%B", cwd=clone).strip()
        author = git("log", "-1", "--format=%an <%ae>", cwd=clone).strip()
        git(
            "commit",
            "--quiet",
            "--author",
            author,
            "-m",
            f"[{distro}/{branch}/{packagename}] {msg}\n\nX-Upstream-Commit: {c}",
            cwd=wt,
        )
        logging.info("%s/%s/%s: committed %s (%s)", distro, branch, packagename, msg.splitlines()[0], c[:12])


def import_upstream(distro: str, branch: str, packagename: str, sha: str | None) -> None:
    """Import a package's latest published upstream commit onto upstream-rpm -- one commit.

    No history: the release and version live in the imported srcpkg.json (ground truth off the
    koji build's NEVR), so even an %autorelease package needs no commit-count context -- deeper
    history stays upstream. The imported commit must have a published build (that's what
    generates the metadata); an explicit --sha without one is refused.
    """
    assert distro in DISTROS, f"unsupported distro {distro!r} (known: {', '.join(DISTROS)})"
    url = DISTROS[distro]["dist_git"] + packagename + ".git"
    wt = ensure_worktree()
    assert not (wt / "packages" / distro / branch / packagename).exists(), (
        f"{distro}/{branch}/{packagename} already imported"
    )

    koji = xmlrpc.client.ServerProxy(DISTROS[distro]["koji_hub"], allow_none=True)
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        git_net("clone", "--quiet", "--branch", branch, "--single-branch", url, str(clone))
        if sha:
            build = find_build(koji, distro, packagename, url, sha, branch)
            if not (build and is_published(koji, distro, branch, build)):
                raise SystemExit(
                    f"{packagename} commit {sha[:12]} has no published {branch} "
                    "build -- srcpkg.json can only come from one; pick a built commit"
                )
        else:
            # Default target: the newest published build (skip unbuilt/staged tip commits).
            sha = latest_published_commit(koji, clone, distro, packagename, url, branch)
            assert sha, f"no published build for {packagename} on {branch}"
        git("checkout", "--quiet", sha, cwd=clone)
        import_commits(koji, wt, distro, packagename, branch, url, clone, [sha])
    logging.info("imported %s/%s/%s onto upstream-rpm", distro, branch, packagename)


def last_imported_sha(wt: Path, distro: str, branch: str, packagename: str) -> str:
    """The X-Upstream-Commit of the most recent upstream-rpm commit touching this package."""
    rel = f"packages/{distro}/{branch}/{packagename}"
    sha = git(
        "log", "-1", "--format=%(trailers:key=X-Upstream-Commit,valueonly)", "--", rel, f"{rel}.json", cwd=wt
    ).strip()
    assert sha, f"no X-Upstream-Commit trailer for {rel}"
    return sha


def update_upstreams() -> None:
    """Import any new upstream commits for every package currently on upstream-rpm."""
    wt = ensure_worktree()
    # Each import is a packages/<distro>/<branch>/<pkg>/ directory.
    for pkgdir in sorted(p for p in (wt / "packages").glob("*/*/*") if p.is_dir()):
        distro, branch, packagename = pkgdir.relative_to(wt / "packages").parts
        assert distro in DISTROS, f"unsupported distro {distro!r}"
        koji = xmlrpc.client.ServerProxy(DISTROS[distro]["koji_hub"], allow_none=True)
        url = DISTROS[distro]["dist_git"] + packagename + ".git"
        last = last_imported_sha(wt, distro, branch, packagename)
        # Cheap pre-check: a single ls-remote tells us whether the branch moved at all.
        remote_head = git_net("ls-remote", url, f"refs/heads/{branch}").split()[0]
        if remote_head == last:
            logging.info("%s/%s/%s: up to date (%s)", distro, branch, packagename, last[:12])
            continue
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "clone"
            git_net("clone", "--quiet", "--branch", branch, "--single-branch", url, str(clone))
            target = latest_published_commit(koji, clone, distro, packagename, url, branch, since=last)
            if not target:
                logging.info("%s/%s/%s: new commits, but none published yet", distro, branch, packagename)
                continue
            commits = git("rev-list", "--reverse", "--first-parent", f"{last}..{target}", cwd=clone).split()
            import_commits(koji, wt, distro, packagename, branch, url, clone, commits)
            logging.info("%s/%s/%s: imported %d new commit(s)", distro, branch, packagename, len(commits))


def replay(commits: list[str], rel: str, take_upstream: bool = False) -> bool:
    """Cherry-pick upstream-rpm commits (oldest first) 1:1 onto the current branch.

    Forces each commit's srcpkg.json to the %dist-predicted form of its upstream content, and
    returns True if every commit applied cleanly, False if any left conflict markers (see below).
    The package dir (spec/patches/...) is replayed verbatim by cherry-pick, preserving each
    message, author and X-Upstream-Commit trailer. The generated srcpkg.json is fully derived:
    rather than 3-way merge it (which
    conflicts once we've rewritten dist tags, since our side no longer matches upstream's merge
    base) we overwrite it from upstream and re-run predict_dist(). So the json is never merged,
    only recomputed -- which is also why the dir and json must be listed together when selecting
    `commits` (a metadata-only rebuild still gets its own faithful commit).

    -Xno-renames: a pure subtree add whose merge base holds other packages main lacks would
    otherwise misfire as a "directory rename split".

    take_upstream discards a local delta we don't want to carry over (an obsolete modification per
    sync(), a Release-only bump per update()): the dir is taken from the commit wholesale instead of
    merged into ours, so the delta -- files we added included -- is gone and nothing can conflict.
    Only the first commit needs it; the dir is upstream's from there on.
    """
    jrel = f"{rel}.json"
    branchdir = (_root() / rel).parent
    buckrel = f"{Path(rel).parent}/BUCK"
    conflicts: list[str] = []
    for i, c in enumerate(commits):
        # Stage the commit's dir without committing yet: taken wholesale when we discard our delta,
        # otherwise merged into ours -- which on import is clean (verbatim on both sides), while on
        # update a local modification may conflict (handled below). The json is never merged -- we
        # recompute it, discarding any conflict cherry-pick left in it.
        if take_upstream and i == 0:
            restore(c, rel)
        else:
            subprocess.run(
                ["git", "cherry-pick", "-n", "-Xno-renames", "--allow-empty", c],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        if git("ls-tree", c, "--", jrel, cwd=ROOT).strip():
            meta = cast(SrcpkgMetadata, json.loads(git("show", f"{c}:{jrel}", cwd=ROOT)))
            predict_dist(meta)
            # Fold the build projection into the metadata (main-only, never on the pristine
            # upstream-rpm mirror), so the per-branch BUCK can load the .json directly. SDE = this
            # commit's author date (the date `-C` stamps below; stable across the metadata amend,
            # advancing on every rebuild).
            project_build_fields(meta, int(git("show", "-s", "--format=%at", c, cwd=ROOT)))
            write_json(_root() / jrel, meta)
            git("add", "--", jrel, cwd=ROOT)
        elif (_root() / jrel).exists():
            git("rm", "--quiet", "--", jrel, cwd=ROOT)
        # Regenerate the per-branch BUCK to match the .json set (a no-op git-add when unchanged).
        regenerate_buck(branchdir)
        git("add", "--", buckrel, cwd=ROOT)
        # The json is recomputed, never merged, so exclude it. A conflict left in the package dir
        # is kept with its markers and committed with a CONFLICT: subject (packages.md); we return
        # False so the caller (and ultimately the CLI) reports it and exits non-zero -- the PR
        # lands as a failing draft for a human to resolve.
        conflicted = [
            p for p in git("diff", "--name-only", "--diff-filter=U", cwd=ROOT).split() if p != jrel
        ]
        if conflicted:
            git("add", "--", *conflicted, cwd=ROOT)
            msg = git("log", "-1", "--format=%B", c, cwd=ROOT).strip()
            author = git("log", "-1", "--format=%an <%ae>", c, cwd=ROOT).strip()
            git(
                "commit",
                "--quiet",
                "--allow-empty",
                "--author",
                author,
                "-m",
                f"CONFLICT: {Path(rel).name} ({' '.join(conflicted)})\n\n{msg}",
                cwd=ROOT,
            )
            conflicts.append(c)
            logging.warning("%s: conflict in %s", Path(rel).name, " ".join(conflicted))
        else:
            git("commit", "--quiet", "--allow-empty", "-C", c, cwd=ROOT)
    return not conflicts


def import_(packagename: str, distro: str | None, branch: str | None) -> None:
    """Replay a package's full upstream-rpm history onto main (see replay()).

    main mirrors upstream-rpm's packages/<distro>/<branch>/<pkg>/ layout, so commits apply at the
    same path and we keep them 1:1 (message, author, X-Upstream-Commit, and the %autorelease
    commit count) -- with each srcpkg.json rewritten to its predicted native dist tags.

    The commit list must cover the package's *full* history: the generated srcpkg.json is a
    sibling path (packages/.../<pkg>.json, not inside the dir), and metadata-only rebuilds touch
    only it; selecting on the dir alone would drop those.
    """
    wt = ensure_worktree()
    if not (distro and branch):
        matches = [p for p in (wt / "packages").glob(f"*/*/{packagename}") if p.is_dir()]
        assert matches, f"{packagename} not on upstream-rpm"
        assert len(matches) == 1, (
            f"{packagename} on upstream-rpm from several sources; specify distro and branch"
        )
        distro, branch, _ = matches[0].relative_to(wt / "packages").parts
    rel = f"packages/{distro}/{branch}/{packagename}"
    assert (wt / rel).is_dir(), f"{distro}/{branch}/{packagename} not on upstream-rpm"
    assert not (_root() / rel).exists(), f"{distro}/{branch}/{packagename} already on this branch"

    commits = git(
        "rev-list", "--reverse", "--first-parent", "upstream-rpm", "--", rel, f"{rel}.json", cwd=wt
    ).split()
    replay(commits, rel)
    logging.info("imported %s/%s/%s onto main (%d commit(s))", distro, branch, packagename, len(commits))


def resolve_main(packagename: str, distro: str | None = None, branch: str | None = None) -> str:
    """The packages/<distro>/<branch>/<pkg> path of a package on the current branch.

    main mirrors upstream-rpm's deep layout, so one package name may exist at several
    distro/branch coordinates (e.g. fedora/f44 and fedora/rawhide); pass distro and branch to
    disambiguate. Raises a readable error if the package is absent or ambiguous.
    """
    if distro and branch:
        rel = f"packages/{distro}/{branch}/{packagename}"
        if not (_root() / rel).is_dir():
            raise SystemExit(f"{distro}/{branch}/{packagename}: not on this branch")
        return rel
    matches = sorted(p for p in (_root() / "packages").glob(f"*/*/{packagename}") if p.is_dir())
    if not matches:
        raise SystemExit(f"{packagename}: no such package on this branch")
    if len(matches) > 1:
        coords = ", ".join("/".join(p.relative_to(_root() / "packages").parts[:2]) for p in matches)
        raise SystemExit(
            f"{packagename}: present from several sources ({coords}); specify distro and branch"
        )
    return str(matches[0].relative_to(_root()))


def upstream_anchor(rel: str) -> str:
    """The most recent X-Upstream-Commit trailer on commits touching a package path, else ''.

    `rel` is packages/<distro>/<branch>/<pkg>; '' means native / not imported. Restricted to
    commits that actually carry the trailer, so later local rebuild commits (which
    don't) don't shadow the import/update anchor. Both the dir and its sibling <pkg>.json count,
    so a trailing upstream metadata-only rebuild is still seen as the anchor.
    """
    return git(
        "log",
        "-1",
        "--format=%(trailers:key=X-Upstream-Commit,valueonly)",
        "--grep=^X-Upstream-Commit:",
        "--",
        rel,
        f"{rel}.json",
        cwd=ROOT,
    ).strip()


def is_modified(ours: str, upstream: str) -> bool:
    """Whether our package tree differs from the imported upstream one beyond a Release: delta.

    A pure Release: bump (our rebuilds) counts as unmodified, per packages.md. Both args are tree-ish
    refs in this repo (the upstream-rpm branch shares our object store),
    e.g. 'HEAD:packages/<pkg>' and '<sha>:packages/<distro>/<branch>/<pkg>'. Used by
    list/update/diff.
    """
    if git("rev-parse", ours, cwd=ROOT).strip() == git("rev-parse", upstream, cwd=ROOT).strip():
        return False
    changed = [
        ln
        for ln in git("diff", ours, upstream, cwd=ROOT).splitlines()
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]
    return not (changed and all(re.match(r"[-+]Release:", ln) for ln in changed))


def restore(ref: str, *paths: str) -> None:
    """Stage `paths` exactly as they are at `ref`, dropping whatever we added on top.

    `git checkout <ref> -- <paths>` alone only rewinds what `ref` *has*, so a file our local
    modification added (a downstream patch) would survive it and keep the package modified. Clearing
    the paths first makes the result an exact tree match. A path that `ref` doesn't have at all is
    simply gone afterwards.
    """
    git("rm", "--quiet", "-r", "--ignore-unmatch", "--", *paths, cwd=ROOT)
    at_ref = [p for p in paths if git("ls-tree", ref, "--", p, cwd=ROOT).strip()]
    if at_ref:
        git("checkout", ref, "--", *at_ref, cwd=ROOT)


def pending_upstream(wt: Path, rel: str) -> tuple[str, list[str]]:
    """The upstream-rpm commit our import is anchored at, plus the newer ones (oldest first).

    Exactly what `update` and `sync` replay; empty when we are on the latest upstream commit.
    """
    anchor = upstream_anchor(rel)
    if not anchor:
        raise SystemExit(f"{rel.removeprefix('packages/')}: native package, no upstream to take")
    urc = git(
        "log", "upstream-rpm", "-1", "--format=%H", f"--grep=^X-Upstream-Commit: {anchor}$", cwd=wt
    ).strip()
    assert urc, f"no upstream-rpm commit for {rel} @ {anchor}"
    return urc, git(
        "rev-list", "--reverse", "--first-parent", f"{urc}..upstream-rpm", "--", rel, f"{rel}.json", cwd=wt
    ).split()


def update(packagename: str, distro: str | None = None, branch: str | None = None) -> bool:
    """Replay any new upstream-rpm commits for a package onto the current (main) branch.

    Preserves the 1:1 commit correspondence and rewrites each srcpkg.json to its predicted native dist
    tags (see replay()). Returns True on success; False if a local modification conflicted (the
    conflicting commits are still made, with their markers and a CONFLICT: subject) -- main() turns
    that into a non-zero exit.
    """
    wt = ensure_worktree()
    rel = resolve_main(packagename, distro, branch)
    urc, new = pending_upstream(wt, rel)
    if not new:
        logging.info("%s: already up to date", rel.removeprefix("packages/"))
        return True
    # A purely-Release local delta (a rebuild's Release: bump) has nothing worth preserving over an
    # update: take the dir from upstream so the new release wins cleanly rather than colliding on the
    # Release: line (packages.md). A real modification is kept, and may conflict.
    ours, base = f"HEAD:{rel}", f"{urc}:{rel}"
    release_only = git("rev-parse", ours, cwd=ROOT).strip() != git(
        "rev-parse", base, cwd=ROOT
    ).strip() and not is_modified(ours, base)
    ok = replay(new, rel, take_upstream=release_only)
    logging.info("%s: applied %d new commit(s)", rel.removeprefix("packages/"), len(new))
    return ok


def rpm_metadata(
    packagename: str, rpms: list[str], distro: str | None = None, branch: str | None = None
) -> None:
    """(Re)compute a package's packages/<distro>/<branch>/<pkg>.json from locally built rpm files.

    Used for native packages (no upstream koji build to fetch) and as the post-build
    recompute check for imported ones; pass the binary rpms and optionally the .src.rpm.
    """
    rel = resolve_main(packagename, distro, branch)
    metafile = _root() / f"{rel}.json"
    options = branch_properties((_root() / rel).parent).get("rpmbuild_options", {}).get(packagename, [])
    meta = metadata_from_rpms([Path(r) for r in rpms], _root() / rel / f"{packagename}.spec", options)
    # The rpms give relationships/file lists, but not the lookaside `sources` (those come from
    # dist-git) nor the buck projection; carry the sources over and re-derive the projection so a
    # recompute of an imported package doesn't drop them.
    sde = 0
    if metafile.exists():
        old = cast(SrcpkgMetadata, json.loads(metafile.read_text()))
        meta["sources"] = old.get("sources", [])
        sde = old.get("source_date_epoch") or 0
    project_build_fields(meta, sde)
    write_json(metafile, meta)
    logging.info(
        "wrote %s.json (%s)", rel.removeprefix("packages/"), ", ".join(meta["binaries"]) or "no binaries"
    )


def update_all() -> bool:
    """Run `update` for every imported package on the current branch.

    Keeps the individual per-package commits (one batch on this branch); turning that into
    a single branch/PR is the out-of-scope automation layer. Native packages (no upstream)
    are skipped. A package whose update conflicts still gets its CONFLICT: commits (see replay)
    and we keep going for the rest; return False if any conflicted, so one conflict doesn't
    block the whole batch (main() makes it a non-zero exit).
    """
    ok = True
    for pkgdir in sorted(
        p for p in (_root() / "packages").glob("*/*/*") if p.is_dir() and is_package_entry(p)
    ):
        distro, branch, pkg = pkgdir.relative_to(_root() / "packages").parts
        rel = str(pkgdir.relative_to(_root()))
        if not upstream_anchor(rel):
            logging.info("%s/%s/%s: native, no upstream to update from", distro, branch, pkg)
            continue
        ok = update(pkg, distro, branch) and ok
    return ok


def sync(packagename: str, distro: str | None = None, branch: str | None = None) -> bool:
    """Update a package to the latest upstream, discarding our local modifications.

    `update` for a delta we no longer want to carry -- upstream adopted it, or we dropped the
    requirement. The new upstream commits are replayed the same way, except that the first one takes
    upstream's package dir wholesale instead of merging ours into it, so our delta is gone and nothing
    can conflict (replay). When we are already on the latest upstream commit there is nothing to fold
    the discard into, so it becomes a commit of its own.

    Afterwards `diff` is empty and `list` shows the package clean. Returns True on success; see
    update() on the False case.
    """
    wt = ensure_worktree()
    rel = resolve_main(packagename, distro, branch)
    coord = rel.removeprefix("packages/")
    _, new = pending_upstream(wt, rel)
    if new:
        ok = replay(new, rel, take_upstream=True)
        logging.info("%s: discarded our modifications, applied %d new commit(s)", coord, len(new))
        return ok
    # The import commit holds the pristine upstream dir + predicted json we go back to.
    restore(import_commit(rel), rel, f"{rel}.json")
    if not git("diff", "--cached", "--name-only", cwd=ROOT).strip():
        raise SystemExit(f"{coord}: already in sync with upstream (nothing to discard)")
    git("commit", "--quiet", "-m", f"{Path(rel).name}: Discard local modifications", cwd=ROOT)
    logging.info("%s: discarded our modifications", coord)
    return True


def import_commit(rel: str, head: str = "HEAD") -> str:
    """The commit that last imported a package as of `head`, or '' if it is native.

    The base our local modifications sit on: both the release they bump (local_bump) and the pristine
    tree they deviate from (resets_to_import) are measured against it. Both the dir and its sibling
    <pkg>.json count, so a trailing upstream metadata-only rebuild is the base too.
    """
    return git(
        "log", head, "-1", "--grep=^X-Upstream-Commit:", "--format=%H", "--", rel, f"{rel}.json", cwd=ROOT
    ).strip()


def resets_to_import(c: str, rel: str) -> bool:
    """Whether local commit `c` put a package back to the upstream version it is based on.

    `sync` produces such a commit, and so does a hand-written revert of a local modification. It
    *removes* a modification instead of making one, which is why it restarts the release bump
    (local_bump) and needs neither a Release bump nor an X-Rebuild: trailer (check_commit) -- the
    tree comparison here establishes the release directly, instead of tracking it in a trailer.
    """
    base = import_commit(rel, c)
    if not base or base == c:
        return False
    paths = ["--", rel, f"{rel}.json"]
    return git("ls-tree", c, *paths, cwd=ROOT) == git("ls-tree", base, *paths, cwd=ROOT)


def local_bump(rel: str, head: str = "HEAD") -> int:
    """The package's local minor-version bump: the count of its local commits since the last import.

    Local = commits without an X-Upstream-Commit trailer that either touch packages/.../<pkg>/ or
    carry an X-Rebuild: <pkg> trailer (packages.md), counted from the most recent import commit up to
    `head` -- a fresh upstream release restarts the bump, so the next modification is `.1` again. A
    local commit that discards our delta restarts it just the same (resets_to_import): the package is
    back at the import, hence back to its unsuffixed release. A branch-history scan, fine at this scale.
    """
    pkg = Path(rel).name
    anchor = import_commit(rel, head)
    revs = f"{anchor}..{head}" if anchor else head
    touched = set(
        git(
            "log", revs, "--invert-grep", "--grep=^X-Upstream-Commit:", "--format=%H", "--", rel, cwd=ROOT
        ).split()
    )
    rebuilt = set(git("log", revs, f"--grep=^X-Rebuild: {pkg}$", "--format=%H", cwd=ROOT).split())
    ours = touched | rebuilt
    bump = 0
    # Newest first, over every local commit that touched the package at all -- a reset ends the count
    # the way the import does, and it may well be one that `ours` does not count (a discarded
    # X-Rebuild bump touches only the json).
    for c in git(
        "log",
        revs,
        "--invert-grep",
        "--grep=^X-Upstream-Commit:",
        "--format=%H",
        "--",
        rel,
        f"{rel}.json",
        cwd=ROOT,
    ).split():
        if resets_to_import(c, rel):
            break
        if c in ours:
            bump += 1
    return bump


def bumped_release(release: str, prev_bump: int) -> str:
    """`release` with its local minor bump advanced to prev_bump+1.

    The `.N` is inserted before the dist tag / %{?dist} macro (packages.md's "increase Release by
    0.1"); prev_bump (the current bump, 0 if none) tells us whether the base already carries a
    `.N` to replace or we append a fresh one.
    """
    m = re.search(rf"(%\{{?\??dist\}}?|\.(?:fc|el|hum)\d+(?:{NATIVE_DIST})?)$", release)
    suffix = m.group() if m else ""
    base = release[: len(release) - len(suffix)]
    pkgrel = base if prev_bump == 0 else base.rsplit(".", 1)[0]
    return f"{pkgrel}.{prev_bump + 1}{suffix}"


def rebuild(packagename: str, reason: str, distro: str | None = None, branch: str | None = None) -> None:
    """Generate a no-change rebuild commit for a package (e.g. a library it links against changed).

    Per packages.md: a package using %autorelease gets a commit that touches only its srcpkg.json
    and carries an X-Rebuild: <pkg> trailer; a static-Release package gets its spec Release: bumped
    instead. Either way srcpkg.json advances to the bumped release (the local minor `.N`, one past
    the existing local commits) -- the release a real rebuild will carry, confirmed later by the
    post-build recompute.
    """
    rel = resolve_main(packagename, distro, branch)
    pkg = Path(rel).name
    spec = _root() / rel / f"{pkg}.spec"
    metafile = _root() / f"{rel}.json"
    meta = cast(SrcpkgMetadata, json.loads(metafile.read_text()))
    prev = local_bump(rel)

    vr = build_vr(meta)
    assert vr, f"{rel}: no version-release in srcpkg.json"
    old_release = vr.rsplit("-", 1)[1]
    new_release = bumped_release(old_release, prev)
    # Bump the release in the package's own NEVRs (self-provides + versioned sibling deps end in
    # '-<release>'); other packages have no such suffix and are left alone.
    for arch in meta["binaries"].values():
        for binmeta in arch.values():
            for key in ("Provides", "Requires", "Recommends"):
                binmeta[key] = [
                    re.sub(rf"-{re.escape(old_release)}$", f"-{new_release}", s) for s in binmeta[key]
                ]
    project_build_fields(meta, meta.get("source_date_epoch") or 0)  # refresh version/release/dist
    write_json(metafile, meta)

    subject = f"{pkg}: Rebuild against {reason}"
    if uses_autorelease(spec.read_text()):
        # The release is a commit count -- no Release: line to touch. Only the generated metadata
        # changes; the X-Rebuild trailer makes that bump load-bearing (and countable above).
        git("add", "--", f"{rel}.json", cwd=ROOT)
        git("commit", "--quiet", "-m", f"{subject}\n\nX-Rebuild: {pkg}", cwd=ROOT)
    else:
        text = spec.read_text()
        m = re.search(r"^(Release:\s*)(\S+)", text, re.M)
        assert m, f"{spec}: no Release: line"
        spec.write_text(text[: m.start(2)] + bumped_release(m.group(2), prev) + text[m.end(2) :])
        git("add", "--", rel, f"{rel}.json", cwd=ROOT)
        git("commit", "--quiet", "-m", subject, cwd=ROOT)
    logging.info(
        "%s: rebuilt against %s (release -> %s)", rel.removeprefix("packages/"), reason, new_release
    )


def self_provide_vr(name: str, binmeta: BinaryMetadata) -> str | None:
    """The version-release from a binary's own '<name> = V-R' self-provide, or None.

    Also matches the arch-qualified form ('<name>(x86-64) = V-R').
    """
    for provide in binmeta["Provides"]:
        if m := re.match(rf"{re.escape(name)}(?:\(\S+\))? = (\S+)$", provide):
            return cast(str, m.group(1))
    return None


def build_vr(meta: SrcpkgMetadata) -> str | None:
    """The build's version-release, from any binary's self-provide ('<binary> = V-R'), or None.

    All sub-packages share the source's V-R, so we read it off a binary rather than via the source
    package name -- which often isn't a binary name at all (e.g. source krb5 builds krb5-libs/
    krb5-server, source python-foo builds python3-foo).
    """
    for arch in meta["binaries"].values():
        for name, binmeta in arch.items():
            if vr := self_provide_vr(name, binmeta):
                return vr
    return None


def arch_vr_skew(meta: SrcpkgMetadata) -> list[str]:
    """Binaries whose recorded version-release differs between arch buckets; [] when consistent.

    The arch buckets of `binaries` are refreshed by independent per-arch builds (packages.md
    "Multiple architectures"), so a partial update can leave e.g. x86_64 recording a Release bump
    that aarch64 never built. A binary recorded under several arches must carry the same
    self-provide V-R in each. Comparison is per binary name, so a subpackage with its own Version:
    (libbpf's usdt-devel) is never held against its siblings.
    """
    vrs: dict[str, dict[str, str]] = {}  # binary name -> arch -> V-R
    for arch, bucket in meta["binaries"].items():
        for name, binmeta in bucket.items():
            if vr := self_provide_vr(name, binmeta):
                vrs.setdefault(name, {})[arch] = vr
    return [
        f"{name} version-release skew: " + ", ".join(f"{a} has {vr}" for a, vr in sorted(per.items()))
        for name, per in sorted(vrs.items())
        if len(set(per.values())) > 1
    ]


def build_dist(meta: SrcpkgMetadata) -> str | None:
    """The upstream %dist tag (e.g. '.fc44') in the build's releases, or None.

    A predicted main-side release ('.fc44aos') still yields '.fc44' here, which callers re-suffix.
    """
    vr = build_vr(meta)
    if vr and (d := re.search(r"\.(?:fc|el|hum)\d+", vr)):
        return d.group()
    return None


def native_dist(rel: str) -> str:
    """The dist tag of a package's own builds: its upstream dist + NATIVE_DIST (e.g. '.fc44aos').

    Keeps NEVRs unique across distros. Read from the build's binary releases in srcpkg.json;
    native packages (no upstream build) just get '.' + NATIVE_DIST.
    """
    metafile = _root() / f"{rel}.json"
    if metafile.exists():
        if d := build_dist(cast(SrcpkgMetadata, json.loads(metafile.read_text()))):
            return d + NATIVE_DIST
    return "." + NATIVE_DIST


# --- build-system projection (main branch only) -------------------------------------------
# replay() adds the build fields to each package's <pkg>.json and regenerates the branch BUCK in
# every replayed commit. The upstream-rpm mirror must match upstream, so it never gets the build
# fields. Buck loads each .json as a value, and the branch BUCK passes the values to rpm.branch.


def build_version(meta: SrcpkgMetadata) -> str:
    """The upstream Version the built binaries share, with epoch and Release dropped.

    What the rule's subpackage-NVR fidelity gate matches against (rpm filenames carry no epoch).
    """
    vr = build_vr(meta)
    if not vr:
        raise SystemExit('no "<binary> = version-release" self-provide in metadata')
    return vr.split(":", 1)[-1].rsplit("-", 1)[0]  # drop epoch, then release


def build_release(meta: SrcpkgMetadata, dist: str) -> str:
    """The package Release with the dist tag stripped (e.g. '8.fc44aos' with '.fc44aos' -> '8').

    The %autorelease/Release base the build re-applies %{?dist} to, so an %autorelease spec builds
    hermetically (no rpmautospec/git in the buildroot, like the srpm verb's freeze).
    """
    vr = build_vr(meta)
    if not vr:
        raise SystemExit('no "<binary> = version-release" self-provide in metadata')
    return vr.split(":", 1)[-1].rsplit("-", 1)[1].removesuffix(dist)  # drop epoch + version, strip dist


def project_build_fields(meta: SrcpkgMetadata, source_date_epoch: int) -> None:
    """Fold the build-system projection into the srcpkg metadata, in place (main side only).

    These are the rpm_package inputs its Starlark macro can't compute itself: version/release come
    off a binary's self-provide (regex), dist is our dist tag, and source_date_epoch is the
    commit's author date. build_requires, binaries, and sources are left as-is -- the macro reads
    them straight from the metadata (each arch's produced subpackage set is that arch's `binaries`
    bucket; each source's http_file `out` is its URL basename).
    """
    d = build_dist(meta)
    dist = d + NATIVE_DIST if d else "." + NATIVE_DIST
    meta["version"] = build_version(meta)
    meta["release"] = build_release(meta, dist)
    meta["dist"] = dist
    meta["source_date_epoch"] = source_date_epoch


def _load_symbol(pkg: str) -> str:
    """A Starlark identifier for a package's loaded metadata (names may contain '-', '+', '.')."""
    return "_" + re.sub(r"\W", "_", pkg)


def regenerate_buck(branchdir: Path) -> None:
    """(Re)write packages/<distro>/<branch>/BUCK declaring every package whose .json exists.

    The buck2 package is the branch directory; each <pkg>/ subdir is a plain source tree (no
    nested BUCK), so the pristine dist-git checkouts stay untouched. buck2 loads each <pkg>.json
    natively (as its `value`) and the branch hands them all to `rpm.branch`, which validates each
    and derives per-package build metadata (incl. the self-hosting buildroot deps) in buck — the
    importer emits only data, never resolution logic. All packages in a branch share one
    package buildroot, `//buildroots/<distro>:<branch>` by default (a cell-relative alias package
    the consuming project provides); an overlay branch may override it via the `buildroot` property
    to build against another branch's buildroot (e.g. our packages over `//buildroots/fedora:rawhide`).
    Loads are sorted, so an add/remove touches one line and rebases re-resolve by regenerating.
    """
    distro, branch = branchdir.parts[-2], branchdir.parts[-1]
    pkgs = sorted(p.stem for p in branchdir.glob("*.json") if is_package_entry(p))
    local_loads = {f"{p}.json": f'load(":{p}.json", {_load_symbol(p)} = "value")' for p in pkgs}
    has_properties = (branchdir / BRANCH_PROPERTIES).exists()
    if has_properties:
        local_loads[BRANCH_PROPERTIES] = f'load(":{BRANCH_PROPERTIES}", _properties = "value")'

    # all loads have to come first in Starlark; the local ones sorted by file name, like the formatter
    loads = ['load("@tine//package_system/rpm:defs.bzl", "rpm")']
    loads += [local_loads[name] for name in sorted(local_loads)]
    # Add a stub for branches without _properties.json to keep the rest of the file
    # in the same shape
    if not has_properties:
        loads.append("_properties = {}")

    lines = ["# @generated -- do not edit (regenerated on import/update).", *loads, "", "PACKAGES = {"]
    lines += [f'    "{p}": {_load_symbol(p)},' for p in pkgs]
    lines += [
        "}",
        "",
        "rpm.branch(",
        "    packages = PACKAGES,",
        f'    buildroot = _properties.get("buildroot", "//buildroots/{distro}:{branch}"),',
        '    buildroot_only_packages = _properties.get("buildroot_only_packages", []),',
        f'    distro = "{distro}",',
        '    in_place_packaging = _properties.get("in_place_packaging", {}),',
        '    in_place_rpmbuild_options = _properties.get("in_place_rpmbuild_options", {}),',
        '    in_place_specs = _properties.get("in_place_specs", {}),',
        '    seed_only_packages = _properties.get("seed_only_packages", []),',
        '    rpmbuild_options = _properties.get("rpmbuild_options", {}),',
        ")",
    ]
    (branchdir / "BUCK").write_text("\n".join(lines) + "\n")


def fetch_sources(pkgdir: Path, distro: str, packagename: str) -> None:
    """Download a package's source archives from the upstream lookaside into pkgdir (uncommitted).

    Via dist-git-client, which validates the SHA512 as it downloads. No-op when the package ships
    no `sources` file.
    """
    sources = pkgdir / "sources"
    if not (sources.exists() and sources.read_text().strip()):
        return
    logging.info("fetching %s sources from the %s lookaside", packagename, distro)
    cmd = [
        "dist-git-client",
        "--loglevel",
        "warning",
        "--forked-from",
        DISTROS[distro]["dist_git"] + packagename + ".git",
        "sources",
    ]
    logging.debug("%s  (cwd=%s)", " ".join(cmd), pkgdir)
    subprocess.run(cmd, cwd=pkgdir, check=True)


def srpm(packagename: str, distro: str | None = None, branch: str | None = None) -> Path:
    """Assemble packages/<distro>/<branch>/<pkg>/ into a .src.rpm with `rpmbuild -bs` (-> _build/).

    Fetches the source tarballs from the upstream lookaside (uncommitted) and builds with our
    native dist tag (native_dist, e.g. .fc44aos); returns the built srpm path. An %autorelease spec
    is frozen first (like Fedora's koji plugin): %autorelease is pinned to the recorded release
    (pkgrel[.minorbump]) and %autochangelog blanked, so the srpm rebuilds reproducibly with no
    rpmautospec or git in the buildroot.
    """
    rel = resolve_main(packagename, distro, branch)
    distro = rel.split("/")[1]
    pkgdir = _root() / rel
    spec = pkgdir / f"{packagename}.spec"
    assert spec.exists(), f"{rel}: no {packagename}.spec"

    fetch_sources(pkgdir, distro, packagename)

    builddir = _root() / "_build"  # also holds binary rpms from mockbuild
    builddir.mkdir(exist_ok=True)
    spec_text = spec.read_text()
    with tempfile.TemporaryDirectory() as tmp:
        build_spec = spec
        if uses_autorelease(spec_text):
            meta = cast(SrcpkgMetadata, json.loads((_root() / f"{rel}.json").read_text()))
            release = build_release(meta, native_dist(rel))  # pkgrel[.minorbump], cross-checked at import
            build_spec = Path(tmp) / f"{packagename}.spec"
            build_spec.write_text(
                f"%global autorelease {release}%{{?dist}}\n%global autochangelog %{{nil}}\n{spec_text}"
            )
        out = subprocess.run(
            [
                "rpmbuild",
                "-bs",
                "--define",
                f"_topdir {tmp}",
                "--define",
                f"_sourcedir {pkgdir}",
                "--define",
                f"_srcrpmdir {builddir}",
                "--define",
                f"dist {native_dist(rel)}",
                str(build_spec),
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
    srcrpm = Path(
        next(ln for ln in out.splitlines() if ln.startswith("Wrote: ")).removeprefix("Wrote: ").strip()
    )
    logging.info("wrote %s", srcrpm)
    return srcrpm


def mock_config(distro: str, branch: str) -> str:
    """The mock chroot config name (-r) matching a package's import source.

    E.g. fedora/rawhide -> 'fedora-rawhide-<hostarch>', fedora/f44 -> 'fedora-44-...',
    centos/c10s -> 'centos-stream-10-...'. mock's default config tracks the *host* release,
    which is generally not the release a package was imported from.
    """
    arch = platform.machine()
    if distro == "fedora":
        if branch == "rawhide":
            return f"fedora-rawhide-{arch}"
        m = re.fullmatch(r"f(\d+)", branch)
        assert m, f"unsupported fedora branch: {branch}"
        return f"fedora-{m.group(1)}-{arch}"
    if distro == "centos":
        m = re.fullmatch(r"c(\d+)s", branch)
        assert m, f"unsupported centos branch: {branch}"
        return f"centos-stream-{m.group(1)}-{arch}"
    raise AssertionError(f"unsupported distro: {distro}, specify a --mock-config")


def mockbuild(
    packagename: str, distro: str | None = None, branch: str | None = None, root: str | None = None
) -> None:
    """Build a package locally with mock into _build/ -- a developer tool, not the build path.

    For debugging / forwarding changes upstream; the production build path is ENG-193. Assembles
    the srpm, then runs `mock` against the import source's buildroot (see mock_config) with our
    native dist tag (native_dist); the binary rpms and build logs land next to the srpm in _build/.
    """
    rel = resolve_main(packagename, distro, branch)
    distro, branch = rel.split("/")[1:3]
    srcrpm = srpm(packagename, distro, branch)
    logging.info("mock-building %s", srcrpm.name)
    subprocess.run(
        [
            "mock",
            "-r",
            root or mock_config(distro, branch),
            "--define",
            f"dist {native_dist(rel)}",
            f"--resultdir={_root() / '_build'}",
            str(srcrpm),
        ],
        check=True,
    )


def local_version_release(specfile: Path) -> tuple[str, str]:
    """Version and display Release from a spec, for packages with no upstream.

    Imported ones read the resolved version-release from srcpkg.json instead. %autorelease is shown
    literally (it can only be resolved at srpm/build time), and %{?dist} is dropped.
    """
    text = specfile.read_text()
    vm = re.search(r"^Version:\s*(\S+)", text, re.M)
    rm = re.search(r"^Release:\s*(\S+)", text, re.M)
    assert vm and rm, f"no Version/Release in {specfile}"
    release = "%autorelease" if "autorelease" in rm.group(1) else re.sub(r"%\{?\??dist\}?", "", rm.group(1))
    return cast(str, vm.group(1)), release


def srcpkg_vr(metafile: Path) -> str:
    """The built version-release recorded in a srcpkg.json, or '?' if none."""
    return build_vr(cast(SrcpkgMetadata, json.loads(metafile.read_text()))) or "?"


def diff_package(packagename: str, distro: str | None = None, branch: str | None = None) -> None:
    """Show a package's local modifications: its main tree against the imported upstream-rpm one.

    main and upstream-rpm share the packages/<distro>/<branch>/<pkg>/ path, so we diff that one
    path between the two; the sibling srcpkg.json is outside the dir, so metadata is ignored.
    Empty output means unmodified (a rebuild's Release: bump does show, as a real change).
    """
    rel = resolve_main(packagename, distro, branch)
    anchor = upstream_anchor(rel)
    if not anchor:
        raise SystemExit(f"{rel.removeprefix('packages/')}: native package (no upstream to diff against)")
    urc = git(
        "log", "upstream-rpm", "-1", "--format=%H", f"--grep=^X-Upstream-Commit: {anchor}$", cwd=ROOT
    ).strip()
    print(git("diff", f"{urc}:{rel}", f"HEAD:{rel}", cwd=ROOT), end="")


def list_packages() -> None:
    """Print a table of all packages on this branch.

    Columns: version/release, modification status, and whether a newer build is waiting on
    upstream-rpm.
    """
    wt = ensure_worktree()
    rows: list[tuple[str, ...]] = []
    for pkgdir in sorted(
        p for p in (_root() / "packages").glob("*/*/*") if p.is_dir() and is_package_entry(p)
    ):
        distro, branch, pkg = pkgdir.relative_to(_root() / "packages").parts
        rel = str(pkgdir.relative_to(_root()))
        source = f"{distro}/{branch}"
        anchor = upstream_anchor(rel)
        if not anchor:
            # No upstream: the spec is the only version source (%autorelease shown as-is).
            version, release = local_version_release(pkgdir / f"{pkg}.spec")
            rows.append((source, pkg, version, release, "native", "-"))
            continue
        # Imported: take the concrete version-release from our copied srcpkg.json, since the
        # spec's %autorelease cannot be resolved in-tree.
        vr = srcpkg_vr(_root() / f"{rel}.json")
        version, release = vr.rsplit("-", 1) if "-" in vr else (vr, "")
        # The upstream-rpm commit carrying our anchor (main and upstream-rpm share `rel`).
        urc = git(
            "log", "upstream-rpm", "-1", "--format=%H", f"--grep=^X-Upstream-Commit: {anchor}$", cwd=wt
        ).strip()
        status = "modified" if is_modified(f"HEAD:{rel}", f"{urc}:{rel}") else "clean"
        # Out of date iff upstream-rpm has newer commits than our anchor.
        latest = last_imported_sha(wt, distro, branch, pkg)
        upstream = "up to date" if latest == anchor else f"update -> {srcpkg_vr(wt / f'{rel}.json')}"
        rows.append((source, pkg, version, release, status, upstream))

    headers = ("SOURCE", "PACKAGE", "VERSION", "RELEASE", "STATUS", "UPSTREAM")
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    for row in (headers, *rows):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))


def commit_trailer(c: str, key: str) -> str:
    """The value of a single git trailer on commit `c`, or '' if absent."""
    return git("log", "-1", f"--format=%(trailers:key={key},valueonly)", c, cwd=ROOT).strip()


def spec_at(ref: str, rel: str) -> str:
    """A package's spec text as of a commit/ref, or '' if the spec doesn't exist there."""
    path = f"{rel}/{Path(rel).name}.spec"
    return (
        git("show", f"{ref}:{path}", cwd=ROOT) if git("ls-tree", ref, "--", path, cwd=ROOT).strip() else ""
    )


def check_commit(c: str) -> list[str]:
    """Validate one commit against the release/metadata conventions; return its violations.

    See check() for the branch-wide walk. Only commits that touch packages/ are checked; the
    per-branch BUCK is generated, so it doesn't count as a package change on its own.
    """
    names = git("diff-tree", "--no-commit-id", "-r", "--name-only", c, cwd=ROOT).split()
    if not any(p.startswith("packages/") for p in names):
        return []
    # The package path(s) -- packages/<distro>/<branch>/<pkg>, the `rel` used elsewhere -- this
    # commit touches, derived from the changed file paths. A change is either the package dir or
    # its sibling <pkg>.json; the per-branch BUCK is neither, so it maps to no package.
    rels = set()
    for p in names:
        parts = p.split("/")
        if (
            len(parts) == 4 and parts[3].endswith(".json") and is_package_entry(Path(parts[3]))
        ):  # packages/<distro>/<branch>/<pkg>.json
            rels.add("/".join([*parts[:3], parts[3][:-5]]))
        elif len(parts) >= 5:  # packages/<distro>/<branch>/<pkg>/...
            rels.add("/".join(parts[:4]))
    if not rels:  # only the branch BUCK or _properties.json
        return []

    errors: list[str] = []
    rel = sorted(rels)[0]  # our commits are per-package
    pkg = Path(rel).name
    jrel = f"{rel}.json"
    # A commit that removes the package entirely (unimport) is always allowed: nothing of it is
    # left -- neither spec nor metadata -- to hold to the release/rebuild conventions.
    if not git("ls-tree", c, "--", rel, jrel, cwd=ROOT).strip():
        return []
    upstream = commit_trailer(c, "X-Upstream-Commit")
    rebuild = commit_trailer(c, "X-Rebuild")
    autorel = uses_autorelease(spec_at(c, rel))
    # Whether the commit only refreshes generated metadata, as opposed to changing a build input.
    # The branch's hand-authored _properties.json is such an input (its rpmbuild_options change
    # what a package builds), so a commit touching it is not metadata-only despite being all json.
    json_only = all(p.endswith(".json") for p in names) and not any(
        p.endswith(f"/{BRANCH_PROPERTIES}") for p in names
    )
    # Whether the commit touched the package's sources, as opposed to only json metadata
    dir_changed = any(p.startswith(f"{rel}/") for p in names)
    # A local commit that discards our delta is exempt from the local-change rules below: it takes a
    # modification away rather than adding one, so it has no Release bump and no X-Rebuild: trailer
    # to show, and its release is the import's -- already established by its tree.
    reset = not upstream and resets_to_import(c, rel)

    def bad(msg: str) -> None:
        errors.append(f"{c[:12]} {pkg}: {msg}")

    # A *local* package change must come with metadata (release numbers live in Provides; this
    # guards against forgetting rpm-metadata or its git add). Import commits are exempt: upstream
    # routinely batches commits and builds only the last one (%autorelease counts them all), so an
    # upstream commit without its own published build legitimately carries no metadata -- the
    # import machinery writes json exactly where a build exists, and check cannot second-guess
    # that offline.
    meta: SrcpkgMetadata | None = None
    if jrel not in names:
        if not upstream:
            bad(f"changes the package but not {pkg}.json")
    elif git("ls-tree", c, "--", jrel, cwd=ROOT).strip():
        try:
            meta = cast(SrcpkgMetadata, json.loads(git("show", f"{c}:{jrel}", cwd=ROOT)))
        except json.JSONDecodeError as e:
            bad(f"{pkg}.json is not valid JSON ({e})")
    # An imported commit must trace back to upstream-rpm at the same path.
    if (
        upstream
        and not git(
            "log",
            "upstream-rpm",
            "-1",
            "--format=%H",
            f"--grep=^X-Upstream-Commit: {upstream}$",
            "--",
            rel,
            jrel,
            cwd=ROOT,
        ).strip()
    ):
        bad(f"X-Upstream-Commit {upstream[:12]} has no match on upstream-rpm at {rel}")
    # A local source change must be an %autorelease package or bump Release: per the release rules
    # (incl. native packages).
    if not upstream and dir_changed and not reset and not autorel:
        if err := release_bump_error(c, rel):
            bad(err)
    # A *local* metadata-only change is only legitimate as an %autorelease rebuild. An imported
    # one is fine as-is: upstream's rpmautospec mass rebuilds are empty dist-git commits
    # ("Rebuilt for ..."), mirrored 1:1 to keep the %autorelease count, and their build's
    # recomputed json is all such a commit has to show.
    if not upstream and json_only and not reset:
        if not autorel:
            bad("changes only metadata but is not an %autorelease package")
        if not rebuild:
            bad("changes only metadata but has no X-Rebuild: trailer")
    # An X-Rebuild is an %autorelease, metadata-only no-change rebuild.
    if rebuild and not autorel:
        bad("has X-Rebuild: but is not an %autorelease package")
    if rebuild and set(names) != {jrel}:
        bad(f"has X-Rebuild: but changes more than {pkg}.json")
    # We track changes in git, not %changelog.
    if not upstream and changelog_at(c, rel) != changelog_at(f"{c}^", rel):
        bad("local commit modifies %changelog")
    # The recorded %autorelease release must advance sanely against the metadata chain -- it is
    # never re-derived by counting upstream history: rpmautospec counts dist-git commits we don't
    # import (its count reaches past our import boundary, even past the %autorelease switch), the
    # raw Version: text is often a macro that never changes, and -b/-e/-p offsets defeat counting
    # altogether. The imported release is ground truth off the koji build's NEVR.
    if autorel and meta and build_vr(meta):
        got = build_release(meta, (build_dist(meta) or ".") + NATIVE_DIST)
        if upstream:
            # An import either carries the previous build's metadata forward (unbuilt commit, V-R
            # unchanged), resets the release with a version change, or advances it (upstream
            # counted more commits). Only enforced for the plain integer releases of upstream
            # rpmautospec; a first import (no parent json) was anchored against koji directly.
            if git("ls-tree", f"{c}^", "--", jrel, cwd=ROOT).strip():
                prev = cast(SrcpkgMetadata, json.loads(git("show", f"{c}^:{jrel}", cwd=ROOT)))
                pgot = build_release(prev, (build_dist(prev) or ".") + NATIVE_DIST)
                if (
                    build_vr(prev)
                    and build_version(prev) == build_version(meta)
                    and got != pgot
                    and got.isdigit()
                    and pgot.isdigit()
                    and int(got) <= int(pgot)
                ):
                    bad(f"imported release {got!r} does not advance on the previous {pgot!r}")
        else:
            # A local bump continues the last import's release with our minor '.N': N counts our
            # own commits since that import (the one place a commit count remains -- it is *our*
            # count, fully contained in our history).
            anchor = import_commit(rel, c)
            base = "0"  # native package: no upstream release to continue
            if anchor and git("ls-tree", anchor, "--", jrel, cwd=ROOT).strip():
                am = cast(SrcpkgMetadata, json.loads(git("show", f"{anchor}:{jrel}", cwd=ROOT)))
                base = build_release(am, (build_dist(am) or ".") + NATIVE_DIST)
            # No local commit counted (a curation change is not one, see above): the release is
            # still the import's, unsuffixed -- `.N` starts at `.1` (bumped_release).
            bump = local_bump(rel, c)
            want = f"{base}.{bump}" if bump else base
            if got != want:
                bad(f"%autorelease is {got!r} in Provides but git history implies {want!r}")
    # Arch buckets are refreshed by independent per-arch builds; they must agree on the NEVR.
    if meta:
        for skew in arch_vr_skew(meta):
            bad(skew)
    return errors


def release_bump_error(ref: str, rel: str) -> str | None:
    """Verify ref's Release: bump according to docs/user/importer.md rules"""

    now = re.search(r"^Release:\s*(\S+)", spec_at(ref, rel), re.M)
    if now is None:
        return "spec has no Release: line"
    was = re.search(r"^Release:\s*(\S+)", spec_at(f"{ref}^", rel), re.M)
    if was is None:
        # A native package's first commit has no parent Release:
        return None
    want = bumped_release(was.group(1), local_bump(rel, f"{ref}^"))
    if now.group(1) == want:
        return None
    return f"local commit must bump Release: from {was.group(1)} to {want}, not {now.group(1)}"


def changelog_at(ref: str, rel: str) -> str:
    """The spec's %changelog section (to end of file) as of a ref, or '' if none."""
    m = re.search(r"^%changelog.*", spec_at(ref, rel), re.M | re.S)
    return m.group() if m else ""


def check(start_ref: str | None = None) -> list[str]:
    """Validate the release/metadata conventions on every packages/-touching commit of the branch.

    Walks start_ref..HEAD (or all of HEAD when no ref is given, e.g. origin/main in a PR) and
    returns the collected violations, newest commit first; empty means clean. main() turns a
    non-empty result into a non-zero exit so a PR's CI fails. Metadata *integrity* isn't checked
    here -- only a rebuild can confirm that (packages.md).
    """
    ensure_branch()  # check_commit resolves X-Upstream-Commit: trailers against it
    revs = f"{start_ref}..HEAD" if start_ref else "HEAD"
    errors: list[str] = []
    for c in git("rev-list", revs, cwd=ROOT).split():
        errors += check_commit(c)
    return errors


def main() -> None:
    assert __doc__ is not None
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--debug", action="store_true", help="log every git/koji/bodhi step")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-upstream", help="import a new package from an upstream dist-git")
    p.add_argument("packagename")
    p.add_argument("distro", help="curated distro: " + ", ".join(DISTROS))
    p.add_argument("branch", help="upstream branch, e.g. 'rawhide' or 'f44'")
    p.add_argument("--sha", help="import this commit instead of the branch HEAD")

    sub.add_parser(
        "update-upstreams", help="import new upstream commits for all currently imported packages"
    )

    ip = sub.add_parser("import", help="copy a package from upstream-rpm onto the main branch")
    ip.add_argument("packagename")
    ip.add_argument("distro", nargs="?", help="disambiguate when imported from several sources")
    ip.add_argument("branch", nargs="?")

    up = sub.add_parser("update", help="apply new upstream-rpm commits for a package onto this branch")
    up.add_argument("packagename")
    up.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    up.add_argument("branch", nargs="?")

    sub.add_parser("update-all", help="run update for every imported package on this branch")

    rb = sub.add_parser("rebuild", help="generate a no-change rebuild commit (Release bump)")
    rb.add_argument("packagename")
    rb.add_argument("reason", help="what triggered it, e.g. openssl-3.5.0-1")
    rb.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    rb.add_argument("branch", nargs="?")

    rm = sub.add_parser("rpm-metadata", help="(re)compute a package srcpkg.json from local rpm files")
    rm.add_argument("packagename")
    rm.add_argument("rpms", nargs="+", help="built rpm files (binary rpms; optionally the .src.rpm)")
    rm.add_argument("--distro", help="disambiguate when present from several sources")
    rm.add_argument("--branch")

    sub.add_parser("list", help="table of all packages: version/release, status, upstream updates")

    cp = sub.add_parser("check", help="validate release/metadata conventions on branch commits")
    cp.add_argument("start_ref", nargs="?", help="check commits after this ref (default: all)")

    dp = sub.add_parser("diff", help="show a package's local modifications vs its imported upstream")
    dp.add_argument("packagename")
    dp.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    dp.add_argument("branch", nargs="?")

    yp = sub.add_parser("sync", help="update a package to the latest upstream, discarding our changes")
    yp.add_argument("packagename")
    yp.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    yp.add_argument("branch", nargs="?")

    sp = sub.add_parser("srpm", help="assemble a .src.rpm (rpmbuild -bs) for a package on this branch")
    sp.add_argument("packagename")
    sp.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    sp.add_argument("branch", nargs="?")

    mb = sub.add_parser("mockbuild", help="locally build a package with mock into _build/ (dev tool)")
    mb.add_argument("packagename")
    mb.add_argument("distro", nargs="?", help="disambiguate when present from several sources")
    mb.add_argument("branch", nargs="?")
    mb.add_argument("-r", "--root", help="chroot config or name; passed on to mock")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(message)s"
    )
    if ROOT is None:
        raise SystemExit(
            "no OS.git found: this tine cell is not part of a git checkout of a project that uses it"
        )
    if args.command == "import-upstream":
        import_upstream(args.distro, args.branch, args.packagename, args.sha)
    elif args.command == "update-upstreams":
        update_upstreams()
    elif args.command == "import":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        import_(args.packagename, args.distro, args.branch)
    elif args.command == "update":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        if not update(args.packagename, args.distro, args.branch):
            raise SystemExit(EXIT_CONFLICT)
    elif args.command == "update-all":
        if not update_all():
            raise SystemExit(EXIT_CONFLICT)
    elif args.command == "rebuild":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        rebuild(args.packagename, args.reason, args.distro, args.branch)
    elif args.command == "rpm-metadata":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        rpm_metadata(args.packagename, args.rpms, args.distro, args.branch)
    elif args.command == "list":
        list_packages()
    elif args.command == "check":
        errors = check(args.start_ref)
        for e in errors:
            logging.error(e)
        if errors:
            raise SystemExit(1)
    elif args.command == "diff":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        diff_package(args.packagename, args.distro, args.branch)
    elif args.command == "sync":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        if not sync(args.packagename, args.distro, args.branch):
            raise SystemExit(1)  # sync can never conflict by design
    elif args.command == "srpm":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        srpm(args.packagename, args.distro, args.branch)
    elif args.command == "mockbuild":
        assert (args.distro is None) == (args.branch is None), "specify both distro and branch, or neither"
        mockbuild(args.packagename, args.distro, args.branch, args.root)


if __name__ == "__main__":
    main()
