# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Resolve and install native packages into filesystem roots."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load("//platform:architecture.bzl", "architecture")
load(":local_packages.bzl", "LocalPackageUniverseInfo", "select_local_packages")
load(":manager.bzl", "PackageManagerInfo", "configure_repositories")
load(
    ":repository.bzl",
    "ConfiguredPackageRepositoryInfo",
    "LocalPackageInfo",
    "materialize_local_repository",
    "select_package_artifacts",
)
load(":solver.bzl", "solve_command")
load(":system.bzl", "PackageSystemInfo")

_EXTRA_REPO_PRIORITY = 50

_RootInfo = provider(
    doc = "The installed root tree carried out of an anonymous target.",
    fields = {"root": provider_field(Artifact)},
)

def resolve_packages(
    ctx: AnalysisContext,
    package_manager_dep: Dependency,
    box_dep: Dependency,
    install: list[str],
    stack: list[Artifact],
    extra_packages: list[Artifact] = [],
    local_seed: list[str] | None = None,
    identifier: str | None = None,
    install_files: list[Artifact] = [],
) -> Artifact:
    """Plan an install in `box_dep` and select its exact package artifacts.

    A manager with local packages offers the seed's runtime closure of locally built packages to
    the solver; `local_seed` widens that seed beyond this request, e.g. to a whole layer stack.
    `install_files` are JSON lists of install specs that the solve adds to `install`."""
    package_manager = package_manager_dep[PackageManagerInfo]
    configured_repositories, solver_caches = configure_repositories(ctx, package_manager, box_dep)
    system = package_manager.package_system[PackageSystemInfo]

    if package_manager.local_packages != None:
        if extra_packages:
            fail("resolve_packages: explicit extra packages cannot combine with manager local packages")

        # The seed of local packages has to be known at analysis, and analysis can't read install files.
        if install_files:
            fail("resolve_packages: install files cannot combine with manager local packages")
        selected = select_local_packages(
            package_manager.local_packages[LocalPackageUniverseInfo],
            local_seed if local_seed != None else install,
        )
        extra_packages = [dep[LocalPackageInfo].packages for dep in selected]

    extra_repo = None
    if extra_packages:
        extra_repo = materialize_local_repository(
            ctx,
            box_dep,
            package_manager.package_system,
            extra_packages,
        )

    plan_repositories = []
    if extra_repo != None:
        plan_repositories.append(
            ConfiguredPackageRepositoryInfo(
                id = "extra",
                directory = extra_repo,
                priority = _EXTRA_REPO_PRIORITY,
            )
        )
    plan_repositories.extend(configured_repositories)
    prefix = identifier + "/" if identifier != None else ""
    tx = ctx.actions.declare_output(prefix + "transaction.json")
    plan = solve_command(
        ctx = ctx,
        box = box_dep[box.Info],
        system = system,
        repositories = plan_repositories,
        install = install,
        install_files = install_files,
        arch = package_manager.arch,
        output = tx.as_output(),
        solver_caches = solver_caches,
        lowers = stack,
        spec_name = prefix + "solve.spec.json",
    )
    ctx.actions.run(plan, category = "plan", identifier = identifier or "install", allow_cache_upload = True)

    return select_package_artifacts(
        ctx,
        tx,
        repositories = configured_repositories,
        suffix = system.package_suffix,
        extra_packages = extra_packages,
        name = prefix + "install.closure",
    )

def _install_actions(
    ctx: AnalysisContext,
    package_manager_dep: Dependency,
    box_dep: Dependency,
    install: list[str],
    stack: list[Artifact],
    extra_packages: list[Artifact],
    install_files: list[Artifact] = [],
    identifier: str | None = None,
) -> Artifact:
    package_manager = package_manager_dep[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    closure = resolve_packages(ctx, package_manager_dep, box_dep, install, stack, extra_packages, identifier = identifier, install_files = install_files)
    prefix = identifier + "/" if identifier != None else ""
    out = ctx.actions.declare_output(prefix + ("install.delta" if stack else "root"), dir = True)
    work = ctx.actions.declare_output(prefix + "install.work", dir = True) if stack else None
    cmd = cmd_args(
        box.run(box = box_dep[box.Info], exe = system.install),
        specs.args(
            ctx.actions,
            prefix + "install.spec.json",
            {
                "arch": architecture.spelling(package_manager.arch, system.arch_schema),
                "box_config": False,
                "docs": True,
                "installroot": None,
                "langs": [],
                "lower": stack,
                "packages_dir": closure,
                "target": out.as_output(),
                "work": work.as_output() if work != None else None,
            },
        ),
    )
    ctx.actions.run(cmd, category = "install", identifier = identifier or "install")
    return out

def _install_packages_impl(ctx: AnalysisContext) -> list[Provider]:
    root = _install_actions(ctx, ctx.attrs.package_manager, ctx.attrs.box, ctx.attrs.install, [], ctx.attrs.extra_packages)
    return [DefaultInfo(default_output = root), _RootInfo(root = root)]

_install_packages = anon_rule(
    impl = _install_packages_impl,
    attrs = {
        "box": attrs.dep(providers = [box.Info]),
        "extra_packages": attrs.list(attrs.source(), default = []),
        "install": attrs.list(attrs.string()),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
    },
    artifact_promise_mappings = {
        "root": lambda p: p[_RootInfo].root,
    },
)

def install_packages(
    ctx: AnalysisContext,
    package_manager: Dependency,
    box_dep: Dependency,
    install: list[str],
    stack: list[Artifact] = [],
    extra_packages: list[Artifact] = [],
    install_files: list[Artifact] = [],
    identifier: str | None = None,
) -> Artifact:
    if not stack:
        root = ctx.actions.anon_target(
            _install_packages,
            {
                "box": box_dep,
                "extra_packages": extra_packages,
                "install": sorted(install),
                "name": "//install-packages:{}".format(package_manager.label.name),
                "package_manager": package_manager,
            },
        ).artifact("root")
        # A promise is resolved before the anon target is analysed, so its path kind is asserted here.
        return ctx.actions.assert_short_path(ctx.actions.assert_has_content_based_path(root), short_path = "root")
    return _install_actions(ctx, package_manager, box_dep, install, stack, extra_packages, install_files, identifier)
