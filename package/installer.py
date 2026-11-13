# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run one native package installation against the root its spec names.

Every package system's installer answers the same request: a directory holding the exact package
closure, and either a snapshot to capture over lower snapshots, or a root the caller has already
mounted. Every package system restores and captures the root the same way, so this module does it and
the driver only runs the transaction.
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
import storage
import uapi16

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
    # The install captures the snapshot store `snapshot` over the snapshot stores in `lower`. An image layer
    # sets `installroot` to the root that it mounted instead.
    snapshot: str | None
    installroot: str | None
    lower: list[str]
    # Only a box install sets `target`, to the directory that the sandbox mounts.
    target: str | None
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
    install: Callable[[Path, Path, InstallSpec], None],
    argv: list[str] | None = None,
) -> None:
    """Mount the root this invocation names and hand it to `install`.

    The callback receives the closure directory, the root to install into and the spec.
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
    lowers = spec["lower"]

    if spec["installroot"] is not None:
        if spec["snapshot"] is not None or spec["target"] is not None or lowers:
            fail(f"{prog}: installroot excludes snapshot, target and lower")
        installroot = Path(spec["installroot"]).absolute()
        install(packages_dir, installroot, spec)
        _normalize(installroot, spec)
        return

    if spec["snapshot"] is None:
        fail(f"{prog}: one of snapshot and installroot is required")
    out = Path(spec["snapshot"])

    # The closure is empty when the lower snapshots contain every package of the layer, for example in the
    # buildroot of a package whose spec needs nothing beyond the base root. The package manager then doesn't
    # run, and the snapshot only repeats the manifest of the snapshot below it.
    if lowers and not any(packages_dir.iterdir()):
        (out / "blobs").mkdir(parents=True)
        shutil.copyfile(Path(lowers[-1]) / uapi16.MANIFEST, out / uapi16.MANIFEST)
    else:
        with storage.root(storage.Snapshot(lowers)) as restored:
            with rootfs.rootfs(BUILDROOT, bind=restored.path, apivfs=True, strict_unmount=True):
                install(packages_dir, Path(BUILDROOT), spec)
                _normalize(Path(BUILDROOT), spec)
            storage.capture(restored, out, lowers, epoch=int(os.environ["SOURCE_DATE_EPOCH"]))

    if spec["target"] is None:
        return
    snapshot = storage.Snapshot([*lowers, out])
    # The sandbox stacks the directories of a box's layers and decodes no whiteouts. A file that a
    # layer deletes would still show up from the layer below.
    if deleted := storage.delta(snapshot, len(lowers)):
        fail(f"{prog}: a box layer cannot delete from the layers below it, but deletes {', '.join(deleted)}")
    storage.write_buck_tree(snapshot, Path(spec["target"]))
