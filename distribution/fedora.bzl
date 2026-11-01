# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The conventional Fedora distribution declaration."""

load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//package:buildroot.bzl", "buildroot")
load("//package:manager.bzl", "package_manager")
load("//package:release.bzl", "os_release")
load("//package:repository.bzl", "repository_universe")
load("//package_system/rpm:rules.bzl", "PACKAGE_SYSTEM", "rpm_remote_repository")

_FEDORA_KEY_URL = "https://src.fedoraproject.org/rpms/fedora-repos/raw/rawhide/f/RPM-GPG-KEY-fedora-{}-primary"

# Fedora's per-release signing key fingerprints, as fedora-repos ships them, e.g.:
#   gpg --show-keys --with-fingerprint /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-46-primary
# cross-check them against https://fedoraproject.org/security/ when adding them
_FEDORA_SIGNING_KEYS = {
    "44": "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6",
    "45": "4F50A6114CD5C6976A7F1179655A4B02F577861E",
    "46": "D924B10D3E810DABDD8B56B596E7E91491211FCE",
    "47": "B2E766FA50CA6FA2D5ED06FC09D879E09488FB64",
}

def _signing_keys(*versions) -> dict[str, str]:
    """The `signing_keys` for a repository signed with the named Fedora releases' keys."""
    for version in versions:
        if version not in _FEDORA_SIGNING_KEYS:
            fail("no Fedora {} signing key is known; add its fingerprint to distribution/fedora.bzl".format(version))
    return {_FEDORA_SIGNING_KEYS[version]: _FEDORA_KEY_URL.format(version) for version in versions}

_FEDORA_PACKAGE_SETS = {
    "bootable": [
        "bash",
        "coreutils",
        "dbus-broker",
        "fedora-release",
        "kernel-core",
        "systemd",
        "systemd-boot-unsigned",
        "systemd-udev",
        "util-linux",
    ],
    "buildroot": ["@buildsys-build"],
    "initrd": [
        "bash",
        "kmod",
        # avoid full libcurl, not necessary for imds
        "libcurl-minimal",
        "systemd",
        "systemd-imds",
        "systemd-networkd",
        "systemd-udev",
        "veritysetup",
    ],
}

def _merge_package_sets(
    defaults: dict[str, list[str]],
    overrides: dict[str, list[str]],
) -> dict[str, list[str]]:
    package_sets = dict(defaults)
    package_sets.update(overrides)
    return package_sets

# The root box of a Fedora release contains the packages that resolve, verify and install a box.
# tine//distribution:tools.box contains everything else.
_FEDORA_ROOT_PACKAGES = [
    "coreutils",
    "python3-createrepo_c",
    "python3-libdnf5",
    # The rpm drivers define rpm macros through the rpm module.
    "python3-rpm",
    "rpm",
    # A relaxed box binds the host's /run, and nss-systemd reads the caller's identity from the host's
    # UserDB services there. Without libnss_systemd, getpwuid() cannot find a systemd-homed caller, and
    # ssh-keygen aborts.
    "systemd-libs",
]

def _check_name(name: str, family: str, version: str) -> None:
    expected = "{}.{}".format(family, version)
    if name != expected:
        fail("{} release name must be {!r}".format(family, expected))

def _release(
    name: str,
    version: str,
    architectures: list[str],
    signing_keys: dict[str, str],
    baseurl: str | None = None,
    rpmrepo_mirror: str | None = None,
    rpmrepo_snapshot: str | None = None,
    package_set_overrides: dict[str, list[str]] = {},
    additional_repositories: list[str] = [],
    repository_priorities: dict[str, int] = {},
    visibility: list[str] | None = None,
) -> None:
    """Declare the conventional Fedora release target bundle.

    `signing_keys` is stated rather than derived from `version`, as some releases (in particular rawhide)
    have multiple active trusted keys.
    """
    _check_name(name, "fedora", version)
    if rpmrepo_mirror == None and baseurl == None:
        if version == "rawhide":
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/development/rawhide/Everything/$basearch/os/"
        else:
            baseurl = "https://dl.fedoraproject.org/pub/fedora/linux/releases/{}/Everything/$basearch/os/".format(version)

    rpm_remote_repository(
        name = name + ".repository",
        architectures = architectures,
        baseurl = baseurl,
        rpmrepo_mirror = rpmrepo_mirror,
        rpmrepo_snapshot = rpmrepo_snapshot,
        signing_keys = signing_keys,
    )
    repository_universe(
        name = name + ".repositories",
        target_compatible_with = distribution.serves(name, architectures),
        package_system = PACKAGE_SYSTEM,
        # The additional repositories are part of the release, not just of installs from it. An
        # box resolves against the universe and nothing else, so leaving them out would stop a
        # release from building with the tools it ships: an image running a systemd from one of
        # these has to be assembled by the matching ukify and repart, which live in the same place.
        required_repositories = [":" + name + ".repository"] + additional_repositories,
    )
    distribution.new(name = name + ".distribution", architectures = architectures, visibility = visibility)
    os_release(
        name = name + ".release",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        box = ":" + name + ".box",
        repository_universe = ":" + name + ".repositories",
        package_sets = _merge_package_sets(_FEDORA_PACKAGE_SETS, package_set_overrides),
        visibility = visibility,
    )
    box.new(
        name = name + ".box",
        architectures = architectures,
        packages = _FEDORA_ROOT_PACKAGES,
        release = ":" + name + ".release",
        repository_universe = ":" + name + ".repositories",
        root = True,
        visibility = visibility,
    )
    package_manager(
        name = name + ".package-manager",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        release = ":" + name + ".release",
        additional_repositories = additional_repositories,
        repository_priorities = repository_priorities,
        visibility = visibility,
    )
    buildroot(
        name = name + ".buildroot",
        target_compatible_with = distribution.serves(name, architectures, in_box = True),
        # The buildroot is configured for its release, so its box is the release's tools box.
        incoming_transition = ":" + name + ".distribution",
        package_manager = ":" + name + ".package-manager",
        package_set = "buildroot",
        visibility = visibility,
    )

fedora = struct(
    release = _release,
    signing_keys = _signing_keys,
)
