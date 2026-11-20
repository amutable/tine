# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Shared native package buildroots."""

load("//box:runtime.bzl", "BoxInfo")
load(":install.bzl", "install_closure", "install_packages", "resolve_packages")
load(":manager.bzl", "PackageManagerInfo")
load(":repository.bzl", "LocalPackageInfo")

BuildrootInfo = provider(
    doc = "A base root, and the package manager and box that build native packages on it.",
    fields = {
        "box": provider_field(Dependency),
        "package_manager": provider_field(Dependency),
        "root": provider_field(Artifact),
    },
)

# The buildroot of one package: the packages it adds to the base root, and the same installed as a delta.
PackageBuildroot = record(
    # The installed layers, lowest first: the base root, and the delta when there is one.
    layers = list[Artifact],
    # The package directory that the delta installs, or None when the package needs no delta.
    closure = Artifact | None,
)

def _buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    manager = ctx.attrs.package_manager[PackageManagerInfo]
    if manager.local_packages != None:
        fail(
            "buildroot: a package manager with local packages is not supported; buildroot "
            + "contents must come from explicit, cycle-checked package buildroot deps"
        )
    packages = ctx.attrs.packages
    if ctx.attrs.package_set != None:
        if packages:
            fail("buildroot: packages and package_set are mutually exclusive")
        packages = manager.package_sets.get(ctx.attrs.package_set)
        if packages == None:
            fail("buildroot: unknown package set {!r}".format(ctx.attrs.package_set))
    if not packages:
        fail("buildroot: packages or package_set must be specified")
    root = install_packages(ctx, ctx.attrs.package_manager, ctx.attrs.box, packages)
    return [
        DefaultInfo(default_output = root),
        BuildrootInfo(
            box = ctx.attrs.box,
            root = root,
            package_manager = ctx.attrs.package_manager,
        ),
    ]

_buildroot = rule(
    impl = _buildroot_impl,
    supports_incoming_transition = True,
    attrs = {
        "box": attrs.dep(providers = [BoxInfo], doc = "the box that installs the buildroot and builds packages on it"),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "package_set": attrs.option(
            attrs.string(),
            default = None,
            doc = "release package set installed in every package buildroot",
        ),
        "packages": attrs.list(
            attrs.string(),
            default = [],
            doc = "explicit packages installed in every package buildroot",
        ),
    },
)

def _extra_packages(owner: str, base: BuildrootInfo, buildroot_packages: list[Dependency]) -> list[Artifact]:
    package_system = base.package_manager[PackageManagerInfo].package_system
    extra_packages = []
    for dependency in buildroot_packages:
        info = dependency[LocalPackageInfo]
        if info.package_system.label.raw_target() != package_system.label.raw_target():
            fail(
                "{}: buildroot dependency {} uses package system {}, expected {}".format(
                    owner,
                    dependency.label,
                    info.package_system.label.raw_target(),
                    package_system.label.raw_target(),
                ),
            )
        extra_packages.append(info.packages)
    return extra_packages

def _delta(
    ctx: AnalysisContext,
    owner: str,
    base: BuildrootInfo,
    install: list[str],
    buildroot_packages: list[Dependency],
    install_files: list[Artifact] = [],
    identifier: str | None = None,
) -> PackageBuildroot:
    """Return the buildroot of the base root and `install` and `install_files`, solved over the base root.

    The solve prefers the packages of `buildroot_packages`.
    """
    stack = [base.root]
    closure = resolve_packages(
        ctx,
        base.package_manager,
        base.box,
        install,
        stack,
        _extra_packages(owner, base, buildroot_packages),
        identifier = identifier,
        install_files = install_files,
    )
    delta = install_closure(ctx, base.package_manager, base.box, closure, stack, identifier)
    return PackageBuildroot(layers = stack + [delta], closure = closure)

def package_buildroot(
    ctx: AnalysisContext,
    owner: str,
    base: BuildrootInfo,
    build_requires: list[str],
    buildroot_packages: list[Dependency],
) -> PackageBuildroot:
    """Return the buildroot that one package builds on: the shared base root, plus `build_requires`.

    The solve prefers the packages of `buildroot_packages` over the repositories of the package manager.
    `owner` starts a failure message.
    """

    # The packages of buildroot_packages are only candidates for the solve, so they install nothing on
    # their own. rpm.branch() derives them from the BuildRequires of every architecture, so a package can
    # have them without any BuildRequires on this architecture.
    if not build_requires:
        return PackageBuildroot(layers = [base.root], closure = None)
    return _delta(ctx, owner, base, build_requires, buildroot_packages)

def recipe_buildroot(
    ctx: AnalysisContext,
    owner: str,
    base: BuildrootInfo,
    build_requires: list[str],
    buildroot_packages: list[Dependency],
    requires: Artifact,
) -> PackageBuildroot:
    """Return the buildroot of `build_requires` and the JSON list `requires` of build dependencies.

    Both lists are solved together in the base root. That solve can choose another provider or version for a
    package of `build_requires` than `package_buildroot()` does, so its root isn't always a superset of that
    one. `owner` starts a failure message.
    """
    return _delta(ctx, owner, base, build_requires, buildroot_packages, install_files = [requires], identifier = "recipe")

def buildroot(name: str, **kwargs) -> None:
    if not name.endswith(".buildroot"):
        fail("buildroot name must end with '.buildroot': {}".format(name))
    _buildroot(name = name, **kwargs)
