#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Resolve package transactions or prebuild libdnf5 repository caches.

Solves against pinned metadata and optionally an existing lower stack. Remote
packages are identified by repository and content checksum; local packages also
record their input location. `make-cache` amortizes metadata parsing across solve actions.
"""

import argparse
import sys
from contextlib import ExitStack
from pathlib import Path

import libdnf5
import libdnf5.comps
import libdnf5.conf
import libdnf5.rpm
import specs
from util import fail

import macros
import storage
import transaction

# A multilib package in a pinned-arch transaction indicates a bad solve.
MULTILIB_ARCHES = ("i686", "i386", "i586")

CACHEDIR = Path("/var/tmp/plan-cache")


class SolveSpec(transaction.Spec):
    install: list[str]
    install_files: list[str]
    lower: list[str]
    # Prebuilt repository caches (make-cache outputs) seeding this solve.
    cache: list[str]


def load_repositories(spec: transaction.Spec) -> list[transaction.Repository]:
    """Read the configured repositories a solve or cache build runs against."""
    # libdnf5 needs absolute paths.
    return transaction.load_repositories(spec, absolute=True)


def load_base(
    repos: list[transaction.Repository],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path] | None = None,
) -> libdnf5.base.Base:
    """Load pinned repositories, optionally seeding libdnf5's parsed metadata cache."""
    base = libdnf5.base.Base()
    cfg = base.get_config()
    cfg.cachedir = str(cachedir)
    cfg.install_weak_deps = False
    if installroot is not None:
        cfg.installroot = str(installroot)
    if seeds:
        # libdnf5 only copies out of system_cachedir, so Buck outputs can stay read-only.
        seed_root = cachedir.parent / (cachedir.name + "-seed")
        seed_root.mkdir(parents=True, exist_ok=True)
        for seed in seeds:
            for sub in sorted(seed.iterdir()):
                (seed_root / sub.name).symlink_to(sub)
        cfg.system_cachedir = str(seed_root)
    # Buck pins freshness through action inputs, not wall-clock age.
    cfg.metadata_expire = -1
    # Filelists resolves path dependencies; comps expands `@group` install specs.
    cfg.get_optional_metadata_types_option().set(
        f"{libdnf5.conf.METADATA_TYPE_FILELISTS},{libdnf5.conf.METADATA_TYPE_COMPS}"
    )
    # Do not let host detection affect provider selection.
    base.get_vars().set("arch", arch)
    base.get_vars().set("basearch", arch)
    # Each distribution's dnf5 package enables its own set of libdnf5 plugins in the box. Arch's
    # enables one whose library it does not install, and setup() then fails.
    cfg.plugins = False
    base.setup()
    # libdnf5 reads the rpm database of `installroot` through the box's rpm.
    macros.define()

    sack = base.get_repo_sack()
    for repo in repos:
        rc = sack.create_repo(repo.id).get_config()
        rc.baseurl = f"file://{repo.path}"  # pinned repodata read locally
        rc.get_pkg_gpgcheck_option().set(False)
        # Lower priorities win even when another repository has a newer NEVRA.
        rc.get_priority_option().set(repo.priority)
    if installroot is not None:
        # Load the rpmdb so installed packages satisfy dependencies.
        sack.load_repos()
    else:
        sack.load_repos(libdnf5.repo.Repo.Type_AVAILABLE)
    return base


def undelivered(
    base: libdnf5.base.Base,
    tx: libdnf5.base.Transaction,
    install: list[str],
    settings: libdnf5.base.GoalJobSettings,
    installed: bool,
) -> list[str]:
    """Report the install specs a resolved transaction does not actually deliver.

    dnf's solver may satisfy an install request with something other than the named package, for example
    an obsoleting split-out subpackage. When that happens, dnf reports no error, and a whole package
    silently vanishes from the image.

    Validate the generated transaction against the original goal, and report what's missing.

    Use libdnf's matcher, because a spec is not always a package name: it can name a group, a capability,
    a file or a version, and only `resolve_pkg_spec` reads all of those the way `add_install` did.
    """
    carried = libdnf5.rpm.PackageSet(base)
    for tp in tx.get_transaction_packages():
        if libdnf5.transaction.transaction_item_action_is_inbound(tp.get_action()):
            carried.add(tp.get_package())
    if installed:
        # A spec the lower stack already carries is delivered without the transaction adding it.
        lower = libdnf5.rpm.PackageQuery(base)
        lower.filter_installed()
        carried.update(lower)

    groups = {group.get_group().get_groupid() for group in tx.get_transaction_groups()}
    missing = []
    for spec in install:
        if spec.startswith("@"):
            if spec.removeprefix("@") not in groups:
                missing.append(spec)
            continue
        # resolve_pkg_spec() doesn't evaluate rich dependencies. For (A if B), it would report A as missing
        # while B is absent, although the dependency holds. The solve still requests every rich dependency,
        # and it fails when no package satisfies one, such as (A or B) with neither A nor B available.
        if spec.startswith("("):
            continue
        wanted = libdnf5.rpm.PackageQuery(base)
        wanted.resolve_pkg_spec(spec, settings, False)
        delivered = libdnf5.rpm.PackageQuery(wanted)
        delivered.intersection(carried)
        if not delivered.empty():
            continue
        # Naming what took its place is the useful diagnosis: an obsoleting split-out subpackage
        # looks like a successful solve from every other angle.
        obsoleters = libdnf5.rpm.PackageQuery(base)
        obsoleters.filter_obsoletes(wanted)
        obsoleters.intersection(carried)
        blame = ", ".join(sorted(pkg.get_nevra() for pkg in obsoleters.to_sorted_vector()))
        missing.append(f"{spec}, obsoleted by {blame}" if blame else spec)
    return missing


def plan(
    repos: list[transaction.Repository],
    install: list[str],
    cachedir: Path,
    installroot: Path | None,
    arch: str,
    seeds: list[Path],
) -> list[transaction.TransactionPackage]:
    base = load_base(repos, cachedir, installroot, arch, seeds)
    repositories = {repo.id: repo for repo in repos}

    goal = libdnf5.base.Goal(base)
    # add_install supports groups; use only their mandatory members.
    settings = libdnf5.base.GoalJobSettings()
    settings.set_group_package_types(libdnf5.comps.PackageType_MANDATORY)
    for spec in install:
        goal.add_install(spec, settings)
    tx = goal.resolve()

    # An installed lower legitimately satisfies requested packages without adding them.
    problems = [
        log.to_string()
        for log in tx.get_resolve_logs()
        if log.get_problem() != libdnf5.base.GoalProblem_ALREADY_INSTALLED
    ]
    if problems:
        fail("plan resolution failed:\n  " + "\n  ".join(problems))

    if missing := undelivered(base, tx, install, settings, installroot is not None):
        fail("plan: the transaction does not install:\n  " + "\n  ".join(missing))

    # Only inbound transaction items need downloading.
    resolved: list[transaction.TransactionPackage] = []
    for tp in tx.get_transaction_packages():
        if not libdnf5.transaction.transaction_item_action_is_inbound(tp.get_action()):
            continue
        pkg = tp.get_package()
        if pkg.get_arch() in MULTILIB_ARCHES:
            fail(f"refusing multilib package {pkg.get_nevra()} (32-bit in a {arch} closure)")
        chk = pkg.get_checksum()
        if chk.get_type_str() != "sha256":
            fail(f"expected sha256 repodata checksum for {pkg.get_nevra()}")
        repo = repositories[pkg.get_repo_id()]
        resolved.append(
            transaction.entry(
                pkg.get_nevra(),
                repo,
                chk.get_checksum(),
                pkg.get_location(),
                pkg.get_download_size(),
            )
        )
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="plan")
    sub = p.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="resolve the closure, writing the transaction")
    specs.add_argument(solve)
    solve.add_argument(
        "--out",
        required=True,
        help="output transaction JSON, or `-` for stdout (remote adds url/size; local adds location)",
    )

    cache = sub.add_parser("make-cache", help="just load the repos (no solve)")
    specs.add_argument(cache)
    cache.add_argument("--out", required=True, help="output cache dir, reusable via a solve cache")

    args = p.parse_args(argv)

    if args.command == "make-cache":
        spec = specs.load(transaction.Spec, args.spec, prog="plan")
        repos = load_repositories(spec)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        load_base(repos, out, None, spec["arch"])
        print(f"plan: cached {len(repos)} repo(s)", file=sys.stderr)
        return

    solve_spec = specs.load(SolveSpec, args.spec, prog="plan")
    install = transaction.install_specs(solve_spec["install"], solve_spec["install_files"])
    repos = load_repositories(solve_spec)
    seeds = [Path(cache_dir).absolute() for cache_dir in solve_spec["cache"]]
    if not install:
        transaction.write(Path(args.out), [], repos)
        return

    with ExitStack() as stack:
        installroot = None
        if solve_spec["lower"]:
            installroot = stack.enter_context(storage.root(storage.Snapshot(solve_spec["lower"]))).path
        tx = plan(
            repos,
            install,
            CACHEDIR,
            installroot,
            solve_spec["arch"],
            seeds,
        )
    transaction.write(Path(args.out), tx, repos)


if __name__ == "__main__":
    main()
