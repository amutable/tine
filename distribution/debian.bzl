# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The conventional Debian distribution declaration."""

load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load("//package_system/deb:rules.bzl", "ARCHIVE_MIRROR", "PACKAGE_SYSTEM", "deb_remote_repository")
load("//platform:architecture.bzl", "architecture")

# The signing keys of the archive, under the names that ftp-master serves them as. Print the
# fingerprint of a downloaded key with:
#   gpg --show-keys --with-fingerprint archive-key-13.asc
# When you add a key, compare its fingerprint with the debian-archive-keyring package.
_DEBIAN_KEY_URL = "https://ftp-master.debian.org/keys/{}.asc"
_DEBIAN_SIGNING_KEYS = {
    "archive-key-12": "B8B80B5B623EAB6AD8775C45B7C5D7D6350947F8",
    "archive-key-13": "04B54C3CDCA79751B16BC6B5225629DF75B188BD",
}

def _signing_keys(*names) -> dict[str, str]:
    """Return the `signing_keys` attribute for the named archive keys."""
    for name in names:
        if name not in _DEBIAN_SIGNING_KEYS:
            fail("no Debian signing key {} is known; add its fingerprint to distribution/debian.bzl".format(name))
    return {_DEBIAN_SIGNING_KEYS[name]: _DEBIAN_KEY_URL.format(name) for name in names}

def _package_sets(arch: str, overrides: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return the package sets for one architecture, with the overrides of the caller applied.

    The kernel package of Debian has the architecture in its name, for example `linux-image-amd64`.
    So the `bootable` set differs per architecture, and `debian.release()` selects the sets by
    architecture.
    """
    sets = dict(_DEBIAN_PACKAGE_SETS)
    sets["bootable"] = sorted(sets["bootable"] + ["linux-image-{}".format(arch)])
    sets.update(overrides)
    return sets

_DEBIAN_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        # systemd loads libcryptsetup, libfdisk and the tpm2-tss libraries with dlopen(), so the
        # systemd packages declare no dependency on them. Without libcryptsetup, systemd-sysext and
        # portablectl cannot set up the verity partitions of a DDI and fail with "Operation not
        # supported". systemd-repart needs libfdisk. Measured boot and TPM2 unlocking need
        # tpm2-tss. The package name of a tpm2-tss library contains its soname, so a soname bump
        # renames the package and the solve fails. Run `apt-cache search libtss2-esys` in the box to
        # find the new name.
        "libcryptsetup12",
        "libfdisk1",
        "libtss2-esys-3.0.2-0t64",
        "libtss2-mu-4.0.1-0t64",
        "libtss2-rc0t64",
        "libtss2-tcti-device0t64",
        "login",
        "systemd",
        "systemd-boot-efi",
        "systemd-boot-tools",
        # Debian puts systemd-repart and systemd-keyutil into the systemd-repart package. The systemd
        # package doesn't depend on systemd-repart.
        "systemd-repart",
        "systemd-sysv",
        "udev",
        "util-linux",
    ],
    # `build-essential` depends on the compiler and the tools that every Debian package build assumes.
    "buildroot": ["build-essential"],
    "initrd": [
        "bash",
        "cryptsetup-bin",
        "kmod",
        "libtss2-esys-3.0.2-0t64",
        "libtss2-mu-4.0.1-0t64",
        "libtss2-rc0t64",
        "libtss2-tcti-device0t64",
        "mount",
        "systemd",
        "systemd-cryptsetup",
        "systemd-imds",
        "udev",
        "util-linux",
    ],
}

_COMPONENTS = ("main", "contrib", "non-free-firmware", "non-free")
_REQUIRED = "main"

# The root box of a Debian release contains the packages that resolve, verify and install a box.
# tine//distribution:tools.box contains everything else.
_DEBIAN_ROOT_PACKAGES = [
    "apt",
    "bash",
    "coreutils",
    "dpkg",
    # A relaxed box binds the host's /run, and nss-systemd reads the caller's identity from the host's
    # UserDB services there. Without libnss_systemd, getpwuid() cannot find a systemd-homed caller, and
    # ssh-keygen aborts.
    "libnss-systemd",
    "python3",
    # package_system/deb/verify.py runs sqv. apt depends on sqv today, but list it explicitly.
    "sqv",
]

def _check_name(name: str) -> None:
    if not name.startswith("debian.") or name.count(".") != 1:
        fail("debian release name must be 'debian.<suite>': {}".format(name))

def _release(
    name: str,
    architectures: list[str],
    signing_keys: dict[str, str],
    suite: str | None = None,
    archive_snapshot: str | None = None,
    archive_mirror: str = ARCHIVE_MIRROR,
    repository_urls: dict[str, str] = {},
    package_set_overrides: dict[str, list[str]] = {},
    enable_repository_groups: list[str] = [],
    disable_repository_groups: list[str] = [],
    additional_repositories: list[str] = [],
    repository_priorities: dict[str, int] = {},
    visibility: list[str] | None = None,
) -> None:
    """Declare the targets of a Debian release: repositories, release, distribution and package manager.

    `signing_keys` are the archive keys, normally from `debian.signing_keys(...)`. At least one
    archive key must have signed the Release of the suite. The caller passes the keys explicitly,
    so that a reviewer sees which keys a release trusts.

    All components share one archive pin. Packages from `main` and `contrib` are only guaranteed
    to install together when the two components come from the same timestamp. `repository_urls`
    must therefore override every component or none. With a partial override, refresh-catalog
    would advance the pinned components and leave the overridden components behind.
    """
    _check_name(name)
    suite = suite or name.removeprefix("debian.")
    unknown = [component for component in repository_urls if component not in _COMPONENTS]
    if unknown:
        fail("debian.release: unknown repository components {}".format(sorted(unknown)))
    if (archive_snapshot == None) == (not repository_urls):
        fail("debian.release: takes archive_snapshot or repository_urls, not both or neither: {}".format(name))
    if repository_urls and sorted(repository_urls) != sorted(_COMPONENTS):
        fail(
            "debian.release: repository_urls must cover every component {}: {}".format(
                sorted(_COMPONENTS),
                name,
            ),
        )

    for component in _COMPONENTS:
        deb_remote_repository(
            name = "{}.{}.repository".format(name, component),
            suite = suite,
            component = component,
            architectures = architectures,
            baseurl = repository_urls.get(component),
            archive_mirror = archive_mirror if archive_snapshot else None,
            archive_snapshot = archive_snapshot,
            signing_keys = signing_keys,
        )

    repository_universe(
        name = name + ".repositories",
        target_compatible_with = distribution.serves(name, architectures),
        package_system = PACKAGE_SYSTEM,
        # A box resolves against the universe and nothing else. An image runs the tools of the release's
        # tools box against what it installs from these repositories, so the tools box resolves from
        # them as well.
        required_repositories = [":{}.{}.repository".format(name, _REQUIRED)] + additional_repositories,
        optional_repository_groups = {component: [":{}.{}.repository".format(name, component)] for component in _COMPONENTS if component != _REQUIRED},
        default_repository_groups = ["non-free-firmware"],
    )
    distribution.new(name = name + ".distribution", architectures = architectures, visibility = visibility)
    os_release(
        name = name + ".release",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        box = ":" + name + ".box",
        repository_universe = ":" + name + ".repositories",
        package_sets = architecture.select({arch: _package_sets(architecture.spelling(arch, "deb"), package_set_overrides) for arch in architectures}),
        visibility = visibility,
    )
    box.new(
        name = name + ".box",
        architectures = architectures,
        packages = _DEBIAN_ROOT_PACKAGES,
        release = ":" + name + ".release",
        repository_universe = ":" + name + ".repositories",
        root = True,
        visibility = visibility,
    )
    package_manager(
        name = name + ".package-manager",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        release = ":" + name + ".release",
        enable_repository_groups = enable_repository_groups,
        disable_repository_groups = disable_repository_groups,
        additional_repositories = additional_repositories,
        repository_priorities = repository_priorities,
        visibility = visibility,
    )

debian = struct(
    release = _release,
    signing_keys = _signing_keys,
)
