# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Construct package-solver commands and reusable repository caches."""

load("//:specs.bzl", "specs")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//platforms:architecture.bzl", "architecture")
load(":repository.bzl", "ConfiguredPackageRepositoryInfo", "encode_repositories")
load(":system.bzl", "PackageSystemInfo")

def solve_command(
    ctx: AnalysisContext,
    box: BoxInfo,
    system: PackageSystemInfo,
    repositories: list[ConfiguredPackageRepositoryInfo],
    install: list[str],
    arch: str,
    output: OutputArtifact | None = None,
    solver_caches: list[Artifact] = [],
    lowers: list[Artifact] = [],
    spec_name: str = "solve.spec.json",
) -> cmd_args:
    """Construct a package solve command for one configured solver context.

    The transaction destination stays on the command line: the same command is published as a
    run target, where the caller names the transaction it wants written.
    """
    command = cmd_args(
        box_run(box = box, exe = system.plan),
        "solve",
        specs.args(
            ctx.actions,
            spec_name,
            {
                "arch": architecture.spelling(arch, system.arch_schema),
                "cache": solver_caches,
                "install": install,
                "lower": lowers,
                "repositories": encode_repositories(repositories),
            },
        ),
    )
    if output != None:
        command.add("--out", output)
    return command

def _solver_cache_impl(ctx: AnalysisContext) -> list[Provider]:
    box = ctx.attrs.box[BoxInfo]
    system = ctx.attrs.package_system[PackageSystemInfo]
    repository = ConfiguredPackageRepositoryInfo(
        id = ctx.attrs.repository_id,
        directory = ctx.attrs.repository_dir,
        # A solver cache contains the parsed metadata, which does not depend on the priority that a
        # solve gives the repository.
        priority = 0,
        baseurl = ctx.attrs.baseurl,
    )
    # Every consumer of a promised artifact would have to assert a content-based path; keep the plain one.
    cache = ctx.actions.declare_output("cache", dir = True, has_content_based_path = False)
    ctx.actions.run(
        cmd_args(
            box_run(box = box, exe = system.plan),
            "make-cache",
            specs.args(
                ctx.actions,
                "make-cache.spec.json",
                {
                    "arch": architecture.spelling(ctx.attrs.arch, system.arch_schema),
                    "repositories": encode_repositories([repository]),
                },
            ),
            "--out",
            cache.as_output(),
        ),
        category = "solver_cache",
        identifier = repository.id,
    )
    return [DefaultInfo(default_output = cache)]

_solver_cache = anon_rule(
    impl = _solver_cache_impl,
    attrs = {
        "arch": attrs.string(),
        "baseurl": attrs.option(attrs.string(), default = None),
        "box": attrs.dep(providers = [BoxInfo]),
        "package_system": attrs.dep(providers = [PackageSystemInfo]),
        "repository_dir": attrs.source(),
        "repository_id": attrs.string(),
    },
    artifact_promise_mappings = {
        "cache": lambda providers: providers[DefaultInfo].default_outputs[0],
    },
)

def solver_cache(
    ctx: AnalysisContext,
    box: Dependency,
    package_system: Dependency,
    repository: ConfiguredPackageRepositoryInfo,
    arch: str,
) -> Artifact:
    """Reuse parsed repository metadata for an identical solver context."""
    return ctx.actions.anon_target(
        _solver_cache,
        {
            "arch": arch,
            "baseurl": repository.baseurl,
            "box": box,
            "package_system": package_system,
            "repository_dir": repository.directory,
            "repository_id": repository.id,
        },
    ).artifact("cache")
