#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Install an exact package set into a fresh, layered, or already-mounted root.

The same driver bootstraps boxes, assembles buildroots, and extends image layers. pacman
applies the transaction, so hooks and install scriptlets run as they would on a real system;
the planner has already chosen the exact set, and pacman refuses it if it is not closed.

It then normalizes the bookkeeping that would otherwise make two identical installs differ.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from util import fail

import alpm
import installer


def _source_date_epoch() -> int:
    """The assembly epoch every install stamps its database with."""
    return int(os.environ.get("SOURCE_DATE_EPOCH", "0"))


def write_config(directory: Path, arch: str, langs: list[str], docs: bool) -> Path:
    """Write the configuration for one install.

    NoExtract is where pacman expresses what rpm's `nodocs` and `_install_langs` do, and it is
    per-transaction configuration rather than image content, so it lives in this scratch file.
    """
    lines = [
        "[options]",
        # pacman would otherwise take this from the build host's uname.
        f"Architecture = {arch}",
        # Package bytes are pinned by checksum, and no key material is available here anyway.
        "SigLevel = Never",
    ]
    # Arch ships licenses apart from the documentation, so dropping the one keeps the other.
    lines += [
        f"NoExtract = {'!' if keep else ''}{tree}/*" for keep, tree in installer.path_rules(docs, langs)
    ]

    config = directory / "pacman.conf"
    config.write_text("\n".join(lines) + "\n")
    return config


def scratch_options(scratch: Path) -> list[str]:
    """Create and name the paths pacman would otherwise resolve inside the root it installs into.

    pacman resolves each of these before it starts, so they have to exist first.
    """
    options = []
    for option, name in (("--cachedir", "cache"), ("--gpgdir", "gnupg")):
        directory = scratch / name
        directory.mkdir(parents=True, exist_ok=True)
        options += [option, str(directory)]
    return [*options, "--logfile", str(scratch / "pacman.log")]


def _assume_installed() -> list[str]:
    """Tell pacman what the plan was already resolved against."""
    return [argument for name in alpm.ASSUME_INSTALLED for argument in ("--assume-installed", name)]


def install(
    packages_dir: Path,
    installroot: Path,
    scratch: Path,
    *,
    arch: str,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    # A closure entry keeps the extension its repository served, and pacman reads the format
    # from the file rather than the name either way.
    packages = sorted(str(p) for p in packages_dir.iterdir() if alpm.is_package(p.name))
    if not packages:
        # A layer whose request a lower one already satisfies resolves to nothing, which is not
        # an error: the root it would have installed into is already the root that was wanted.
        print("nothing to install", file=sys.stderr)
        return

    # alpm does not create this, and pacman resolves its other paths against the root itself.
    (installroot / alpm.LOCAL_DB).mkdir(parents=True, exist_ok=True)

    # alpm always runs the root's own usr/share/libalpm/hooks, which it rebases onto --root. What
    # --hookdir displaces is the administrator's directory, which alpm does not rebase, so without
    # this the box's /etc/pacman.d/hooks would run against the image. Point it at the image's
    # own, so an image that ships administrator hooks has them run for its installs; an image that
    # ships none gets an empty scratch directory rather than one created in it to say so.
    hookdir = installroot / "etc/pacman.d/hooks"
    if not hookdir.is_dir():
        hookdir = scratch / "hooks"
        hookdir.mkdir(exist_ok=True)

    print(f"installing {len(packages)} packages into {installroot}", file=sys.stderr)
    command = [
        "pacman",
        "--config", str(write_config(scratch, arch, langs or [], docs)),
        "--root", str(installroot),
        "--dbpath", str(installroot / alpm.DBPATH),
        *scratch_options(scratch),
        "--hookdir", str(hookdir),
        # The same capabilities the plan was resolved with; pacman checks the closure again.
        *_assume_installed(),
        "--noconfirm",
        # An incremental install re-offers packages a lower layer already carries.
        "--needed",
        "--upgrade",
        *packages,
    ]  # fmt: skip
    if subprocess.run(command).returncode != 0:
        fail("pacman transaction failed")


def parkdb(installroot: Path, epoch: int) -> None:
    """Replace the wall-clock install time alpm records with the assembly epoch."""
    local = installroot / alpm.LOCAL_DB
    descs = sorted(local.glob("*/desc"))
    if not descs:
        fail(f"no alpm database at {local}; the install did not populate it")
    for desc in descs:
        text = desc.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        stamped = "".join(
            f"{epoch}\n" if index and lines[index - 1].strip() == "%INSTALLDATE%" else line
            for index, line in enumerate(lines)
        )
        desc.write_text(stamped, encoding="utf-8")


def scrub(installroot: Path) -> None:
    """Remove the bookkeeping this package system leaves beside its database."""
    shutil.rmtree(installroot / alpm.DBPATH / "sync", ignore_errors=True)
    (installroot / "var/log/pacman.log").unlink(missing_ok=True)


def _install(packages_dir: Path, installroot: Path, spec: installer.InstallSpec) -> None:
    with installer.fresh_machine_id(installroot), tempfile.TemporaryDirectory(prefix="pacman.") as scratch:
        install(
            packages_dir,
            installroot,
            Path(scratch),
            arch=spec["arch"],
            langs=spec["langs"],
            docs=spec["docs"],
        )
    parkdb(installroot, _source_date_epoch())
    scrub(installroot)


def main(argv: list[str] | None = None) -> None:
    installer.run("install", _install, argv)


if __name__ == "__main__":
    main()
