# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the rpm importer

    buck test tine//tools:importer-test

These are integration tests: they stand up a local dist-git repo, build *real* rpms from it
(so the metadata pipeline's `rpm -qp`/`rpmspec` calls run for real), and mock only the network
edges -- koji (XML-RPC), the koji rpm download, and the source lookaside.

They run in-process (patching the tool's module globals + network calls, and chdir-ing into the
temp monorepo), so they must run sequentially -- which plain unittest does. TestCLI is the one
out-of-process case, exercising the real argparse/dispatch layer.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, override
from unittest import mock

import importer as tool

TOOL_PATH = Path(__file__).parent / "importer.py"
# a deterministic dist tag for the built rpms (branch 'rawhide' picks the newest .fcNN, so any works)
DIST = ".fc99"


# our native %dist suffix (NATIVE_DIST in the tool); NDIST is the dist tag our own rebuilds
# of DIST imports carry
NATIVE_DIST = tool.NATIVE_DIST
NDIST = DIST + NATIVE_DIST

# Keep bytes, not paths into a previous test's deleted temporary directory.
_RPM_CACHE: dict[tuple[str, str, str, bytes, tuple[str, ...]], dict[Path, bytes]] = {}


def setUpModule() -> None:
    # The dev box contains rpmbuild only when it builds from a Fedora release.
    if shutil.which("rpmbuild") is None:
        raise unittest.SkipTest("the box contains no rpmbuild")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def init_repo(path: Path, branch: str) -> None:
    path.mkdir(parents=True)
    git("init", "--quiet", f"--initial-branch={branch}", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)


# A minimal noarch package: a runtime Requires and an extra Provides on top of the auto ones, so
# the metadata's relation lists have something to assert. {release} is a static release number.
NOARCH_SPEC = """\
Name:    {pkg}
Version: {version}
Release: {release}%{{?dist}}
Summary: Test package
License: MIT
BuildArch: noarch
Source0: {pkg}-%{{version}}.tar.gz
BuildRequires: sed
Requires: bash
Provides: {pkg}-api = %{{version}}

%description
Test package for the importer tests.

%prep
%setup -q

%build

%install
mkdir -p %{{buildroot}}%{{_datadir}}/{pkg}
install -m644 README %{{buildroot}}%{{_datadir}}/{pkg}/README

%files
%{{_datadir}}/{pkg}/README
"""

# Same, but rpmautospec-style: Release is %autorelease (resolved to <count>%{?dist} at build).
# (NOARCH_SPEC is a .format template, so its dist braces are doubled -- match that here.)
AUTORELEASE_SPEC = NOARCH_SPEC.replace("Release: {release}%{{?dist}}", "Release: %autorelease")
assert AUTORELEASE_SPEC != NOARCH_SPEC, "AUTORELEASE_SPEC replace did not match"

# A glibc-shaped package with both flavours of noarch subpackage, to pin the
# producing-arch bucketing. `-doc` is interchangeable noarch (declared for every arch);
# `-sysroot-<arch>` names the target cpu, so each arch's build declares only its own
# (glibc's per-arch cross-compilation sysroots).
MULTIARCH_SPEC = """\
%global debug_package %{nil}
%global _empty_manifest_terminate_build 0
Name:    sysrootpkg
Version: 1.0
Release: 1%{?dist}
Summary: Multiarch test package
License: MIT

%description
Main (arch) package.

%package doc
Summary: Interchangeable noarch docs
BuildArch: noarch
%description doc
Built identically on every arch.

%package sysroot-%{_target_cpu}
Summary: Cross sysroot for %{_target_cpu}
BuildArch: noarch
%description sysroot-%{_target_cpu}
Only this arch's build produces it.

%files

%files doc

%files sysroot-%{_target_cpu}
"""

# A kernel-shaped package: `-doc` is a noarch subpackage declared for every arch, but its %files
# is gated behind with_doc, which %ifnarch noarch forces off for any real build target (as
# kernel.spec does for kernel-doc). So only a noarch build produces it; an x86_64/aarch64 build
# declares it but builds no rpm -- rpmspec --builtrpms must not report it there.
KERNEL_DOC_SPEC = """\
%global debug_package %{nil}
%global _empty_manifest_terminate_build 0
Name:    kerneltest
Version: 1.0
Release: 1%{?dist}
Summary: Kernel-shaped test package
License: GPLv2

%define with_doc 1
%ifnarch noarch
%define with_doc 0
%endif

%description
Main (arch) package.

%package doc
Summary: Docs, only built in the noarch pass
BuildArch: noarch
%description doc
Only built when with_doc.

%files

%if %{with_doc}
%files doc
%endif
"""

# AUTORELEASE_SPEC plus a bcond-gated noarch subpackage with its own BuildRequires (as gcc.spec's
# basic bcond gates languages): only a build with --with=extra produces it, so rpm-metadata
# must evaluate the spec with the same options (the branch's rpmbuild_options).
BCOND_GATED_SPEC = (
    "%global _empty_manifest_terminate_build 0\n"
    "%bcond_with extra\n"
    + AUTORELEASE_SPEC
    + """
%if %{{with extra}}
%package extra
Summary: Bcond-gated extra subpackage
BuildArch: noarch
BuildRequires: gawk

%description extra
Only exists when built --with extra.

%files extra
%endif
"""
)

# Same shape gated on a plain macro instead of a bcond, for the --define option form.
MACRO_GATED_SPEC = (
    "%global _empty_manifest_terminate_build 0\n"
    + AUTORELEASE_SPEC
    + """
%if 0%{{?build_extra}}
%package extra
Summary: Macro-gated extra subpackage
BuildArch: noarch
BuildRequires: gawk

%description extra
Only exists when built with build_extra defined.

%files extra
%endif
"""
)


def make_tarball(pkg: str, version: str) -> bytes:
    """A minimal, valid <pkg>-<version>.tar.gz (with a top-level dir, so %setup -q works).

    Deterministic: fixed member + gzip mtimes, so the same (pkg, version) always yields identical
    bytes -- successive same-version commits then share a `sources` hash (and the mock lookaside
    serves one consistent tarball for it)."""
    data = b"hello\n"
    tarbuf = io.BytesIO()
    with tarfile.open(fileobj=tarbuf, mode="w") as tar:
        info = tarfile.TarInfo(f"{pkg}-{version}/README")
        info.size = len(data)  # info.mtime defaults to 0
        tar.addfile(info, io.BytesIO(data))
    gzbuf = io.BytesIO()
    with gzip.GzipFile(fileobj=gzbuf, mode="wb", mtime=0) as gz:
        gz.write(tarbuf.getvalue())
    return gzbuf.getvalue()


def build_rpms(
    topdir: Path,
    spec_text: str,
    pkg: str,
    version: str,
    tarball: bytes,
    defines: dict[str, str],
    dist: str = DIST,
    target: str | None = None,
    options: list[str] | None = None,
) -> list[Path]:
    """Build the srpm + binary rpm(s) from a spec, mimicking what koji would have produced.

    Pass `target` to build for another arch (`rpmbuild --target`); noarch content cross-builds
    fine, so this reproduces koji's per-arch tasks (and their arch-varying subpackage sets).
    `options` are extra raw rpmbuild CLI options (--with/--without/--define).
    """
    for sub in ("SOURCES", "SPECS"):
        (topdir / sub).mkdir(parents=True)
    (topdir / "SOURCES" / f"{pkg}-{version}.tar.gz").write_bytes(tarball)
    spec = topdir / "SPECS" / f"{pkg}.spec"
    spec.write_text(spec_text)
    # These fixtures have at most a few files; extra workers cost more than they save.
    cmd = [
        "-ba",
        "--define",
        "_smp_build_nthreads 1",
        "--define",
        f"dist {dist}",
        *(options or []),
    ]
    if target:
        cmd += ["--target", target]
    for k, v in defines.items():
        cmd += ["--define", f"{k} {v}"]
    key = (pkg, version, spec_text, tarball, tuple(cmd))
    if key not in _RPM_CACHE:
        subprocess.run(
            ["rpmbuild", "--define", f"_topdir {topdir}", *cmd, str(spec)],
            check=True,
            capture_output=True,
        )
        _RPM_CACHE[key] = {r.relative_to(topdir): r.read_bytes() for r in sorted(topdir.rglob("*.rpm"))}
    else:
        for relative, data in _RPM_CACHE[key].items():
            rpm = topdir / relative
            rpm.parent.mkdir(parents=True, exist_ok=True)
            rpm.write_bytes(data)
    return [topdir / relative for relative in _RPM_CACHE[key]]


class FakeKoji:
    """A stand-in koji hub: answers the three calls the importer makes, keyed by build."""

    def __init__(self, package_id: int, builds: list[dict[str, Any]]) -> None:
        self._package_id = package_id
        self._builds = builds  # each: koji build fields + private '_sha' and '_rpms'

    def getPackageID(self, name: str) -> int:
        return self._package_id

    def listBuilds(
        self,
        packageID: object = None,
        userID: object = None,
        taskID: object = None,
        prefix: object = None,
        state: object = None,
        volumeID: object = None,
        source: str | None = None,
        *rest: object,
    ) -> list[dict[str, Any]]:
        # The importer only ever queries by exact dist-git source (git+<url>#<sha>).
        return [
            {k: v for k, v in b.items() if not k.startswith("_")}
            for b in self._builds
            if source and source.endswith("#" + b["_sha"])
        ]

    def listBuildRPMs(self, build_id: int) -> list[dict[str, Any]]:
        return next(b["_rpms"] for b in self._builds if b["build_id"] == build_id)

    def listTags(self, build_id: int) -> list[dict[str, str]]:
        return []


class PackagesTestCase(unittest.TestCase):
    """Shared scaffolding for all verb tests.

    A temp monorepo (with an orphan upstream-rpm branch), local dist-git repos with real built
    rpms, and mocked network edges (koji, the rpm download, the source lookaside). Subclasses add
    the actual per-verb tests.
    """

    @override
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="import-test-"))
        self._oldcwd = Path.cwd()
        self.tool = tool

        # A monorepo with an (orphan, empty) upstream-rpm branch for the worktree to check out.
        self.monorepo = self._tmp / "monorepo"
        init_repo(self.monorepo, "main")
        git("commit", "--quiet", "--allow-empty", "-m", "init", cwd=self.monorepo)
        git("checkout", "--quiet", "--orphan", "upstream-rpm", cwd=self.monorepo)
        git("commit", "--quiet", "--allow-empty", "-m", "init upstream-rpm", cwd=self.monorepo)
        git("checkout", "--quiet", "main", cwd=self.monorepo)

        # Point the tool at the temp monorepo; the tool runs git in the CWD, so chdir there.
        os.chdir(self.monorepo)
        self._patches: list[Any] = [
            mock.patch.object(self.tool, "ROOT", self.monorepo),
            mock.patch.object(self.tool, "WORKTREE", self.monorepo / ".upstream-rpm"),
            mock.patch.dict(self.tool.DISTROS),  # snapshot; make_upstream retargets a distro
            mock.patch("xmlrpc.client.ServerProxy", side_effect=self._server_proxy),
            mock.patch("urllib.request.urlopen", side_effect=self._urlopen),
        ]
        for p in self._patches:
            p.start()

        self._rpms: dict[str, Path] = {}  # rpm basename -> built rpm file
        self._lookaside: dict[str, bytes] = {}  # source filename -> bytes
        self._builds: list[dict[str, Any]] = []  # koji builds; FakeKoji reads this live
        self._koji = FakeKoji(1, self._builds)

        # All distros clone/fetch from our local dist-git tree instead of the real hosts.
        self.distgit = self._tmp / "dist-git"
        self.distgit.mkdir()
        for d in self.tool.DISTROS:
            self.tool.DISTROS[d] = {**self.tool.DISTROS[d], "dist_git": f"{self.distgit}/"}

    @override
    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        os.chdir(self._oldcwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- mock network edges -------------------------------------------------------------

    def _server_proxy(self, url: str, *a: object, **k: object) -> FakeKoji:
        return self._koji

    def _urlopen(self, url: str, *a: object, **k: object) -> io.BytesIO:
        # Serve both koji rpm downloads (self._rpms) and lookaside sources (self._lookaside).
        name = url.rsplit("/", 1)[-1]
        if name in self._rpms:
            return io.BytesIO(self._rpms[name].read_bytes())
        return io.BytesIO(self._lookaside[name])

    # --- fixture builder ----------------------------------------------------------------

    def commit(
        self, pkg: str, branch: str, version: str, release: str, msg: str, autorelease: bool = False
    ) -> str:
        """Add a dist-git commit for `pkg` (creating its local repo on first call); return the sha.

        Writes the spec (+ a per-commit marker so successive commits differ) and a `sources` file,
        and registers the source tarball with the mock lookaside."""
        repo = self.distgit / f"{pkg}.git"
        if not repo.exists():
            init_repo(repo, branch)
        tmpl = AUTORELEASE_SPEC if autorelease else NOARCH_SPEC
        (repo / f"{pkg}.spec").write_text(
            tmpl.format(pkg=pkg, version=version, release=release) + f"\n# {msg}\n"
        )
        tarball = make_tarball(pkg, version)
        self._lookaside[f"{pkg}-{version}.tar.gz"] = tarball
        (repo / "sources").write_text(
            f"SHA512 ({pkg}-{version}.tar.gz) = {hashlib.sha512(tarball).hexdigest()}\n"
        )
        git("add", ".", cwd=repo)
        git("commit", "--quiet", "-m", msg, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    def build_koji(self, pkg: str, version: str, release: str, sha: str, autorelease: bool = False) -> None:
        """Build the rpms a koji build for `sha` would have produced.

        Builds at <version>-<release> and registers the build with the mock koji + rpm download map.
        """
        tmpl = AUTORELEASE_SPEC if autorelease else NOARCH_SPEC
        spec_text = tmpl.format(pkg=pkg, version=version, release=release)
        # %autorelease isn't defined without rpmautospec; supply the resolved value for the build.
        defines = {"autorelease": f"{release}%{{?dist}}", "autochangelog": "%nil"} if autorelease else {}
        topdir = self._tmp / "build" / f"{pkg}-{version}-{release}"
        for r in build_rpms(
            topdir, spec_text, pkg, version, self._lookaside[f"{pkg}-{version}.tar.gz"], defines
        ):
            self._rpms[r.name] = r
        rel = f"{release}{DIST}"
        self._builds.append(
            {
                "build_id": len(self._builds) + 1,
                "nvr": f"{pkg}-{version}-{rel}",
                "name": pkg,
                "version": version,
                "release": rel,
                "_sha": sha,
                "_rpms": [
                    {"name": pkg, "version": version, "release": rel, "arch": a} for a in ("src", "noarch")
                ],
            }
        )

    def build_targets(
        self, spec_text: str, pkg: str, version: str, targets: list[str]
    ) -> tuple[list[Path], Path]:
        """Build a spec once per rpm --target; return (koji-style union of binary rpms, spec path).

        Mimics koji's per-arch tasks: same-named noarch rpms are deduped to one (koji keeps a
        single copy), arch rpms stay distinct. The srpm is dropped -- these fixtures exercise
        binary bucketing, not BuildRequires.
        """
        tarball = make_tarball(pkg, version)
        rpms: dict[str, Path] = {}
        spec = self._tmp / "targetbuild" / f"{pkg}.spec"  # set below to the last build's copy
        for t in targets:
            topdir = self._tmp / "targetbuild" / t
            for r in build_rpms(topdir, spec_text, pkg, version, tarball, {}, target=t):
                rpms[r.name] = r
            spec = topdir / "SPECS" / f"{pkg}.spec"
        return [r for r in rpms.values() if not r.name.endswith(".src.rpm")], spec

    def make_upstream(
        self, pkg: str, distro: str, branch: str, version: str = "1.0", release: str = "1"
    ) -> str:
        """Commit + build a single-commit, static-release package; return the dist-git sha."""
        sha = self.commit(pkg, branch, version, release, f"Update to {version}")
        self.build_koji(pkg, version, release, sha)
        return sha

    def imported_shas(self, rel: str, ref: str) -> list[str]:
        """The X-Upstream-Commit trailers on `ref` for a package path, newest first.

        `ref` is 'upstream-rpm' or 'main' (main carries the same trailers, cherry-picked from
        upstream-rpm).
        """
        out = git(
            "log",
            "--format=%(trailers:key=X-Upstream-Commit,valueonly)",
            ref,
            "--",
            rel,
            f"{rel}.json",
            cwd=self.monorepo,
        )
        return [ln for ln in out.splitlines() if ln]

    def seed(self, pkg: str, distro: str = "fedora", branch: str = "rawhide", **kw: str) -> str:
        """Put a package on the upstream-rpm branch; return the dist-git sha it was imported from.

        The precondition for the main-branch verbs (import/update/diff/...).
        """
        sha = self.make_upstream(pkg, distro, branch, **kw)
        self.tool.import_upstream(distro, branch, pkg, sha)
        return sha

    def modify(
        self,
        rel: str,
        old: str,
        new: str,
        addfile: str | None = None,
        release: tuple[str, str] | None = None,
    ) -> None:
        """Make (and commit) a local spec modification on main, as a developer would.

        Optionally adds a new file (a downstream patch) and advances the release from `release`'s
        (old, new) pair, in the spec and in the recorded metadata -- what the conventions demand of a
        local change and `check` enforces (an %autorelease spec has no Release: line to bump, so only
        the metadata moves). Tests that don't run `check` can leave it out.
        """
        if addfile:
            (self.monorepo / rel / addfile).write_text(f"# {addfile}\n")
        spec = self.monorepo / rel / f"{Path(rel).name}.spec"
        spec.write_text(spec.read_text().replace(old, new))
        if release:
            spec.write_text(spec.read_text().replace(f"Release: {release[0]}%", f"Release: {release[1]}%"))
            metafile = self.monorepo / f"{rel}.json"
            meta = json.loads(metafile.read_text())
            for arch in meta["binaries"].values():
                for binmeta in arch.values():
                    for key in ("Provides", "Requires", "Recommends"):
                        binmeta[key] = [
                            s.replace(f"-{release[0]}{NDIST}", f"-{release[1]}{NDIST}") for s in binmeta[key]
                        ]
            self.tool.project_build_fields(meta, meta.get("source_date_epoch") or 0)
            self.tool.write_json(metafile, meta)
        git("add", "-A", "--", rel, f"{rel}.json", cwd=self.monorepo)
        git("commit", "--quiet", "-m", f"{Path(rel).name}: local patch", cwd=self.monorepo)

    def capture(self, fn: Callable[..., object], *args: object) -> str:
        """Run a verb that prints to stdout (diff/list) and return its captured output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue()


class UpstreamPackages(PackagesTestCase):
    """`import-upstream` and `update-upstreams`.

    Mirror upstream dist-git commits (and their koji build metadata) onto the upstream-rpm branch.
    """

    def test_ensure_branch_from_origin(self) -> None:
        """A fresh CI clone has only origin/upstream-rpm; the tool creates the local branch."""
        git("update-ref", "refs/remotes/origin/upstream-rpm", "upstream-rpm", cwd=self.monorepo)
        git("branch", "--quiet", "-D", "upstream-rpm", cwd=self.monorepo)

        wt = self.tool.ensure_worktree()

        self.assertTrue((wt / ".git").exists())
        self.assertEqual(
            git("rev-parse", "upstream-rpm", cwd=self.monorepo),
            git("rev-parse", "refs/remotes/origin/upstream-rpm", cwd=self.monorepo),
        )

    def test_worktree_refreshes_from_origin(self) -> None:
        """A stale clone's branch and worktree fast-forward to origin; an ahead one stays."""
        origin = self._tmp / "origin.git"
        git("clone", "--quiet", "--bare", str(self.monorepo), str(origin), cwd=self._tmp)
        git("remote", "add", "origin", str(origin), cwd=self.monorepo)

        # Simulate the mirror advancing: push a commit, then rewind the local state to before it.
        wt = self.tool.ensure_worktree()
        git("commit", "--quiet", "--allow-empty", "-m", "newer import", cwd=wt)
        tip = git("rev-parse", "HEAD", cwd=wt).strip()
        git("push", "--quiet", "origin", "upstream-rpm", cwd=wt)
        git("reset", "--quiet", "--hard", "HEAD^", cwd=wt)
        git("update-ref", "-d", "refs/remotes/origin/upstream-rpm", cwd=self.monorepo)

        self.tool.ensure_worktree()
        self.assertEqual(git("rev-parse", "upstream-rpm", cwd=self.monorepo).strip(), tip)

        # A branch that is ahead (imports not pushed yet) is left alone.
        git("commit", "--quiet", "--allow-empty", "-m", "local import", cwd=wt)
        ahead = git("rev-parse", "HEAD", cwd=wt).strip()
        self.tool.ensure_worktree()
        self.assertEqual(git("rev-parse", "upstream-rpm", cwd=self.monorepo).strip(), ahead)

        # Unpushed changes that collide with origin's fail.
        git("push", "--quiet", "origin", "upstream-rpm", cwd=wt)
        git("reset", "--quiet", "--hard", "HEAD^", cwd=wt)
        git("commit", "--quiet", "--allow-empty", "-m", "colliding import", cwd=wt)
        with self.assertRaises(subprocess.CalledProcessError):
            self.tool.ensure_worktree()

    def test_worktree_initializes_origin(self) -> None:
        """A fresh OS.git without any imports gets an empty upstream-rpm branch on origin."""
        git("branch", "--quiet", "-D", "upstream-rpm", cwd=self.monorepo)
        origin = self._tmp / "origin.git"
        git("clone", "--quiet", "--bare", str(self.monorepo), str(origin), cwd=self._tmp)
        git("remote", "add", "origin", str(origin), cwd=self.monorepo)

        wt = self.tool.ensure_worktree()

        tip = git("rev-parse", "upstream-rpm", cwd=origin)
        self.assertEqual(git("rev-parse", "HEAD", cwd=wt), tip)
        self.assertEqual(git("log", "--format=%s", tip, cwd=origin), "Initialize upstream-rpm branch")
        self.assertEqual(git("ls-tree", tip, cwd=origin), "")

    def test_new_package(self) -> None:
        sha = self.make_upstream("testpkg", "fedora", "rawhide")
        self.tool.import_upstream("fedora", "rawhide", "testpkg", None)

        wt = self.monorepo / ".upstream-rpm"
        rel = "packages/fedora/rawhide/testpkg"

        # The dist-git tree is mirrored 1:1.
        self.assertTrue((wt / rel / "testpkg.spec").is_file())
        self.assertTrue((wt / rel / "sources").is_file())

        # Exactly one commit, preserving the upstream subject + linking the source sha.
        log = git("log", "--format=%s%n%b", "upstream-rpm", "--", rel, cwd=self.monorepo)
        self.assertIn("[fedora/rawhide/testpkg] Update to 1.0", log)
        self.assertIn(f"X-Upstream-Commit: {sha}", log)

        # srcpkg.json: binaries bucketed under each build arch, BuildRequires, and source sha256.
        meta = json.loads((wt / f"{rel}.json").read_text())
        self.assertEqual(sorted(meta["binaries"]), ["aarch64", "x86_64"])
        binmeta = meta["binaries"]["x86_64"]["testpkg"]
        self.assertIn("testpkg = 1.0-1.fc99", binmeta["Provides"])  # auto self-provide
        self.assertIn("testpkg-api = 1.0", binmeta["Provides"])  # explicit extra Provides
        self.assertIn("bash", binmeta["Requires"])  # explicit Requires
        self.assertIn("/usr/share/testpkg/README", binmeta["Files"])
        self.assertEqual(meta["build_requires"]["_all"], ["sed"])
        self.assertEqual(len(meta["sources"]), 1)
        self.assertEqual(
            meta["sources"][0]["sha256sum"],
            hashlib.sha256(self._lookaside["testpkg-1.0.tar.gz"]).hexdigest(),
        )
        self.assertEqual(meta["sources"][0]["size"], len(self._lookaside["testpkg-1.0.tar.gz"]))
        self.assertTrue(meta["sources"][0]["url"].endswith("/testpkg-1.0.tar.gz"))

    def test_download_retries_transient_reset(self) -> None:
        """A connection reset mid-download is retried, so the import still completes."""
        sha = self.make_upstream("flaky", "fedora", "rawhide")

        attempts = {"n": 0}

        def reset_once(url: str, *a: object, **k: object) -> object:
            attempts["n"] += 1
            if attempts["n"] == 1:  # first download drops the connection mid-body, like the CI failures
                resp = mock.MagicMock()
                resp.__exit__.return_value = False  # do not swallow the error raised inside the with
                resp.__enter__.return_value.read.side_effect = ConnectionResetError(104, "reset")
                return resp
            return self._urlopen(url, *a, **k)

        # time.sleep is patched out so the back-off does not slow the test.
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", side_effect=reset_once):
            self.tool.import_upstream("fedora", "rawhide", "flaky", sha)

        # The reset was injected and the import still finished: the mirror was written.
        self.assertGreaterEqual(attempts["n"], 2)  # first attempt reset, then retried
        rel = "packages/fedora/rawhide/flaky"
        self.assertTrue((self.monorepo / ".upstream-rpm" / rel / "flaky.spec").is_file())

    def test_gitignored_tracked_files_imported(self) -> None:
        """Files upstream tracks are imported even when the package's .gitignore names them.

        Fedora dist-git .gitignores routinely list committed files (stale lookaside-style entries,
        e.g. p11-kit ignores its own trust-extract-compat and p11-kit-client.service). git only
        ignores *untracked* files, so upstream keeps them tracked -- and so must our mirror.
        """
        repo = self.distgit / "testpkg.git"
        self.commit("testpkg", "rawhide", "1.0", "1", "Initial import")  # creates the repo
        # Two files upstream committed despite listing them in its .gitignore.
        extra = ["trust-extract-compat", "p11-kit-client.service"]
        (repo / ".gitignore").write_text("".join(f"/{f}\n" for f in extra))
        for f in extra:
            (repo / f).write_text(f"# {f}\n")
        git("add", "--force", *extra, cwd=repo)
        git("add", ".gitignore", cwd=repo)
        git("commit", "--quiet", "-m", "Ship helper files", cwd=repo)
        sha = git("rev-parse", "HEAD", cwd=repo).strip()
        self.build_koji("testpkg", "1.0", "1", sha)

        self.tool.import_upstream("fedora", "rawhide", "testpkg", sha)
        self.tool.import_("testpkg", None, None)

        rel = "packages/fedora/rawhide/testpkg"
        for ref in ("upstream-rpm", "main"):
            tracked = git("ls-tree", "-r", "--name-only", ref, "--", rel, cwd=self.monorepo).split()
            for f in extra:
                self.assertIn(f"{rel}/{f}", tracked, f"{f} missing on {ref}")

    def test_autorelease_import_single_commit(self) -> None:
        """Importing an %autorelease package takes only the target commit, no history.

        The release lives in the imported srcpkg.json (ground truth off the koji NEVR), so no
        commit-count context is needed -- deeper history stays upstream. An explicit --sha
        without a published build is refused: only a build can generate the metadata.
        """
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial import", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        c2 = self.commit("arp", "rawhide", "1.0", "2", "Rebuild", autorelease=True)
        self.build_koji("arp", "1.0", "2", c2, autorelease=True)
        c3 = self.commit("arp", "rawhide", "1.0", "3", "Unbuilt tip", autorelease=True)

        with self.assertRaisesRegex(SystemExit, "no published rawhide build"):
            self.tool.import_upstream("fedora", "rawhide", "arp", c3)

        self.tool.import_upstream("fedora", "rawhide", "arp", None)  # newest published: c2

        rel = "packages/fedora/rawhide/arp"
        self.assertEqual(self.imported_shas(rel, "upstream-rpm"), [c2])
        # The metadata carries the resolved release (upstream's autorelease count -> 2).
        meta = json.loads((self.monorepo / ".upstream-rpm" / f"{rel}.json").read_text())
        self.assertIn("arp = 1.0-2.fc99", meta["binaries"]["x86_64"]["arp"]["Provides"])

    def test_unbuilt_noop_upstream_commit_skipped(self) -> None:
        """An unbuilt empty upstream commit is not mirrored at all.

        It changes nothing we track and no build refreshed the json; nothing consumes such a
        commit (on our side the release is not a commit count, it lives in srcpkg.json), so
        update-upstreams skips it. The next build's release jumps over it -- rpmautospec still
        counted it upstream.
        """
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial import", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c1)
        git(
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Rebuilt for Mass Rebuild",
            cwd=self.distgit / "arp.git",
        )
        c3 = self.commit("arp", "rawhide", "1.0", "3", "Fix", autorelease=True)
        self.build_koji("arp", "1.0", "3", c3, autorelease=True)

        self.tool.update_upstreams()

        rel = "packages/fedora/rawhide/arp"
        self.assertEqual(self.imported_shas(rel, "upstream-rpm"), [c3, c1])  # no mass rebuild
        meta = json.loads((self.monorepo / ".upstream-rpm" / f"{rel}.json").read_text())
        self.assertIn("arp = 1.0-3.fc99", meta["binaries"]["x86_64"]["arp"]["Provides"])

    def test_update_upstreams(self) -> None:
        """update-upstreams advances out-of-date packages, leaving current ones untouched."""
        # Package A: imported at its HEAD (up to date).
        a = self.make_upstream("aaa", "fedora", "rawhide", "1.0", "1")
        self.tool.import_upstream("fedora", "rawhide", "aaa", a)
        # Package B: two commits (1.0 then 2.0); import only the older one.
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        b2 = self.commit("bbb", "rawhide", "2.0", "1", "Update to 2.0")
        self.build_koji("bbb", "2.0", "1", b2)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)

        rel_a = "packages/fedora/rawhide/aaa"
        rel_b = "packages/fedora/rawhide/bbb"
        self.assertEqual(self.imported_shas(rel_a, "upstream-rpm"), [a])
        self.assertEqual(self.imported_shas(rel_b, "upstream-rpm"), [b1])
        a_commit = git("log", "-1", "--format=%H", "upstream-rpm", "--", rel_a, cwd=self.monorepo)

        self.tool.update_upstreams()

        # B advanced to its HEAD (both commits now present); A is untouched.
        self.assertEqual(self.imported_shas(rel_b, "upstream-rpm"), [b2, b1])
        self.assertEqual(self.imported_shas(rel_a, "upstream-rpm"), [a])
        self.assertEqual(
            git("log", "-1", "--format=%H", "upstream-rpm", "--", rel_a, cwd=self.monorepo), a_commit
        )
        meta = json.loads((self.monorepo / ".upstream-rpm" / f"{rel_b}.json").read_text())
        self.assertIn("bbb = 2.0-1.fc99", meta["binaries"]["x86_64"]["bbb"]["Provides"])

    def test_unsupported_distro(self) -> None:
        with self.assertRaises(AssertionError):
            self.tool.import_upstream("bogus", "rawhide", "testpkg", None)

    def test_unsupported_branch(self) -> None:
        # A real dist-git branch, but not a coordinate we support (fedora is rawhide / f<N>): the
        # clone and build lookup succeed, then the branch->dist mapping rejects it.
        self.make_upstream("ebp", "fedora", "epel10")
        with self.assertRaisesRegex(AssertionError, "branch"):
            self.tool.import_upstream("fedora", "epel10", "ebp", None)

    def test_nonexistent_package(self) -> None:
        # The local clone fails immediately; skip the retry delays but check their schedule.
        with (
            mock.patch("time.sleep") as sleep,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.tool.import_upstream("fedora", "rawhide", "ghost", None)
        self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (2, 4, 8, 16)])


class ArchBucketing(PackagesTestCase):
    """metadata_from_rpms buckets binaries by the build arch that *produces* each rpm.

    An arch rpm goes under its own arch; a noarch rpm under every build arch whose spec
    evaluation (rpmspec --target --builtrpms) actually builds an rpm for its name.
    """

    def test_noarch_bucketed_by_producing_arch(self) -> None:
        """Interchangeable noarch lands in every arch; arch-specific noarch only in its own."""
        rpms, spec = self.build_targets(MULTIARCH_SPEC, "sysrootpkg", "1.0", ["x86_64", "aarch64", "s390x"])
        meta = self.tool.metadata_from_rpms(rpms, spec)

        # The main arch rpm and the interchangeable -doc land in each build arch; the per-arch
        # sysroot lands only in its own. No build arch produces the s390x rpm.
        self.assertEqual(
            set(meta["binaries"]["x86_64"]), {"sysrootpkg", "sysrootpkg-doc", "sysrootpkg-sysroot-x86_64"}
        )
        self.assertEqual(
            set(meta["binaries"]["aarch64"]), {"sysrootpkg", "sysrootpkg-doc", "sysrootpkg-sysroot-aarch64"}
        )

    def test_noarch_only_subpackage_dropped(self) -> None:
        """A subpackage no build arch produces (its %files gated off) is dropped, not misattributed.

        koji's noarch pass builds kerneltest-doc, so it's in the fetched rpm set; but with_doc is
        forced off for x86_64/aarch64, so neither build produces it. --builtrpms reflects that, and
        it lands in no bucket (we do no separate noarch build, so it's simply not shipped).
        """
        arch_rpms, spec = self.build_targets(KERNEL_DOC_SPEC, "kerneltest", "1.0", ["x86_64"])
        noarch_rpms, _ = self.build_targets(KERNEL_DOC_SPEC, "kerneltest", "1.0", ["noarch"])
        doc = [r for r in noarch_rpms if r.name.startswith("kerneltest-doc")]
        self.assertTrue(doc, "noarch build should produce kerneltest-doc")
        meta = self.tool.metadata_from_rpms(arch_rpms + doc, spec)
        self.assertEqual(set(meta["binaries"]["x86_64"]), {"kerneltest"})


class DownstreamPackages(PackagesTestCase):
    """`import`, `update`, `update-all`, etc.

    Bring packages (and their new upstream commits) from the upstream-rpm mirror onto the main
    build branch, predicting the native dist tag and generating the buck projection.
    """

    def test_import(self) -> None:
        sha = self.seed("testpkg")  # testpkg lives on upstream-rpm
        self.tool.import_("testpkg", None, None)

        rel = "packages/fedora/rawhide/testpkg"
        # Landed on main: the dir, plus a srcpkg.json with the *predicted* native dist tag (.fc99aos,
        # vs the pristine .fc99 on upstream-rpm).
        self.assertTrue((self.monorepo / rel / "testpkg.spec").is_file())
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"testpkg = 1.0-1{NDIST}", meta["binaries"]["x86_64"]["testpkg"]["Provides"])
        # The buck projection is generated: the per-branch BUCK loads this package's .json.
        buck = (self.monorepo / "packages/fedora/rawhide/BUCK").read_text()
        self.assertIn('load(":testpkg.json"', buck)
        self.assertIn("rpm.branch(", buck)
        # 1:1 commit correspondence: the upstream sha is preserved on main.
        self.assertEqual(self.imported_shas(rel, "main"), [sha])

    def test_update(self) -> None:
        # v1.0 imported to upstream-rpm and main; then v2.0 lands upstream.
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)
        self.tool.import_("bbb", None, None)
        b2 = self.commit("bbb", "rawhide", "2.0", "1", "Update to 2.0")
        self.build_koji("bbb", "2.0", "1", b2)
        self.tool.update_upstreams()  # upstream-rpm -> v2.0

        self.tool.update("bbb")  # main -> v2.0

        rel = "packages/fedora/rawhide/bbb"
        self.assertEqual(self.imported_shas(rel, "main"), [b2, b1])  # both commits, 1:1
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"bbb = 2.0-1{NDIST}", meta["binaries"]["x86_64"]["bbb"]["Provides"])

    def test_update_modified(self) -> None:
        """A non-conflicting local modification is preserved across an update."""
        rel = "packages/fedora/rawhide/bbb"
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)
        self.tool.import_("bbb", None, None)
        # Local downstream change on a line the upstream update doesn't touch.
        self.modify(rel, "Summary: Test package", "Summary: Patched downstream")
        b2 = self.commit("bbb", "rawhide", "2.0", "1", "Update to 2.0")
        self.build_koji("bbb", "2.0", "1", b2)
        self.tool.update_upstreams()

        self.assertTrue(self.tool.update("bbb"))  # merges cleanly -> success

        spec = (self.monorepo / rel / "bbb.spec").read_text()
        self.assertIn("Version: 2.0", spec)  # upstream change applied
        self.assertIn("Summary: Patched downstream", spec)  # local modification kept
        self.assertNotIn("CONFLICT", git("log", "--format=%s", "main", "--", rel, cwd=self.monorepo))

    def test_update_conflict(self) -> None:
        """A local edit that collides with an upstream change

        It is committed with conflict markers, with a CONFLICT: subject, and the verb
        exits with EXIT_CONFLICT (the update bot opens a draft PR).
        """
        rel = "packages/fedora/rawhide/bbb"
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)
        self.tool.import_("bbb", None, None)
        # Local edit to the very line the upstream update also changes (the Version).
        self.modify(rel, "Version: 1.0", "Version: 1.0.local")
        b2 = self.commit("bbb", "rawhide", "2.0", "1", "Update to 2.0")
        self.build_koji("bbb", "2.0", "1", b2)
        self.tool.update_upstreams()

        self.assertFalse(self.tool.update("bbb"))  # conflict -> False (main() exits EXIT_CONFLICT)

        # The conflict is committed on main with a CONFLICT: subject and the markers kept.
        subject = git("log", "-1", "--format=%s", "main", "--", rel, cwd=self.monorepo)
        self.assertTrue(subject.startswith("CONFLICT: bbb"), subject)
        spec = (self.monorepo / rel / "bbb.spec").read_text()
        # computed markers, so conflict scans over the source tree don't trip on this test
        self.assertIn("<" * 7, spec)
        self.assertIn(">" * 7, spec)
        # Both sides of the clash are kept: our local Version and the upstream one.
        self.assertIn("Version: 1.0.local", spec)  # ours (local modification)
        self.assertIn("Version: 2.0", spec)  # theirs (upstream update)

    def test_update_all(self) -> None:
        # Two packages on main at v1.0, each with a v2.0 waiting upstream.
        for pkg in ("ppp", "qqq"):
            c1 = self.commit(pkg, "rawhide", "1.0", "1", "Update to 1.0")
            self.build_koji(pkg, "1.0", "1", c1)
            self.tool.import_upstream("fedora", "rawhide", pkg, c1)
            self.tool.import_(pkg, None, None)
        for pkg in ("ppp", "qqq"):
            c2 = self.commit(pkg, "rawhide", "2.0", "1", "Update to 2.0")
            self.build_koji(pkg, "2.0", "1", c2)
        self.tool.update_upstreams()

        self.tool.update_all()  # updates every imported package on main

        for pkg in ("ppp", "qqq"):
            rel = f"packages/fedora/rawhide/{pkg}"
            meta = json.loads((self.monorepo / f"{rel}.json").read_text())
            self.assertIn(f"{pkg} = 2.0-1{NDIST}", meta["binaries"]["x86_64"][pkg]["Provides"])

    def test_update_all_conflict(self) -> None:
        """update-all keeps going past a conflicting package and exits EXIT_CONFLICT at the end."""
        for pkg in ("aaa", "zzz"):
            c1 = self.commit(pkg, "rawhide", "1.0", "1", "Update to 1.0")
            self.build_koji(pkg, "1.0", "1", c1)
            self.tool.import_upstream("fedora", "rawhide", pkg, c1)
            self.tool.import_(pkg, None, None)
        # aaa (processed first) gets a colliding local edit; zzz stays clean.
        self.modify("packages/fedora/rawhide/aaa", "Version: 1.0", "Version: 1.0.local")
        for pkg in ("aaa", "zzz"):
            c2 = self.commit(pkg, "rawhide", "2.0", "1", "Update to 2.0")
            self.build_koji(pkg, "2.0", "1", c2)
        self.tool.update_upstreams()

        self.assertFalse(self.tool.update_all())  # a conflict -> False (main() exits EXIT_CONFLICT)

        # The conflicting package didn't block the clean one: zzz still advanced to 2.0.
        zzz = json.loads((self.monorepo / "packages/fedora/rawhide/zzz.json").read_text())
        self.assertIn(f"zzz = 2.0-1{NDIST}", zzz["binaries"]["x86_64"]["zzz"]["Provides"])
        # ...and aaa got its CONFLICT commit.
        subject = git(
            "log", "-1", "--format=%s", "main", "--", "packages/fedora/rawhide/aaa", cwd=self.monorepo
        )
        self.assertTrue(subject.startswith("CONFLICT: aaa"), subject)

    def test_rebuild_static(self) -> None:
        """rebuild of a static-Release package bumps the spec Release and the metadata (no X-Rebuild)."""
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)

        self.tool.rebuild("testpkg", "openssl-3.5.0-1")

        # Spec Release bumped by the local minor .1, and the metadata NEVRs + release field follow.
        self.assertIn("Release: 1.1%{?dist}", (self.monorepo / rel / "testpkg.spec").read_text())
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"testpkg = 1.0-1.1{NDIST}", meta["binaries"]["x86_64"]["testpkg"]["Provides"])
        self.assertEqual(meta["release"], "1.1")
        # Human-readable subject, and no X-Rebuild trailer (that's the %autorelease path).
        body = git("log", "-1", "--format=%s%n%b", "main", "--", rel, cwd=self.monorepo)
        self.assertIn("testpkg: Rebuild against openssl-3.5.0-1", body)
        self.assertNotIn("X-Rebuild", body)

    def test_rebuild_autorelease(self) -> None:
        """rebuild of an %autorelease package: an X-Rebuild commit touching only srcpkg.json."""
        rel = "packages/fedora/rawhide/arp"
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial import", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        c2 = self.commit("arp", "rawhide", "1.0", "2", "Rebuild", autorelease=True)
        self.build_koji("arp", "1.0", "2", c2, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c2)
        self.tool.import_("arp", None, None)

        self.tool.rebuild("arp", "openssl-3.5.0-1")

        # The bump lands in the metadata (autorelease 2 -> minor .1)...
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"arp = 1.0-2.1{NDIST}", meta["binaries"]["x86_64"]["arp"]["Provides"])
        # ...via an X-Rebuild commit that changes *only* the .json (no dir/spec touch).
        body = git("log", "-1", "--format=%s%n%b", "main", cwd=self.monorepo)
        self.assertIn("arp: Rebuild against openssl-3.5.0-1", body)
        self.assertIn("X-Rebuild: arp", body)
        changed = git("show", "--name-only", "--format=", "HEAD", cwd=self.monorepo).split()
        self.assertEqual(changed, [f"{rel}.json"])

    def test_rpm_metadata_preserves_sources_and_projection(self) -> None:
        """rpm-metadata keeps the lookaside `sources` and refreshes the projection on recompute."""
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        before = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertTrue(before["sources"])  # sources recorded at import time

        # Recompute from natively-built rpms (dist .fc99aos, as a real downstream build produces).
        spec_text = NOARCH_SPEC.format(pkg="testpkg", version="1.0", release="1")
        rpms = build_rpms(
            self._tmp / "build" / "testpkg-native",
            spec_text,
            "testpkg",
            "1.0",
            self._lookaside["testpkg-1.0.tar.gz"],
            {},
            dist=NDIST,
        )
        self.tool.rpm_metadata("testpkg", [str(r) for r in rpms], "fedora", "rawhide")

        after = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertEqual(after["sources"], before["sources"])  # sources preserved, not wiped
        self.assertEqual(
            (after["version"], after["release"], after["dist"]), ("1.0", "1", NDIST)
        )  # projection refreshed

    def test_update_discards_release_only_delta(self) -> None:
        """A local Release-only bump is discarded by an update (no conflict, no extra commit)."""
        rel = "packages/fedora/rawhide/bbb"
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)
        self.tool.import_("bbb", None, None)
        self.tool.rebuild("bbb", "openssl-3.5.0-1")  # local Release-only bump: 1 -> 1.1
        # Upstream ships its own rebuild: same version, Release 2.
        b2 = self.commit("bbb", "rawhide", "1.0", "2", "Rebuilt for something")
        self.build_koji("bbb", "1.0", "2", b2)
        self.tool.update_upstreams()

        self.assertTrue(self.tool.update("bbb"))  # clean: our bump discarded, upstream wins

        self.assertIn("Release: 2%{?dist}", (self.monorepo / rel / "bbb.spec").read_text())
        # The update lands as one commit (the upstream one) -- no separate revert, no CONFLICT.
        subjects = git("log", "--format=%s", "main", "--", rel, cwd=self.monorepo)
        self.assertNotIn("CONFLICT", subjects)
        self.assertNotIn("revert", subjects)
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"bbb = 1.0-2{NDIST}", meta["binaries"]["x86_64"]["bbb"]["Provides"])

    def test_srpm_autorelease(self) -> None:
        """srpm freezes %autorelease so an autorelease package builds a .src.rpm with its release."""
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        c2 = self.commit("arp", "rawhide", "1.0", "2", "Rebuild", autorelease=True)
        self.build_koji("arp", "1.0", "2", c2, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c2)
        self.tool.import_("arp", None, None)

        # Provide the source archive locally instead of hitting the real lookaside.
        def fake_fetch(pkgdir: Path, distro: str, pkg: str) -> None:
            (pkgdir / "arp-1.0.tar.gz").write_bytes(self._lookaside["arp-1.0.tar.gz"])

        with mock.patch.object(self.tool, "fetch_sources", side_effect=fake_fetch):
            srcrpm = self.tool.srpm("arp", None, None)

        self.assertTrue(srcrpm.exists())
        release = subprocess.run(
            ["rpm", "-qp", "--nosignature", "--qf", "%{RELEASE}", str(srcrpm)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        self.assertEqual(release, f"2{NDIST}")  # frozen autorelease (2) + our native dist

    def test_diff(self) -> None:
        """diff shows a package's local modifications vs the imported upstream (empty when clean)."""
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        self.assertEqual(self.capture(self.tool.diff_package, "testpkg", None, None), "")

        # A local spec modification shows up; the sibling .json (metadata) is outside the diff.
        self.modify(rel, "Summary: Test package", "Summary: Patched downstream")
        out = self.capture(self.tool.diff_package, "testpkg", None, None)
        self.assertIn("-Summary: Test package", out)
        self.assertIn("+Summary: Patched downstream", out)

    def test_diff_nonexistent(self) -> None:
        with self.assertRaises(SystemExit):
            self.tool.diff_package("ghost", None, None)

    def test_list(self) -> None:
        """list tabulates each package's version/release, modification status, and update state."""
        self.seed("cleanpkg")
        self.tool.import_("cleanpkg", None, None)
        self.seed("modpkg")
        self.tool.import_("modpkg", None, None)
        self.modify("packages/fedora/rawhide/modpkg", "Summary: Test package", "Summary: Modified")

        rows = {
            ln.split()[1]: ln
            for ln in self.capture(self.tool.list_packages).splitlines()
            if ln.startswith("fedora/")
        }
        self.assertIn("1.0", rows["cleanpkg"])
        self.assertIn("clean", rows["cleanpkg"])
        self.assertIn("up to date", rows["cleanpkg"])
        self.assertIn("modified", rows["modpkg"])

    def test_sync_takes_new_upstream_commits(self) -> None:
        """sync is `update` with our delta declared obsolete: it lands on the latest upstream, clean.

        The discard folds into the replayed upstream commit -- there is no separate reset commit, and
        the 1:1 X-Upstream-Commit correspondence holds.
        """
        rel = "packages/fedora/rawhide/bbb"
        b1 = self.commit("bbb", "rawhide", "1.0", "1", "Update to 1.0")
        self.build_koji("bbb", "1.0", "1", b1)
        self.tool.import_upstream("fedora", "rawhide", "bbb", b1)
        self.tool.import_("bbb", None, None)
        # A local delta that `update` would keep: an edit upstream doesn't touch, plus our own file.
        self.modify(
            rel,
            "Summary: Test package",
            "Summary: Patched downstream",
            addfile="downstream.patch",
            release=("1", "1.1"),
        )
        b2 = self.commit("bbb", "rawhide", "2.0", "1", "Update to 2.0")
        self.build_koji("bbb", "2.0", "1", b2)
        self.tool.update_upstreams()

        before = git("rev-parse", "HEAD", cwd=self.monorepo).strip()
        self.assertTrue(self.tool.sync("bbb", None, None))

        # One commit, and it is upstream's own: the discard rode along in it.
        self.assertEqual(git("rev-list", "--count", f"{before}..HEAD", cwd=self.monorepo).strip(), "1")
        self.assertEqual(self.imported_shas(rel, "main"), [b2, b1])
        # Landed on the new upstream version with nothing of ours left.
        spec = (self.monorepo / rel / "bbb.spec").read_text()
        self.assertIn("Version: 2.0", spec)
        self.assertIn("Summary: Test package", spec)
        self.assertFalse((self.monorepo / rel / "downstream.patch").exists())
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"bbb = 2.0-1{NDIST}", meta["binaries"]["x86_64"]["bbb"]["Provides"])
        self.assertEqual(self.capture(self.tool.diff_package, "bbb", None, None), "")
        self.assertEqual(self.tool.check(), [])

    def test_sync(self) -> None:
        """With no new upstream commits waiting, sync resets the package in a commit of its own."""
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        self.modify(rel, "Summary: Test package", "Summary: Custom downstream build")
        self.assertNotEqual(self.capture(self.tool.diff_package, "testpkg", None, None), "")

        before = git("rev-parse", "HEAD", cwd=self.monorepo).strip()
        self.tool.sync("testpkg", None, None)

        # A single commit resets it: diff empty, list clean, and the spec is back to upstream.
        self.assertEqual(git("rev-list", "--count", f"{before}..HEAD", cwd=self.monorepo).strip(), "1")
        self.assertEqual(self.capture(self.tool.diff_package, "testpkg", None, None), "")
        rows = {
            ln.split()[1]: ln
            for ln in self.capture(self.tool.list_packages).splitlines()
            if ln.startswith("fedora/")
        }
        self.assertIn("clean", rows["testpkg"])
        self.assertIn("Summary: Test package", (self.monorepo / rel / "testpkg.spec").read_text())

        # Syncing an already-clean package fails: there's nothing to reset.
        with self.assertRaises(SystemExit):
            self.tool.sync("testpkg", None, None)

    def test_sync_drops_locally_added_files(self) -> None:
        """sync removes a file our modification *added*, not only the ones upstream also has.

        Restoring the import's paths one by one leaves an added downstream patch behind, and that
        residue keeps the package "modified" for list/diff even though the spec is pristine again.
        """
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        self.modify(
            rel,
            "Summary: Test package",
            "Summary: Patched downstream",
            addfile="downstream.patch",
            release=("1", "1.1"),
        )

        self.tool.sync("testpkg", None, None)

        self.assertFalse((self.monorepo / rel / "downstream.patch").exists())
        self.assertEqual(self.capture(self.tool.diff_package, "testpkg", None, None), "")
        self.assertEqual(self.tool.check(), [])

    def test_sync_restarts_the_local_bump(self) -> None:
        """Discarding our delta puts the release back to the import's, and `.1` becomes free again.

        The reset commit is a local commit, so counting it would make check demand a `.1` release
        from a package that is pristine again -- and push the *next* real modification to `.2`.
        """
        rel = "packages/fedora/rawhide/arp"
        sha = self.commit("arp", "rawhide", "1.0", "1", "Initial import", autorelease=True)
        self.build_koji("arp", "1.0", "1", sha, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", sha)
        self.tool.import_("arp", None, None)
        self.modify(rel, "Summary: Test package", "Summary: Patched", release=("1", "1.1"))
        self.assertEqual(self.tool.local_bump(rel), 1)

        self.tool.sync("arp", None, None)

        self.assertEqual(self.tool.local_bump(rel), 0)
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertEqual(meta["release"], "1")
        self.assertEqual(self.tool.check(), [])

        # The next modification is `.1` again, not `.2`.
        self.tool.rebuild("arp", "openssl-3.5.0-1")
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertEqual(meta["release"], "1.1")
        self.assertEqual(self.tool.check(), [])

    def test_sync_discards_a_rebuild_only_delta(self) -> None:
        """A reset whose whole delta was an X-Rebuild bump touches only metadata, and that is fine.

        It is the one local metadata-only commit that legitimately carries no X-Rebuild: trailer of
        its own -- it takes one away.
        """
        rel = "packages/fedora/rawhide/arp"
        sha = self.commit("arp", "rawhide", "1.0", "1", "Initial import", autorelease=True)
        self.build_koji("arp", "1.0", "1", sha, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", sha)
        self.tool.import_("arp", None, None)
        self.tool.rebuild("arp", "openssl-3.5.0-1")

        self.tool.sync("arp", None, None)

        changed = git("show", "--name-only", "--format=", "HEAD", cwd=self.monorepo).split()
        self.assertEqual(changed, [f"{rel}.json"])
        self.assertEqual(json.loads((self.monorepo / f"{rel}.json").read_text())["release"], "1")
        self.assertEqual(self.tool.check(), [])

    def test_rpm_metadata_recompute(self) -> None:
        """rpm-metadata recomputes srcpkg.json from the actual built rpms (the post-build check).

        This captures local changes the import-time metadata copy can't predict -- here a new
        Provides added by a downstream patch, which only exists once the package is really built.
        """
        rel = "packages/fedora/rawhide/testpkg"
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        imported = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertNotIn("something-new = 1.0", imported["binaries"]["x86_64"]["testpkg"]["Provides"])

        # Local modification whose effect is only visible in a real build: a brand-new Provides.
        self.modify(rel, "Requires: bash", "Requires: bash\nProvides: something-new = 1.0")
        spec_text = (self.monorepo / rel / "testpkg.spec").read_text()
        rpms = build_rpms(
            self._tmp / "build" / "testpkg-mod",
            spec_text,
            "testpkg",
            "1.0",
            self._lookaside["testpkg-1.0.tar.gz"],
            {},
        )

        self.tool.rpm_metadata("testpkg", [str(r) for r in rpms], "fedora", "rawhide")

        recomputed = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn("something-new = 1.0", recomputed["binaries"]["x86_64"]["testpkg"]["Provides"])


class NativePackages(PackagesTestCase):
    """Downstream-only packages: no upstream dist-git and no source archives.

    Created straight on main following the documented flow: write the spec, build locally,
    run rpm-metadata over the built rpms, and commit spec + json together.
    """

    def make_native(self, pkg: str = "mine") -> str:
        """Create, build, and commit a downstream-only package on main; return its packages/ path.

        The built rpms (bare native dist tag, as mockbuild produces) stay in self.native_rpms so a test can
        re-run rpm-metadata.
        """
        rel = f"packages/myos/latest/{pkg}"
        pkgdir = self.monorepo / rel
        pkgdir.mkdir(parents=True)
        spec_text = AUTORELEASE_SPEC.format(pkg=pkg, version="1.0")
        (pkgdir / f"{pkg}.spec").write_text(spec_text)
        # %autorelease isn't defined without rpmautospec; supply the resolved value for the build.
        # A native package has no upstream release to continue, so its %autorelease bases at 0 and
        # the sole creation commit makes it 0.1 (the tool's base-0 rule, see check_commit).
        defines = {"autorelease": "0.1%{?dist}", "autochangelog": "%nil"}
        self.native_rpms: list[Path] = build_rpms(
            self._tmp / "build" / f"{pkg}-native",
            spec_text,
            pkg,
            "1.0",
            make_tarball(pkg, "1.0"),
            defines,
            dist="." + NATIVE_DIST,
        )
        self.tool.rpm_metadata(pkg, [str(r) for r in self.native_rpms], None, None)
        git("add", "packages", cwd=self.monorepo)
        git("commit", "--quiet", "-m", f"{pkg}: new native package", cwd=self.monorepo)
        return rel

    def test_rpm_metadata(self) -> None:
        """rpm-metadata computes a native package's json from the local build.

        There is no dist-git `sources` file to carry over and no upstream dist tag: the built
        release ends in the bare native dist, which must not be mistaken for a Fedora/CentOS one.
        """
        rel = self.make_native()
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertEqual((meta["version"], meta["release"], meta["dist"]), ("1.0", "0.1", "." + NATIVE_DIST))
        self.assertEqual(meta["sources"], [])
        self.assertEqual(meta["build_requires"], {"_all": ["sed"]})
        for arch in ("x86_64", "aarch64"):
            self.assertIn(f"mine = 1.0-0.1.{NATIVE_DIST}", meta["binaries"][arch]["mine"]["Provides"])

    def test_rpm_metadata_json_without_sources(self) -> None:
        """A recompute copes with an existing json that lacks the `sources` key.

        A native package's first json can be written by hand (bootstrapping the build); nothing
        guarantees it carries the `sources` list rpm-metadata otherwise preserves.
        """
        rel = self.make_native()
        metafile = self.monorepo / f"{rel}.json"
        meta = json.loads(metafile.read_text())
        del meta["sources"]
        metafile.write_text(json.dumps(meta))

        self.tool.rpm_metadata("mine", [str(r) for r in self.native_rpms], None, None)

        self.assertEqual(json.loads(metafile.read_text())["sources"], [])

    def rpm_metadata_with_options(self, spec_template: str, options: list[str]) -> None:
        """Build with `options`, recompute via the branch's rpmbuild_options, verify the json.

        The build is invoked with those options (rpm_package's `rpmbuild_options` attr), so the
        recompute must apply them too: without, the gated noarch subpackage would be dropped as
        "no build arch produces it" and its BuildRequires would go unrecorded.
        """
        pkg = "mine"
        rel = f"packages/myos/latest/{pkg}"
        pkgdir = self.monorepo / rel
        pkgdir.mkdir(parents=True)
        spec_text = spec_template.format(pkg=pkg, version="1.0")
        (pkgdir / f"{pkg}.spec").write_text(spec_text)
        (pkgdir.parent / "_properties.json").write_text(json.dumps({"rpmbuild_options": {pkg: options}}))
        defines = {"autorelease": "0.1%{?dist}", "autochangelog": "%nil"}
        rpms = build_rpms(
            self._tmp / "build" / f"{pkg}-options",
            spec_text,
            pkg,
            "1.0",
            make_tarball(pkg, "1.0"),
            defines,
            dist="." + NATIVE_DIST,
            options=options,
        )

        self.tool.rpm_metadata(pkg, [str(r) for r in rpms], None, None)

        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertEqual(meta["build_requires"], {"_all": ["gawk", "sed"]})
        for arch in ("x86_64", "aarch64"):  # interchangeable noarch: recorded for every build arch
            self.assertIn(f"{pkg}-extra", meta["binaries"][arch])

    def test_rpm_metadata_with_option(self) -> None:
        """rpm-metadata applies the branch's rpmbuild_options: the bcond --with form."""
        self.rpm_metadata_with_options(BCOND_GATED_SPEC, ["--with=extra"])

    def test_rpm_metadata_define_option(self) -> None:
        """The --define form: one token carrying both '=' and a space survives the round trip."""
        self.rpm_metadata_with_options(MACRO_GATED_SPEC, ["--define=build_extra 1"])

    def test_list(self) -> None:
        """list shows a native package with its spec's version/release and no upstream state."""
        self.make_native()
        rows = {
            ln.split()[1]: ln
            for ln in self.capture(self.tool.list_packages).splitlines()
            if ln.startswith("myos/")
        }
        self.assertIn("1.0", rows["mine"])
        self.assertIn("native", rows["mine"])
        self.assertNotIn("update", rows["mine"])

    def test_srpm(self) -> None:
        """srpm assembles a native package with the bare native dist and frozen %autorelease.

        A native package has no upstream lookaside; its Source0 is delivered locally (a future
        downstream lookaside would do this -- see fetch_sources, a no-op without a `sources` file),
        so we drop the tarball beside the spec before assembling.
        """
        rel = self.make_native()
        (self.monorepo / rel / "mine-1.0.tar.gz").write_bytes(make_tarball("mine", "1.0"))
        srcrpm = self.tool.srpm("mine", None, None)
        self.assertTrue(srcrpm.exists())
        release = subprocess.run(
            ["rpm", "-qp", "--nosignature", "--qf", "%{RELEASE}", str(srcrpm)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        self.assertEqual(release, f"0.1.{NATIVE_DIST}")

    def test_check(self) -> None:
        """check accepts the creation commit: spec + json together, %autorelease resolving to 0.1."""
        self.make_native()
        self.assertEqual(self.tool.check(), [])


class Check(PackagesTestCase):
    """`check`: validate the release/metadata conventions on the branch's commits.

    One test (shared setUp is the expensive part): build a valid history, assert it passes, then
    introduce a commit violating each rule in turn and assert check() reports it, resetting between.
    """

    def test_autorelease_release_resets_across_version_bump(self) -> None:
        """A version bump's release reset is accepted by check (the version moved with it)."""
        rel = "packages/fedora/rawhide/arp"  # arp = autorelease package
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial 1.0", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c1)
        self.tool.import_("arp", None, None)

        # A new upstream Version restarts upstream's release counter at 1.
        c2 = self.commit("arp", "rawhide", "2.0", "1", "Update to 2.0", autorelease=True)
        self.build_koji("arp", "2.0", "1", c2, autorelease=True)
        self.tool.update_upstreams()
        self.tool.update("arp")

        # The imported metadata carries the reset release (2.0-1); check accepts the
        # non-advancing release because the version changed with it.
        meta = json.loads((self.monorepo / f"{rel}.json").read_text())
        self.assertIn(f"arp = 2.0-1{NDIST}", meta["binaries"]["x86_64"]["arp"]["Provides"])
        self.assertEqual(self.tool.check(), [])

    def test_unbuilt_upstream_commits_import_without_metadata(self) -> None:
        """An upstream commit without its own koji build imports json-less, and check accepts that.

        Upstream routinely batches: push a version bump without building, then build only together
        with a follow-up fix -- one build whose %autorelease counts both commits (systemd 261.1-2).
        The bump's import then legitimately carries no srcpkg.json change; only local commits
        require one.

        The replayed json also carries the per-commit source_date_epoch (the upstream-rpm
        commit's author date, 1s granularity), so pin the author date -- otherwise the bump's
        main-side json change depends on whether the test crosses a second boundary.
        """
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000"}):
            c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial 1.0", autorelease=True)
            self.build_koji("arp", "1.0", "1", c1, autorelease=True)
            self.tool.import_upstream("fedora", "rawhide", "arp", c1)
            self.tool.import_("arp", None, None)
            self.commit("arp", "rawhide", "2.0", "1", "Version 2.0", autorelease=True)
            c3 = self.commit("arp", "rawhide", "2.0", "2", "Fix after bump", autorelease=True)
            self.build_koji("arp", "2.0", "2", c3, autorelease=True)
            self.tool.update_upstreams()
            self.tool.update("arp")

        bump_import = git("log", "main", "--format=%H", "--grep=Version 2.0", cwd=self.monorepo)
        files = git(
            "diff-tree", "--no-commit-id", "-r", "--name-only", bump_import, cwd=self.monorepo
        ).split()
        self.assertIn("packages/fedora/rawhide/arp/arp.spec", files)
        self.assertNotIn("packages/fedora/rawhide/arp.json", files)  # no build -> no metadata
        self.assertEqual(self.tool.check(), [])

    def test_empty_upstream_rebuild_imports_metadata_only(self) -> None:
        """A *built* empty upstream mass-rebuild commit imports as metadata-only.

        rpmautospec mass rebuilds are empty dist-git commits ("Rebuilt for ..."); one that was
        built has its build's recomputed json to show, and that is all. check accepts it: the
        metadata-only-needs-X-Rebuild rule is for local commits, and the release advances.
        """
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial 1.0", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c1)
        self.tool.import_("arp", None, None)
        git(
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Rebuilt for Mass Rebuild",
            cwd=self.distgit / "arp.git",
        )
        c2 = git("rev-parse", "HEAD", cwd=self.distgit / "arp.git")
        self.build_koji("arp", "1.0", "2", c2, autorelease=True)
        self.tool.update_upstreams()
        self.tool.update("arp")

        rebuilt = git("log", "main", "--format=%H", "--grep=Rebuilt for", cwd=self.monorepo)
        files = git("diff-tree", "--no-commit-id", "-r", "--name-only", rebuilt, cwd=self.monorepo).split()
        self.assertEqual(files, ["packages/fedora/rawhide/arp.json"])
        self.assertEqual(self.tool.check(), [])

    def curate(self, pkg: str, options: list[str]) -> None:
        """Commit an rpmbuild_options change for `pkg` plus the metadata refresh it causes.

        This is the entire fallout of a curation edit: the hand-authored _properties.json and the
        package's generated <pkg>.json. The package's own spec/sources stay untouched, and the
        branch BUCK loads _properties.json at parse time instead of embedding it.
        """
        branch = self.monorepo / "packages/fedora/rawhide"
        (branch / "_properties.json").write_text(json.dumps({"rpmbuild_options": {pkg: options}}))
        # stand-in for the rpm-metadata recompute: only the file set matters here
        (branch / f"{pkg}.json").write_text((branch / f"{pkg}.json").read_text() + "\n")
        git("add", "packages", cwd=self.monorepo)  # not -A: the .upstream-rpm worktree lives here
        git("commit", "--quiet", "-m", f"{pkg}: disable docs", cwd=self.monorepo)

    def test_config_refresh_without_dir_change_is_accepted(self) -> None:
        """A metadata-only refresh from a branch config change needs no Release bump.

        Disabling docs via a branch rpmbuild_options rebuilds the same release with a different
        binary set: the generated <pkg>.json changes, but the package's own spec/sources don't --
        so a static-Release package needs neither a bump nor %autorelease.
        """
        self.seed("testpkg")  # static-Release package
        self.tool.import_("testpkg", None, None)
        good = git("rev-parse", "HEAD", cwd=self.monorepo)

        self.curate("testpkg", ["--without=docs"])

        self.assertEqual(self.tool.check(good), [])

    def test_config_refresh_keeps_the_autorelease_unsuffixed(self) -> None:
        """A curation refresh of an %autorelease package keeps the import's bare release.

        It counts as no local commit, so there is no `.N` minor bump to append -- the release
        stays the imported `1`, not `1.0` (a bump starts at `.1`).
        """
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial 1.0", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c1)
        self.tool.import_("arp", None, None)
        good = git("rev-parse", "HEAD", cwd=self.monorepo)

        self.curate("arp", ["--without=docs"])

        meta = json.loads((self.monorepo / "packages/fedora/rawhide/arp.json").read_text())
        self.assertIn(f"arp = 1.0-1{NDIST}", meta["binaries"]["x86_64"]["arp"]["Provides"])
        self.assertEqual(self.tool.check(good), [])

    def test_removing_a_package_is_accepted(self) -> None:
        """Unimporting a package -- removing its dir and json -- passes check.

        A removal leaves no spec or metadata behind, so the release/rebuild conventions (which
        would otherwise fire on the vanished Release: line and %changelog) don't apply.
        """
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        good = git("rev-parse", "HEAD", cwd=self.monorepo)

        branch = self.monorepo / "packages/fedora/rawhide"
        shutil.rmtree(branch / "testpkg")
        (branch / "testpkg.json").unlink()
        git("add", "packages", cwd=self.monorepo)  # not -A: the .upstream-rpm worktree lives here
        git("commit", "--quiet", "-m", "unimport testpkg", cwd=self.monorepo)

        self.assertEqual(self.tool.check(good), [])

    def test_check(self) -> None:
        """check() passes a clean history and flags each convention violation."""
        # Valid history: a static package (import + rebuild) and an %autorelease one (+ rebuild).
        self.seed("testpkg")
        self.tool.import_("testpkg", None, None)
        self.tool.rebuild("testpkg", "openssl-3.5.0-1")
        c1 = self.commit("arp", "rawhide", "1.0", "1", "Initial", autorelease=True)
        self.build_koji("arp", "1.0", "1", c1, autorelease=True)
        c2 = self.commit("arp", "rawhide", "1.0", "2", "Rebuild", autorelease=True)
        self.build_koji("arp", "1.0", "2", c2, autorelease=True)
        self.tool.import_upstream("fedora", "rawhide", "arp", c2)
        self.tool.import_("arp", None, None)
        self.tool.rebuild("arp", "openssl-3.5.0-1")
        self.assertEqual(self.tool.check(), [])  # a clean history passes

        good = git("rev-parse", "HEAD", cwd=self.monorepo)
        testpkg = self.monorepo / "packages/fedora/rawhide/testpkg"
        testpkg_json = self.monorepo / "packages/fedora/rawhide/testpkg.json"
        testpkg_spec = testpkg / "testpkg.spec"
        arp = self.monorepo / "packages/fedora/rawhide/arp"  # arp = autorelease package
        arp_json = self.monorepo / "packages/fedora/rawhide/arp.json"

        def commit(msg: str) -> None:
            git("add", "packages", cwd=self.monorepo)  # not -A: the .upstream-rpm worktree lives here
            git("commit", "--quiet", "-m", msg, cwd=self.monorepo)

        def violates(substr: str) -> None:
            """Assert check() (since the good baseline) reports `substr`, then reset to baseline."""
            self.assertIn(substr, " | ".join(self.tool.check(good)))
            git("reset", "--quiet", "--hard", good, cwd=self.monorepo)

        def bump() -> None:  # a legitimate Release bump, to isolate the check under test
            testpkg_spec.write_text(
                testpkg_spec.read_text().replace("Release: 1.1%{?dist}", "Release: 1.2%{?dist}")
            )

        # C1: a *local* commit changed the package but srcpkg.json wasn't updated (import commits
        # are exempt, see test_unbuilt_upstream_commits_import_without_metadata).
        bump()
        commit("testpkg: forgot json")
        violates("but not testpkg.json")

        # C2: srcpkg.json is not valid JSON.
        bump()
        testpkg_json.write_text("{ not json")
        commit("testpkg: bad json")
        violates("not valid JSON")

        # C3: X-Upstream-Commit that doesn't resolve on upstream-rpm.
        bump()
        commit("testpkg: fake import\n\nX-Upstream-Commit: " + "0" * 40)
        violates("no match on upstream-rpm")

        # C4: a local change that neither uses %autorelease nor bumps Release:.
        testpkg_spec.write_text(
            testpkg_spec.read_text().replace("Summary: Test package", "Summary: Tweaked")
        )
        commit("testpkg: no release bump")
        violates("must bump Release: from 1.1%{?dist} to 1.2%{?dist}, not 1.1%{?dist}")

        # C4b: Release: bumped, but into upstream's integer namespace instead of our minor `.N`.
        testpkg_spec.write_text(
            testpkg_spec.read_text().replace("Release: 1.1%{?dist}", "Release: 2%{?dist}")
        )
        commit("testpkg: upstream-style release bump")
        violates("must bump Release: from 1.1%{?dist} to 1.2%{?dist}, not 2%{?dist}")

        # C5: a metadata-only change on a non-%autorelease package.
        testpkg_json.write_text(testpkg_json.read_text() + "\n")
        commit("testpkg: metadata only")
        violates("changes only metadata but is not an %autorelease")

        # C6: an X-Rebuild that changes more than the srcpkg.json (on the autorelease package).
        arp_json.write_text(arp_json.read_text() + "\n")
        (arp / "arp.spec").write_text((arp / "arp.spec").read_text() + "\n# tweak\n")
        commit("arp: rebuild\n\nX-Rebuild: arp")
        violates("changes more than arp.json")

        # C7: a local commit touches %changelog.
        bump()
        testpkg_spec.write_text(testpkg_spec.read_text() + "\n%changelog\n* Mon Jan 01 2026 Me - note\n")
        commit("testpkg: changelog")
        violates("modifies %changelog")

        # C8: the %autorelease number in Provides disagrees with git history.
        arp_json.write_text(
            arp_json.read_text().replace(f"2.1{NDIST}", f"9.9{NDIST}").replace('"2.1"', '"9.9"')
        )
        commit("arp: wrong release\n\nX-Rebuild: arp")
        violates("git history implies")

        # C9: arch buckets recording different version-releases (a skewed per-arch refresh).
        meta = json.loads(arp_json.read_text())
        binmeta = meta["binaries"]["aarch64"]["arp"]
        binmeta["Provides"] = [p.replace(f"2.1{NDIST}", f"2.2{NDIST}") for p in binmeta["Provides"]]
        arp_json.write_text(json.dumps(meta))
        commit("arp: skewed arches\n\nX-Rebuild: arp")
        violates(f"arp version-release skew: aarch64 has 1.0-2.2{NDIST}, x86_64 has 1.0-2.1{NDIST}")

        # C10: an imported release that moves backwards without a version change.
        arp_json.write_text(arp_json.read_text().replace(f"2.1{NDIST}", f"5{NDIST}").replace('"2.1"', '"5"'))
        commit("arp: import release 5\n\nX-Upstream-Commit: " + "1" * 40)
        arp_json.write_text(arp_json.read_text().replace(f"5{NDIST}", f"3{NDIST}").replace('"5"', '"3"'))
        commit("arp: import release 3\n\nX-Upstream-Commit: " + "2" * 40)
        violates("imported release '3' does not advance on the previous '5'")


class RegenerateBuck(unittest.TestCase):
    """The generated per-branch BUCK reflects the .json package set and loads the branch curation.

    regenerate_buck() emits a load + PACKAGES entry per <pkg>.json, and has the generated file read
    _properties.json itself rather than copying its values in -- so a curation edit takes effect
    without regenerating. _properties.json is never treated as a package. Pure text generation --
    no rpm build pipeline needed.
    """

    @override
    def setUp(self) -> None:
        self.tool = tool
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.branchdir = Path(tmp) / "packages" / "fedora" / "rawhide"
        self.branchdir.mkdir(parents=True)
        for pkg in ("gcc", "glibc"):  # contents are irrelevant; only names glob
            (self.branchdir / f"{pkg}.json").write_text("{}")

    def buck(self) -> str:
        self.tool.regenerate_buck(self.branchdir)
        return (self.branchdir / "BUCK").read_text()

    CURATION = {
        "buildroot": "//buildroots/myos:base",
        "buildroot_only_packages": ["glibc32"],
        "in_place_packaging": {"gcc": "packaging/pins.json"},
        "in_place_rpmbuild_options": {"glibc": ["--with=upstream"]},
        "in_place_specs": {"glibc": "packaging/fedora/glibc.spec"},
        "seed_only_packages": ["gcc"],
        "rpmbuild_options": {"gcc": ["--with=basic"]},
    }

    def curate(self) -> None:
        (self.branchdir / "_properties.json").write_text(json.dumps(self.CURATION))

    def test_package_set(self) -> None:
        buck = self.buck()
        # real packages are loaded and listed...
        self.assertIn('load(":gcc.json", _gcc = "value")', buck)
        self.assertIn('    "glibc": _glibc,', buck)
        # The importer takes the distro from the name of the branch's parent directory.
        self.assertIn('    distro = "fedora",', buck)
        # ...but the curation file is never a PACKAGES entry.
        self.curate()
        self.assertNotIn('"_properties": _properties,', self.buck())

    def test_no_properties(self) -> None:
        # Without curation the load has nothing to bind, so the branch supplies an empty dict and
        # every property falls back to its default.
        buck = self.buck()
        self.assertNotIn('load(":_properties.json"', buck)
        self.assertIn("_properties = {}\n", buck)

    def test_curation_is_loaded_not_embedded(self) -> None:
        # The whole point: buck2 reads _properties.json itself, so editing it takes effect without
        # regenerating. Every curated value must therefore be absent from the generated file.
        self.curate()
        buck = self.buck()
        self.assertIn('load(":_properties.json", _properties = "value")', buck)
        for key in self.CURATION:
            self.assertIn(f'_properties.get("{key}"', buck)
        for value in (
            "//buildroots/myos:base",
            "glibc32",
            "packaging/pins.json",
            "--with=upstream",
            "--with=basic",
        ):
            self.assertNotIn(value, buck)

    def test_formatter_stable(self) -> None:
        # starlark_fmt keeps the cell load first, sorts the local ones by file name, and sorts dict
        # keys, so the generated file must already be in that order to pass a formatter check. A
        # name extending another sorts differently as a file name ('-' < '.') and as a key.
        (self.branchdir / "glibc-common.json").write_text("{}")
        self.curate()
        lines = self.buck().splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith("load(")],
            [
                'load("@tine//package_system/rpm:defs.bzl", "rpm")',
                'load(":_properties.json", _properties = "value")',
                'load(":gcc.json", _gcc = "value")',
                'load(":glibc-common.json", _glibc_common = "value")',
                'load(":glibc.json", _glibc = "value")',
            ],
        )
        packages = lines.index("PACKAGES = {")
        self.assertEqual(
            lines[packages + 1 : packages + 4],
            ['    "gcc": _gcc,', '    "glibc": _glibc,', '    "glibc-common": _glibc_common,'],
        )

    def test_default_buildroot(self) -> None:
        # Absent an override, the buildroot label is derived from the branch's <distro>/<branch>
        # path -- the one default that must be generated, since buck cannot know the branch's
        # distro. It stays the fallback even when the branch overrides it.
        default = '_properties.get("buildroot", "//buildroots/fedora:rawhide")'
        self.assertIn(default, self.buck())
        self.curate()
        self.assertIn(default, self.buck())


class RepoRoot(unittest.TestCase):
    """repo_root() finds the OS.git as Buck's project root, from wherever the tool file sits.

    The layout mirrors a consuming project: the tine cell is a submodule with a .buckconfig and
    .git of its own, and `buck run` executes a copy of the tool under buck-out.
    """

    @override
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.project = Path(tmp) / "os"
        self.cell = self.project / "tine"
        (self.cell / "tools").mkdir(parents=True)
        (self.project / ".git").mkdir()
        (self.project / ".buckconfig").write_text("[cells]\n  root = .\n  tine = tine\n")
        (self.cell / ".git").write_text("gitdir: ../.git/modules/tine\n")
        (self.cell / ".buckconfig").write_text(
            "[cells]\n# the cell's real name\ntine = .\nprelude = prelude\n"
        )

    def test_from_the_cell(self) -> None:
        # No packages/ yet: a new OS.git before its first import.
        self.assertEqual(tool.repo_root(self.cell / "tools" / "importer.py"), self.project)

    def test_from_buck_out(self) -> None:
        start = self.project / "buck-out" / "v2" / "art" / "tine" / "0123" / "tools" / "importer.py"
        self.assertEqual(tool.repo_root(start), self.project)

    def test_standalone_tine(self) -> None:
        # A tine checkout is its own project; the importer must not treat it as an OS.git.
        shutil.rmtree(self.project / ".git")
        (self.project / ".buckconfig").unlink()
        (self.cell / ".git").unlink()
        (self.cell / ".git").mkdir()
        self.assertIsNone(tool.repo_root(self.cell / "tools" / "importer.py"))

    def test_project_without_git(self) -> None:
        shutil.rmtree(self.project / ".git")
        self.assertIsNone(tool.repo_root(self.cell / "tools" / "importer.py"))


class TestCLI(unittest.TestCase):
    """Out-of-process smoke test: the real CLI entry point (argparse + dispatch)."""

    def test_help(self) -> None:
        out = subprocess.run(
            [sys.executable, str(TOOL_PATH), "--help"], check=True, text=True, stdout=subprocess.PIPE
        ).stdout
        for verb in ("import-upstream", "update-upstreams", "import", "diff", "list"):
            self.assertIn(verb, out)


if __name__ == "__main__":
    unittest.main()
