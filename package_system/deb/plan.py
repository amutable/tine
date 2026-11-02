#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Resolve Debian transactions against pinned metadata with APT.

`apt-get --print-uris` resolves the transaction and downloads nothing. It prints the URI, size
and checksum of every package that it would download. Each repository is staged in its own
directory, so the URI also identifies the repository that APT selected the package from.
"""

import argparse
import hashlib
import io
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

import aptget
import deb822
import specs
import util
from util import MAGIC, decompressor

import rootfs
import transaction
from href import relative_href

# The pool names packages by their SHA256, so APT must print this checksum.
HASH = "SHA256"


class SolveSpec(transaction.Spec):
    install: list[str]
    install_files: list[str]
    lower: list[str]
    # The solve spec that all package systems share has `cache`. This driver ignores it, because
    # APT needs no prebuilt solver cache.
    cache: list[str]


def resolved_packages(
    repositories: list[transaction.Repository],
    staged: Path,
    output: str,
) -> list[transaction.TransactionPackage]:
    """Parse the output of `apt-get --print-uris` into the packages of the transaction.

    Two repositories can hold a package with the same name, version and architecture but with
    different contents. The URI identifies the repository that APT selected, and the entry records
    that repository.
    """
    resolved: list[transaction.TransactionPackage] = []
    for line in output.splitlines():
        # apt-get starts the line of a download with a quoted URI. Every other line is a message.
        if not line.startswith("'"):
            continue
        fields = line.split()
        # APT omits the checksum if the index has none of the requested type. The line then has
        # three fields.
        if len(fields) not in (3, 4):
            util.fail(f"plan: apt-get stated a fetch this cannot read: {line!r}")
        uri, name, size, *stated = fields
        package_id = unquote(name).removesuffix(".deb")
        location = urlsplit(uri.strip("'"))
        # APT does not escape `?` and `#`. `urlsplit()` would cut a `Filename` with one of them
        # at that character.
        if location.query or location.fragment:
            util.fail(f"plan: APT selected {package_id} from a location this cannot read: {uri}")
        path = Path(unquote(location.path))
        if not path.is_relative_to(staged):
            util.fail(f"plan: APT selected {package_id} from {path}, which is not a pinned repository")
        position, *parts = path.relative_to(staged).parts
        repository = repositories[int(position)]
        algorithm, _, digest = "".join(stated).partition(":")
        if algorithm != HASH:
            util.fail(f"plan: {repository.id} states no {HASH} for {package_id}")
        resolved.append(
            transaction.entry(
                package_id,
                repository,
                digest,
                relative_href(repository.id, f"package {package_id}", "/".join(parts)),
                deb822.integer(size, f"{repository.id}: package {package_id} size", minimum=1),
            )
        )
    print(f"plan: resolved {len(resolved)} packages", file=sys.stderr)
    return resolved


def _repository_id(value: str) -> str:
    if (
        not value
        or not value.isascii()
        or any(not (character.isalnum() or character in ".+_-") for character in value)
    ):
        util.fail(f"plan: repository id cannot be represented in an APT Release: {value!r}")
    return value


def _digest(stream: io.BufferedIOBase) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def index_entries(index: Path) -> list[tuple[str, int, str]]:
    """Return the checksum, size and name of every entry that the staged Release needs for an index.

    APT looks for the uncompressed name `Packages` in a Release. If the Release lists only
    `Packages.xz`, APT concludes that the repository has no `Packages` index. So a compressed index
    gets two entries: one for the uncompressed content and one for the compressed file.
    """
    with index.open("rb") as raw:
        opener = decompressor(raw.read(MAGIC))
        raw.seek(0)
        if opener is None:
            digest, size = _digest(raw)
            return [(digest, size, deb822.INDEX)]
        with opener(raw) as stream:
            plain = _digest(stream)
    with index.open("rb") as raw:
        packed = _digest(raw)
    return [(*plain, deb822.INDEX), (*packed, index.name)]


def _release(repository: transaction.Repository, index: Path, arch: str) -> str:
    rid = _repository_id(repository.id)
    entries = "".join(f" {digest} {size} {name}\n" for digest, size, name in index_entries(index))
    return (
        "Origin: Tine\n"
        f"Label: {rid}\n"
        "Suite: tine\n"
        f"Codename: {rid}\n"
        "Date: Thu, 01 Jan 1970 00:00:00 UTC\n"
        f"Architectures: {arch}\n"
        "Description: Tine pinned package repository\n"
        "SHA256:\n" + entries
    )


def stage_repositories(
    repositories: list[transaction.Repository],
    scratch: Path,
    arch: str,
) -> tuple[Path, Path, Path]:
    """Stage each pinned Packages index as a flat repository with a label and a pin priority.

    The directory of a repository is named after its position in `repositories`.
    `resolved_packages()` reads the position from the URI of a download.
    """
    staged = scratch / "repositories"
    sources = scratch / "sources.list"
    preferences = scratch / "preferences"
    priorities = sorted({repository.priority for repository in repositories})
    source_lines: list[str] = []
    preference_stanzas: list[str] = []
    for position, repository in enumerate(repositories):
        index = deb822.package_index(repository.path, repository.id)
        directory = staged / str(position)
        directory.mkdir(parents=True)
        (directory / index.name).symlink_to(index)
        (directory / "Release").write_text(_release(repository, index, arch), encoding="utf-8")
        source_lines.append(f"deb [trusted=yes] {directory.as_uri()} ./")

        # A repository with a lower tine priority gets a higher pin, and its packages win even
        # over a higher version in another repository. A pin above 1000 also allows a downgrade.
        # Repositories with the same priority get the same pin. Among them, APT selects the higher
        # version, and for equal versions the repository that `sources.list` names first.
        pin = 1001 + len(priorities) - priorities.index(repository.priority)
        preference_stanzas.append(
            f"Package: *\nPin: release l={_repository_id(repository.id)}\nPin-Priority: {pin}\n"
        )
    sources.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    preferences.write_text("\n".join(preference_stanzas), encoding="utf-8")
    return sources, preferences, staged


def solve(
    repositories: list[transaction.Repository],
    install: list[str],
    lower: list[str],
    arch: str,
) -> list[transaction.TransactionPackage]:
    """Resolve the transaction with APT against the staged repositories, without network access.

    `arch` is the Debian name of the architecture, for example `amd64`.
    """
    with ExitStack() as stack:
        scratch = Path(stack.enter_context(TemporaryDirectory(prefix="deb-plan.", dir="/var/tmp")))
        root = scratch / "root"
        if lower:
            root = stack.enter_context(rootfs.rootfs("/installroot", lowers=lower))
        status = root / "var/lib/dpkg/status"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.touch(exist_ok=True)

        sources, preferences, staged = stage_repositories(repositories, scratch, arch)
        arguments = [
            *aptget.options(scratch, status, arch),
            *aptget.option("Dir::Etc::sourcelist", sources),
            *aptget.option("Dir::Etc::preferences", preferences),
            # Without this option, APT prints the strongest checksum that the index has.
            *aptget.option("Acquire::ForceHash", HASH),
        ]
        env = aptget.environment(scratch)
        aptget.run([*arguments, "update"], env, "update")

        # Debian packages declare no dependency on an essential package such as the shell, so a
        # closure of the requested names alone would lack these packages. The pattern selects
        # every package that the archive marks as essential.
        essential = f"?essential ?architecture({arch})"
        output = aptget.run(
            [
                *arguments,
                "--print-uris",
                # A transaction cannot express a removal, so make APT fail a solve that needs a removal.
                "--no-remove",
                "install",
                *install,
                essential,
            ],
            env,
            "solve",
        )
        resolved = resolved_packages(repositories, staged, output)
        if not resolved and not lower:
            # A layer resolves to nothing when its lower layers already have every requested
            # package. Without lower layers, an empty result is an error.
            util.fail(f"plan: resolved nothing for {' '.join(install)}")
        return resolved


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="plan")
    solve_parser = parser.add_subparsers(dest="command", required=True).add_parser(
        "solve",
        help="resolve the closure, writing the transaction",
    )
    specs.add_argument(solve_parser)
    solve_parser.add_argument(
        "--out",
        required=True,
        help="output transaction JSON (remote adds url/size; local adds location)",
    )
    args = parser.parse_args(argv)

    spec = specs.load(SolveSpec, args.spec, prog="plan")
    install = transaction.install_specs(spec["install"], spec["install_files"])
    repositories = sorted(
        transaction.load_repositories(spec, absolute=True),
        key=lambda repository: repository.priority,
    )
    resolved = solve(repositories, install, spec["lower"], spec["arch"]) if install else []
    transaction.write(Path(args.out), resolved, repositories)


if __name__ == "__main__":
    main()
