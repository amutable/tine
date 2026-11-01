# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Native package managers, and the repository configuration that their consumers run in a box."""

load("//distribution:defs.bzl", "distribution")
load("//platform:architecture.bzl", "architecture")
load(":local_packages.bzl", "LocalPackageUniverseInfo")
load(":release.bzl", "OsReleaseInfo")
load(
    ":repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "LocalPackageRepositoryInfo",
    "PackageRepositoryInfo",
    "check_repository",
    "configure_repository",
    "merge_repositories",
    "select_repositories",
)
load(":solver.bzl", "solver_cache")
load(":system.bzl", "PackageSystemInfo")

_LOCAL_REPOSITORY_PRIORITY = 50
_REMOTE_REPOSITORY_PRIORITY = 99

# ManagedRepository is a repository of a package manager before a consumer configures it for the box
# that the consumer runs in.
ManagedRepository = record(
    dependency = Dependency,
    priority = int,
)

PackageManagerInfo = provider(
    doc = "The repository selection used for native package operations, in whichever box the consumer runs them.",
    fields = {
        # The architecture that packages are resolved and installed for. The box runs as the host
        # architecture.
        "arch": provider_field(str),
        "local_packages": provider_field(Dependency | None, default = None),
        "package_sets": provider_field(dict[str, list[str]]),
        "package_system": provider_field(Dependency),
        "repositories": provider_field(list[ManagedRepository]),
    },
)

def _package_manager_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.base != None:
        if ctx.attrs.release != None:
            fail("derived package_manager cannot set release")
        if ctx.attrs.enable_repository_groups or ctx.attrs.disable_repository_groups:
            fail("derived package_manager cannot change repository groups")
        base = ctx.attrs.base[PackageManagerInfo]
        package_system = base.package_system
        package_sets = base.package_sets
        priorities = {managed.dependency.label.name: managed.priority for managed in base.repositories}
        repositories = [managed.dependency for managed in base.repositories]
        local_packages = ctx.attrs.local_packages if ctx.attrs.local_packages != None else base.local_packages
    else:
        if ctx.attrs.release == None:
            fail("package_manager requires release when base is not set")
        release_info = ctx.attrs.release[OsReleaseInfo]
        package_system = release_info.package_system
        package_sets = release_info.package_sets
        repositories = select_repositories(
            release_info.repository_universe,
            ctx.attrs.enable_repository_groups,
            ctx.attrs.disable_repository_groups,
        )
        priorities = {}
        local_packages = ctx.attrs.local_packages

    repositories = merge_repositories(package_system, repositories + ctx.attrs.additional_repositories)
    by_id = {repository.label.name: repository for repository in repositories}

    # 'extra' is synthesized per install for buildroot_packages and local-package selection.
    if "extra" in by_id:
        fail("package_manager: repository id 'extra' is reserved for extra-package selection")

    for rid in ctx.attrs.repository_priorities:
        if rid not in by_id:
            fail("priority override names unknown repository '{}'".format(rid))
    for repository in repositories:
        check_repository("package_manager", repository)

    managed = []
    for repository in repositories:
        rid = repository.label.name
        default_priority = _LOCAL_REPOSITORY_PRIORITY if repository.get(LocalPackageRepositoryInfo) != None else _REMOTE_REPOSITORY_PRIORITY
        managed.append(
            ManagedRepository(
                dependency = repository,
                priority = ctx.attrs.repository_priorities.get(rid, priorities.get(rid, default_priority)),
            ),
        )

    return [
        DefaultInfo(),
        PackageManagerInfo(
            arch = ctx.attrs._arch,
            package_sets = package_sets,
            package_system = package_system,
            repositories = managed,
            local_packages = local_packages,
        ),
    ]

def configure_repositories(
    ctx: AnalysisContext,
    package_manager: PackageManagerInfo,
    box: Dependency,
) -> (list[ConfiguredPackageRepositoryInfo], list[Artifact]):
    """Configure the package manager's repositories for a consumer that runs in `box`.

    Return the configured repositories and the solver caches of the package system.
    """
    package_system = package_manager.package_system
    configured_repositories = []
    solver_caches = []
    for managed in package_manager.repositories:
        configured = configure_repository(ctx, managed.dependency, managed.priority, package_system, box, {})
        if package_system[PackageSystemInfo].solver_cache:
            solver_caches.append(solver_cache(ctx, box, package_system, configured, package_manager.arch))
        configured_repositories.append(configured)
    return configured_repositories, solver_caches

_package_manager = rule(
    impl = _package_manager_impl,
    attrs = {
        "additional_repositories": attrs.list(
            attrs.dep(providers = [PackageRepositoryInfo]),
            default = [],
        ),
        "base": attrs.option(attrs.dep(providers = [PackageManagerInfo]), default = None),
        "disable_repository_groups": attrs.list(attrs.string(), default = []),
        "enable_repository_groups": attrs.list(attrs.string(), default = []),
        "local_packages": attrs.option(
            attrs.dep(providers = [LocalPackageUniverseInfo]),
            default = None,
            doc = "locally built packages preferred over repository packages during installs",
        ),
        "release": attrs.option(attrs.dep(providers = [OsReleaseInfo]), default = None),
        "repository_priorities": attrs.dict(attrs.string(), attrs.int(), default = {}),
        "_arch": attrs.string(default = architecture.configured(), doc = "the architecture to resolve for"),
    },
)

def package_manager(name: str, **kwargs) -> None:
    if not name.endswith(".package-manager"):
        fail("package_manager name must end with '.package-manager': {}".format(name))

    # An image reaches this through a dependency and configures it on the way, so it takes the
    # package's compatibility without the per-distribution aliases a named target needs.
    _package_manager(name = name, **(distribution.attrs() | kwargs))
