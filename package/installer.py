# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run one native package installation against the root its spec names.

Every package system's installer answers the same request: a directory holding the exact package
closure, and either a fresh root to fill, a delta to persist over a lower stack, or a root the
caller has already mounted. Which of those it is, and how the root is mounted and captured, is
the same work whichever system fills it, so it happens here and the driver is left with the
transaction itself.
"""

import os
import shutil
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

import specs
from util import fail

import rootfs

# A fixed install path avoids embedding Buck hashes and supports scriptlet chroots.
BUILDROOT = "/buildroot"

MINIMAL_NSSWITCH = """\
passwd: files
group: files
shadow: files
hosts: files dns
"""


# What documentation costs an image, and where the translations live.
DOC_PATHS = ("usr/share/doc", "usr/share/man", "usr/share/groff", "usr/share/info", "usr/share/gtk-doc")
LOCALES = "usr/share/locale"


def path_rules(docs: bool, langs: list[str]) -> list[tuple[bool, str]]:
    """Return the directory trees that an install without docs or with selected languages leaves out.

    rpm implements `nodocs` and `_install_langs` itself. pacman and dpkg filter by path, and this
    function returns the paths for them. Each rule is a flag and a directory tree relative to the
    root. The flag says whether the install keeps the tree. A later rule overrides an earlier rule,
    so the rule that keeps a language follows the rule that excludes all locales. Each package
    system renders the trees in its own glob syntax.
    """
    rules = []
    if not docs:
        rules += [(False, path) for path in DOC_PATHS]
    if langs:
        rules.append((False, LOCALES))
        rules += [(True, f"{LOCALES}/{lang}") for lang in langs]
    return rules


class InstallSpec(TypedDict):
    """The request `package/install.bzl`, `box/build.bzl` and an image layer all write."""

    arch: str
    packages_dir: str
    # Either an output root, bound at /buildroot to install into, or a root the caller mounted.
    target: str | None
    installroot: str | None
    lower: list[str]
    work: str | None
    box_config: bool
    langs: list[str]
    docs: bool


@contextmanager
def fresh_machine_id(installroot: Path) -> Generator[None]:
    """Leave a fresh root the marker systemd initializes on first boot, and an existing one be."""
    etc = installroot / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    machine_id = etc / "machine-id"
    fresh = not machine_id.exists()
    if fresh:
        machine_id.write_text("uninitialized\n")
    yield
    if fresh:
        # Packages may replace the marker during the transaction.
        machine_id.write_text("uninitialized\n")


def _configure_box(installroot: Path) -> None:
    """Materialize configuration needed before the box is ever booted."""
    factory_nsswitch = installroot / "usr/share/factory/etc/nsswitch.conf"
    nsswitch = installroot / "etc/nsswitch.conf"
    # authselect makes nsswitch.conf a symlink to the absolute path /etc/authselect/nsswitch.conf.
    # The installer runs outside the root, so the symlink does not resolve and writing through it
    # fails with ENOENT. authselect also stops applying profile updates once a file replaces the symlink.
    if nsswitch.is_symlink():
        pass
    elif factory_nsswitch.is_file():
        shutil.copy2(factory_nsswitch, nsswitch)
    elif not nsswitch.is_file():
        # Minimal boxes need not install systemd's factory configuration.
        nsswitch.write_text(MINIMAL_NSSWITCH)

    # Signing actions reach a PKCS#11 token through p11-kit's client module. Each distribution installs
    # p11-kit-client.so in a different directory. p11-kit looks up a relative module name in its own
    # module directory, so the relative name works in every box. Without P11_KIT_SERVER_ADDRESS, the
    # client module provides no token.
    modules = installroot / "etc/pkcs11/modules"
    modules.mkdir(parents=True, exist_ok=True)
    (modules / "p11-kit-client.module").write_text("module: p11-kit-client.so\n")

    # Create the mountpoint for the sandbox's resolver bind.
    resolv = installroot / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")


def _normalize(installroot: Path, spec: InstallSpec) -> None:
    """Settle what an install leaves behind whichever package system performed it.

    A driver parks its own database, because only it knows what its package manager wrote. What is
    here is what no package system owns: scriptlets that any of them run, and the configuration a
    box needs before it is first entered.
    """
    # ldconfig's auxiliary cache stores inode numbers and mtimes; ld.so.cache itself does not.
    (installroot / "var/cache/ldconfig/aux-cache").unlink(missing_ok=True)
    # from systemd-udev's scriptlet, not meant for images (see systemd docs/BUILDING_IMAGES.md)
    (installroot / "var/lib/systemd/random-seed").unlink(missing_ok=True)
    # Fedora's archlinux-keyring runs `pacman-key --init` in a scriptlet. The command generates a new
    # private key on every install. A root that contains the keyring then differs between two builds,
    # and every action that runs in the root misses the shared cache. tine's pacman drivers keep their
    # keyring in a directory of their own.
    shutil.rmtree(installroot / "etc/pacman.d/gnupg", ignore_errors=True)
    if spec["box_config"]:
        _configure_box(installroot)


def run(
    prog: str,
    install: Callable[[Path, Path, InstallSpec, bool], None],
    argv: list[str] | None = None,
) -> None:
    """Mount the root this invocation names and hand it to `install`.

    The callback receives the closure directory, the root to install into, the spec, and whether
    it is layering over packages a lower stack already carries.
    """
    spec = specs.parse(InstallSpec, prog, argv)
    os.environ.update(
        {
            # Do not pick up personal rpm macros or rpmrc from the box's home directory.
            "HOME": "/",
            # An unprivileged namespace cannot inspect /proc/1/root to detect the chroot itself.
            "SYSTEMD_IN_CHROOT": "1",
            # Buck directory artifacts cannot preserve subvolumes.
            "SYSTEMD_TMPFILES_FORCE_SUBVOL": "0",
            # Tine generates the hardware database and boot artifacts after package installation.
            "SYSTEMD_HWDB_UPDATE_BYPASS": "1",
            "KERNEL_INSTALL_BYPASS": "1",
        }
    )

    # An installer resolves its own paths against the root, so give it no relative ones.
    packages_dir = Path(spec["packages_dir"]).absolute()
    layered = bool(spec["lower"])

    if spec["installroot"] is not None:
        if spec["target"] is not None or layered or spec["work"] is not None:
            fail(f"{prog}: installroot excludes target, lower, and work")
        installroot = Path(spec["installroot"]).absolute()
        install(packages_dir, installroot, spec, layered)
        _normalize(installroot, spec)
        return

    if spec["target"] is None:
        fail(f"{prog}: one of target and installroot is required")

    # Bind sources must exist.
    target = Path(spec["target"])
    target.mkdir(parents=True, exist_ok=True)

    if layered:
        if spec["work"] is None:
            fail(f"{prog}: lower needs a work overlay directory")
        root = rootfs.rootfs(
            BUILDROOT,
            lowers=spec["lower"],
            upperdir=target,
            workdir=spec["work"],
            apivfs=True,
        )
    else:
        root = rootfs.rootfs(BUILDROOT, bind=target, capture_bind=True, apivfs=True)

    with root:
        install(packages_dir, Path(BUILDROOT), spec, layered)
        _normalize(Path(BUILDROOT), spec)

    # The sandbox stacks a box's layers as Buck stores them, without turning markers back into
    # overlayfs whiteouts. Deletions are not supported at box runtime right now.
    if layered and spec["box_config"] and (deleted := rootfs.deletions(target)):
        markers = ", ".join(map(str, deleted))
        fail(f"{prog}: a box layer cannot delete from the layers below it, but has {markers}")
