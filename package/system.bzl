# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Native package-system drivers bundled behind one provider."""

PackageSystemInfo = provider(
    doc = "A native package ecosystem and the drivers that operate on it.",
    fields = {
        "arch_schema": provider_field(str),
        "build": provider_field(Dependency | None),
        "database_format": provider_field(str),
        "database_paths": provider_field(list[str]),
        "extract": provider_field(Dependency),
        "index": provider_field(Dependency | None),
        "install": provider_field(Dependency),
        "keyring": provider_field(Dependency | None),
        "metadata_vouches": provider_field(bool),
        "package_suffix": provider_field(str),
        "pkgdb": provider_field(Dependency),
        "plan": provider_field(Dependency),
        "snapshot": provider_field(Dependency),
        "solver_cache": provider_field(bool),
        "verify": provider_field(Dependency | None),
    },
)

def _package_system_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        PackageSystemInfo(
            arch_schema = ctx.attrs.arch_schema,
            extract = ctx.attrs.extract,
            snapshot = ctx.attrs.snapshot,
            install = ctx.attrs.install,
            pkgdb = ctx.attrs.pkgdb,
            index = ctx.attrs.index,
            package_suffix = ctx.attrs.package_suffix,
            plan = ctx.attrs.plan,
            build = ctx.attrs.build,
            solver_cache = ctx.attrs.solver_cache,
            database_format = ctx.attrs.database_format,
            database_paths = ctx.attrs.database_paths,
            keyring = ctx.attrs.keyring,
            metadata_vouches = ctx.attrs.metadata_vouches,
            verify = ctx.attrs.verify,
        ),
    ]

package_system = rule(
    impl = _package_system_impl,
    attrs = {
        "arch_schema": attrs.string(doc = "the platform/architecture.bzl schema this system names architectures in"),
        "build": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "build a native package",
        ),
        "database_format": attrs.string(
            doc = "suffix naming the format `pkgdb` captures the database in",
        ),
        "database_paths": attrs.list(
            attrs.string(),
            doc = "image-root-relative paths the installed package database occupies",
        ),
        "extract": attrs.exec_dep(providers = [RunInfo], doc = "bootstrap payload extractor"),
        "index": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "write the metadata of a repository of locally built packages",
        ),
        "install": attrs.exec_dep(providers = [RunInfo], doc = "install packages into a root"),
        "keyring": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "build the keyring of a repository's declared signing keys that `verify` checks against; unset where `verify` reads the declared key files as they are",
        ),
        "metadata_vouches": attrs.bool(
            default = False,
            doc = "whether the repository's signed metadata vouches for a package rather than the package for itself; a lock then retains the metadata it was resolved against",
        ),
        "package_suffix": attrs.string(doc = "file suffix a selected native package is named with"),
        "pkgdb": attrs.exec_dep(providers = [RunInfo], doc = "capture an installed root's package database"),
        "plan": attrs.exec_dep(providers = [RunInfo], doc = "resolve package transactions"),
        "snapshot": attrs.exec_dep(providers = [RunInfo], doc = "repository snapshot generator"),
        "solver_cache": attrs.bool(
            default = True,
            doc = "whether the planner reuses metadata prebuilt by its `make-cache` verb",
        ),
        "verify": attrs.option(
            attrs.exec_dep(providers = [RunInfo]),
            default = None,
            doc = "check an upstream package's signature against a repository's declared keys",
        ),
    },
)
