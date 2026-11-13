#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Install an exact package set into a fresh, layered, or already-mounted root.

The same driver bootstraps boxes, assembles buildroots, and extends image layers. `plan.py` has
already chosen the exact packages, so APT gets these packages and no repository. APT decides the
order and runs dpkg. Maintainer scripts therefore run as on a real system, and dpkg configures a
pre-dependency before it unpacks the package that needs it.

For a fresh root, the driver extracts every package before it runs APT. dpkg cannot defer a
`preinst` script, so the first `preinst` that calls a shell would fail in a root without a shell.
debootstrap extracts the packages first for the same reason.
"""

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import aptget
import debfile
import util

import installer

COPYRIGHT = "usr/share/doc/*/copyright"
# `base-files` ships the merged-usr symlinks. `lay_down()` finds its file by this prefix.
BASE_FILES = "base-files_"

# Debian starts a daemon when its package is installed. A build has no init system to start it,
# so this `policy-rc.d` denies every start with exit status 101.
# See https://people.debian.org/~hmh/invokerc.d-policyrc.d-specification.txt.
POLICY_RC_D = "#!/bin/sh\nexit 101\n"


def path_rules(docs: bool, langs: list[str]) -> list[tuple[bool, str]]:
    """Return the globs for dpkg's `--path-exclude` and `--path-include`, in the order dpkg applies them."""
    rules = [(keep, f"{tree}/*") for keep, tree in installer.path_rules(docs, langs)]
    if not docs:
        # Debian ships the license of a package inside its documentation directory.
        rules.append((True, COPYRIGHT))
    return rules


def excluded_trees(docs: bool, langs: list[str]) -> list[str]:
    return [tree for keep, tree in installer.path_rules(docs, langs) if not keep]


def lay_down(packages: list[Path], installroot: Path, excluded: list[str]) -> None:
    """Extract the packages into a fresh root and remove the excluded trees.

    `base-files` goes first. It ships `/bin`, `/sbin`, `/lib` and `/lib64` as symlinks into `/usr`.
    A package that is extracted earlier and ships a file below one of these paths would create a
    directory there, and the symlink of `base-files` cannot replace a directory.

    `--path-exclude` of dpkg only skips files during extraction and never removes a file from disk.
    So this function removes each excluded tree completely. dpkg then unpacks every package again
    and restores the files that an include rule keeps.
    """
    for package in sorted(packages, key=lambda package: not package.name.startswith(BASE_FILES)):
        debfile.unpack(package, installroot)
    for tree in excluded:
        path = installroot / tree
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def is_fresh(installroot: Path) -> bool:
    """Return whether the root has no dpkg database yet."""
    return not (installroot / debfile.ADMINDIR / "status").is_file()


def prepare(installroot: Path) -> None:
    """Create the directories and files of the database that dpkg requires.

    Call this only for a transaction that installs a package. `touch()` changes the timestamps of
    `status`, and overlayfs then copies the file from the lower layer into this layer. A layer that
    installs nothing would contain a copy of the database.
    """
    admin = installroot / debfile.ADMINDIR
    for directory in ("info", "triggers", "updates"):
        (admin / directory).mkdir(parents=True, exist_ok=True)
    for name in ("status", "available"):
        (admin / name).touch(exist_ok=True)


def _replace(path: Path, content: bytes, mode: int) -> None:
    """Write a file at `path`. If `path` is a symlink, replace the symlink and leave its target alone."""
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    path.chmod(mode)


@contextmanager
def denied_daemons(installroot: Path) -> Generator[None]:
    """Install a `policy-rc.d` that denies daemon starts, and remove it after the transaction.

    No package ships this file. It belongs to the administrator, so the image must not keep the
    file that the build wrote.
    """
    policy = installroot / "usr/sbin/policy-rc.d"
    created = not policy.parent.is_dir()
    policy.parent.mkdir(parents=True, exist_ok=True)
    # Keep the existing file only if it is a regular file. `lay_down()` has extracted the packages
    # by now, and a package can ship a symlink at this path that points outside the root. Writing
    # through that symlink would change a file of the build host.
    kept = None
    if not policy.is_symlink() and policy.is_file():
        kept = (policy.read_bytes(), stat.S_IMODE(policy.stat().st_mode))
    try:
        # `_replace()` unlinks the existing file before it writes. The call is inside the `try`,
        # so that the `finally` restores the existing file if the write fails.
        _replace(policy, POLICY_RC_D.encode(), 0o755)
        yield
    finally:
        policy.unlink(missing_ok=True)
        if kept is not None:
            _replace(policy, *kept)
        elif created and not any(policy.parent.iterdir()):
            # A package can have installed files into the directory that this function created.
            # Remove the directory only if it is empty.
            policy.parent.rmdir()


def environment(scratch: Path) -> dict[str, str]:
    """Return the environment for APT and the maintainer scripts, which must not prompt."""
    return {
        **aptget.environment(scratch),
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBCONF_NONINTERACTIVE_SEEN": "true",
        # Debian kernel packages run hooks that build an initramfs. tine builds its own initrd,
        # so tell the hooks to build none.
        "INITRD": "No",
    }


def dpkg_options(installroot: Path, rules: list[tuple[bool, str]]) -> list[str]:
    """Return the APT options that pass the dpkg options to every dpkg that APT runs."""
    options = [
        f"--root={installroot}",
        f"--admindir={installroot / debfile.ADMINDIR}",
        "--force-unsafe-io",
        # dpkg checks a package against the architecture of the box that it runs in. The
        # transaction was resolved for the architecture of the target, which can differ.
        "--force-architecture",
        # dpkg runs debsig-verify if the box has it on PATH. The install must not depend on that.
        "--no-debsig",
        *(f"--path-{'include' if keep else 'exclude'}=/{glob}" for keep, glob in rules),
    ]
    return [argument for option in options for argument in aptget.option("DPkg::Options::", option)]


def install(
    packages_dir: Path,
    installroot: Path,
    *,
    arch: str,
    langs: list[str] | None = None,
    docs: bool = True,
) -> None:
    packages = sorted(path for path in packages_dir.iterdir() if path.suffix == ".deb")
    rules = path_rules(docs, langs or [])
    fresh = is_fresh(installroot)
    if not packages:
        # The closure is empty when a lower layer already has every requested package, and the
        # layer then has nothing to install. In a fresh root, an empty closure is an error.
        if fresh:
            util.fail("install: nothing to install, and nothing installed underneath")
        print("nothing to install", file=sys.stderr)
        return

    prepare(installroot)
    if fresh:
        lay_down(packages, installroot, excluded_trees(docs, langs or []))

    print(f"installing {len(packages)} packages into {installroot}", file=sys.stderr)
    with (
        tempfile.TemporaryDirectory(prefix="deb-install.", dir="/var/tmp") as scratch,
        denied_daemons(installroot),
    ):
        status = installroot / debfile.ADMINDIR / "status"
        arguments = [
            *aptget.options(Path(scratch), status, arch),
            *dpkg_options(installroot, rules),
            # The output goes to the build log, so dpkg needs no pseudo-terminal. The logs that
            # APT keeps of a transaction do not belong in an image.
            *aptget.option("DPkg::Use-Pty", "false"),
            *aptget.option("Dir::Log::Terminal", ""),
            *aptget.option("Dir::Log::History", ""),
            "--yes",
            # `plan.py` pins repositories above 1000, so it can select a version that is older
            # than the installed version.
            "--allow-downgrades",
            # APT resolves the transaction again here. Refuse a removal as `plan.py` does, because a
            # transaction cannot express a removal.
            "--no-remove",
            "install",
            *(str(package) for package in packages),
        ]
        aptget.run(arguments, environment(Path(scratch)), "install", capture=False)


def scrub(installroot: Path) -> None:
    """Remove the files that dpkg and APT leave next to the database and that no image needs.

    The transaction log of dpkg is not in the root. `--root` moves the install paths and the
    database, but dpkg takes the `log` path from the `dpkg.cfg` of the box unchanged, so dpkg
    writes the log to the box. It writes the lock files to the root.
    """
    leftovers = (
        "var/log/alternatives.log",
        f"{debfile.ADMINDIR}/available",
        f"{debfile.ADMINDIR}/lock",
        f"{debfile.ADMINDIR}/lock-frontend",
        f"{debfile.ADMINDIR}/triggers/Lock",
    )
    for name in leftovers:
        (installroot / name).unlink(missing_ok=True)
    # dpkg and debconf keep the previous version of each file that they rewrite, as `<name>-old`.
    for database in (debfile.ADMINDIR, "var/cache/debconf"):
        for leftover in (installroot / database).glob("*-old"):
            leftover.unlink()
    # `updates` holds the journal of a transaction. dpkg only reads it to resume an interrupted
    # transaction.
    for update in (installroot / debfile.ADMINDIR / "updates").glob("*"):
        update.unlink()


def _install(packages_dir: Path, installroot: Path, spec: installer.InstallSpec) -> None:
    with installer.fresh_machine_id(installroot):
        install(
            packages_dir,
            installroot,
            arch=spec["arch"],
            langs=spec["langs"],
            docs=spec["docs"],
        )
    scrub(installroot)


def main(argv: list[str] | None = None) -> None:
    installer.run("install", _install, argv)


if __name__ == "__main__":
    main()
