#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Resolve package transactions against pinned repository databases.

libalpm does the resolving, bound directly in `alpm.py`. It is the same library that will
apply the transaction, so plan and install cannot disagree about which package provides a
capability or which version is newer, and none of those semantics are reimplemented here.

What this driver owns is the frame around it: the pinned databases are staged where alpm looks
for them, in the order that gives repository priority its meaning, the layer stack below becomes
the root the solve resolves against, and each resolved package becomes a transaction entry named
by the content checksum its repository published. Nothing is fetched and only the throwaway root
staged below is written to, so a solve is a pure function of the pins.
"""

import argparse
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import specs
from util import fail

import alpm
import storage
import transaction
from href import relative_href


class SolveSpec(transaction.Spec):
    install: list[str]
    install_files: list[str]
    lower: list[str]
    # Written by the neutral solve command, unread here: alpm prebuilds no repository metadata.
    cache: list[str]


def load_repositories(spec: transaction.Spec) -> list[transaction.Repository]:
    """Order the configured repositories the way libalpm will search them.

    alpm has no repository priority of its own: the first registered database providing a
    package wins, so registration order below is what carries it. A lower number wins, and a
    stable sort leaves declaration order to break a tie.
    """
    return sorted(transaction.load_repositories(spec), key=lambda repository: repository.priority)


def database(repository: transaction.Repository) -> Path:
    databases = sorted(repository.path.glob("*.db"))
    if len(databases) != 1:
        fail(f"{repository.id}: expected exactly one *.db in {repository.path}, found {len(databases)}")
    return databases[0]


def stage(dbpath: Path, repositories: list[transaction.Repository]) -> None:
    """Put each pinned database where alpm looks for the repository it is registered under."""
    sync = dbpath / "sync"
    sync.mkdir(parents=True, exist_ok=True)
    for repository in repositories:
        # alpm finds a database by its registered name, not the name its mirror serves it under.
        shutil.copyfile(database(repository), sync / f"{repository.id}.db")


def resolved_packages(
    repositories: list[transaction.Repository],
    resolved: list[alpm.Package],
) -> list[transaction.TransactionPackage]:
    """Turn what libalpm resolved into the transaction a closure is selected from."""
    by_id = {repository.id: repository for repository in repositories}

    entries = []
    for package in resolved:
        repository = by_id[package.repo]
        location = relative_href(package.repo, f"package {package.id}", package.filename)
        if repository.baseurl is None:
            location = alpm.local_location(location, f"{package.repo}: {package.id}")
        entries.append(transaction.entry(package.id, repository, package.sha256, location, package.size))

    print(f"plan: resolved {len(entries)} packages", file=sys.stderr)
    return entries


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="plan")
    # One verb, because the neutral solve command names it; alpm needs no prebuilt metadata cache.
    solve = parser.add_subparsers(dest="command", required=True).add_parser(
        "solve",
        help="resolve the closure, writing the transaction",
    )
    specs.add_argument(solve)
    solve.add_argument(
        "--out",
        required=True,
        help="output transaction JSON (remote adds url/size; local adds location)",
    )
    args = parser.parse_args(argv)

    spec = specs.load(SolveSpec, args.spec, prog="plan")
    install = transaction.install_specs(spec["install"], spec["install_files"])
    repositories = load_repositories(spec)
    if not install:
        transaction.write(Path(args.out), [], repositories)
        return

    with ExitStack() as stack:
        scratch = Path(stack.enter_context(TemporaryDirectory(prefix="plan.")))
        if spec["lower"]:
            root = stack.enter_context(storage.root(storage.Snapshot(spec["lower"]))).path
        else:
            root = scratch / "root"
            root.mkdir()

        dbpath = root / alpm.DBPATH
        stage(dbpath, repositories)
        solver = stack.enter_context(alpm.Alpm(root, dbpath, spec["arch"]))
        for repository in repositories:
            solver.register(repository.id)
        resolved = solver.resolve(install)

    transaction.write(Path(args.out), resolved_packages(repositories, resolved), repositories)


if __name__ == "__main__":
    main()
