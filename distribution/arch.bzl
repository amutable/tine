# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The conventional Arch Linux distribution declaration."""

load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load("//package_system/pacman:rules.bzl", "ARCHIVE_MIRROR", "PACKAGE_SYSTEM", "pacman_remote_repository")

# Arch's main signing keys, the set `archlinux-keyring` ships as archlinux-trusted, each served by the
# distribution's web key directory under the hash of its user ID's local part, e.g.:
#   gpg-wks-client --print-wkd-url dvzrv@master-key.archlinux.org
# cross-check the fingerprints against https://archlinux.org/master-keys/ when adding one
_ARCH_KEY_URL = "https://openpgpkey.master-key.archlinux.org/.well-known/openpgpkey/master-key.archlinux.org/hu/{}?l={}"
_ARCH_SIGNING_KEYS = {
    "anthraxx": ("D8AFDDA07A5B6EDFA7D8CCDAD6D055F927843F1C", "in9mwr4s84x7gm51851h343n3at1x61g"),
    "artafinde": ("3572FA2A1B067F22C58AF155F8B821B42A6FDCD7", "oq9akx45qcfcte4u1g4akuy9y8dgas4i"),
    "demize": ("69E6471E3AE065297529832E6BA0F5A2037F4F41", "1jnr6tupjpkxe3wady4jrn3kc918dsjt"),
    "dvzrv": ("2AC0A42EFB0B5CBC7A0402ED4DC95B6D7BE9892E", "eszskjyu5okmadiqczsckoun51k6qnae"),
    "gromit": ("99B6618472A3B3B814185BAED7D3D823B88BDB9B", "ed1qppfih3jee3fqsa8kydgw8wfdjtqd"),
}

def arch_signing_keys(*users) -> dict[str, str]:
    """The `signing_keys` for repositories whose packagers the named main keys vouch for, all of them by default.

    A packager's key counts once three declared main keys certify it, so declare at least three.
    """
    users = users or tuple(_ARCH_SIGNING_KEYS)
    if len({user: True for user in users}) < 3:
        fail("arch_signing_keys: three main keys must vouch for a packager, so declare at least three distinct ones: {}".format(users))
    for user in users:
        if user not in _ARCH_SIGNING_KEYS:
            fail("no Arch main key is known for {}; add its fingerprint to distribution/arch.bzl".format(user))
    return {_ARCH_SIGNING_KEYS[user][0]: _ARCH_KEY_URL.format(_ARCH_SIGNING_KEYS[user][1], user) for user in users}

_ARCH_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        "dbus-broker-units",
        "linux",
        "systemd",
        "systemd-sysvcompat",
        "util-linux",
    ],
    # `base-devel` is a metapackage, so makepkg's assumed build environment is one name.
    "buildroot": ["base-devel"],
    # Arch ships udev, networkd and IMDS support inside systemd, and veritysetup inside cryptsetup.
    "initrd": ["bash", "cryptsetup", "kmod", "systemd"],
}

# Arch has no numbered releases; a catalog pins a date in the archive instead.
_COMPONENTS = ("core", "extra", "multilib")

# The root box of an Arch release contains the packages that resolve, verify and install a box.
# tine//distribution:tools.box contains everything else. Extracting alpm packages needs no package
# tooling, so the root box bootstraps its own pacman.
_ARCH_ROOT_PACKAGES = [
    # The keyring driver takes the packagers' keys and the main keys' certifications from here.
    "archlinux-keyring",
    "bash",
    "coreutils",
    "pacman",
    # A relaxed box binds the host's /run, and nss-systemd reads the caller's identity from the host's
    # UserDB services there. Without libnss_systemd, getpwuid() cannot find a systemd-homed caller, and
    # ssh-keygen aborts.
    "systemd-libs",
]

def _check_name(name: str) -> None:
    if not name.startswith("arch.") or name.count(".") != 1:
        fail("arch release name must be 'arch.<release>': {}".format(name))

def arch_release(
    name: str,
    architectures: list[str],
    signing_keys: dict[str, str],
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
    """Declare the conventional Arch Linux release target bundle.

    `signing_keys` are the main keys whose certifications make a packager's key count, normally
    `arch_signing_keys()`; they are stated so that a reviewer sees which keys a release trusts.

    Every repository shares one archive pin, so the release stays internally consistent: core and
    extra are only guaranteed to solve together when they come from the same day. Overriding the
    mirrors is therefore all or nothing, since a release with one repository off the pin would
    have refresh-catalog advance its siblings past it.
    """
    _check_name(name)
    unknown = [component for component in repository_urls if component not in _COMPONENTS]
    if unknown:
        fail("arch_release: unknown repository components {}".format(sorted(unknown)))
    if (archive_snapshot == None) == (not repository_urls):
        fail("arch_release: takes archive_snapshot or repository_urls, not both or neither: {}".format(name))
    if repository_urls and sorted(repository_urls) != sorted(_COMPONENTS):
        fail(
            "arch_release: repository_urls must cover every component {}: {}".format(
                sorted(_COMPONENTS),
                name,
            ),
        )

    for component in _COMPONENTS:
        pacman_remote_repository(
            name = "{}.{}.repository".format(name, component),
            repository = component,
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
        required_repositories = [":" + name + ".core.repository"] + additional_repositories,
        optional_repository_groups = {
            "extra": [":" + name + ".extra.repository"],
            "multilib": [":" + name + ".multilib.repository"],
        },
        default_repository_groups = ["extra"],
    )
    distribution.new(name = name + ".distribution", architectures = architectures, visibility = visibility)
    package_sets = dict(_ARCH_PACKAGE_SETS)
    package_sets.update(package_set_overrides)
    os_release(
        name = name + ".release",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        box = ":" + name + ".box",
        repository_universe = ":" + name + ".repositories",
        package_sets = package_sets,
        visibility = visibility,
    )
    box.new(
        name = name + ".box",
        architectures = architectures,
        packages = _ARCH_ROOT_PACKAGES,
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
