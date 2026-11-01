# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""RPM repositories and package-build rules."""

load("//distribution:defs.bzl", "distribution")
load("//package:build.bzl", "PACKAGE_BUILD_ATTRS", "package_build", "package_build_source")
load("//package:buildroot.bzl", "BuildrootInfo")
load("//package:packaging.bzl", "pinned_packaging")
load("//package:repository.bzl", "RepositoryPin", "declare_remote_repository")
load("//package:system.bzl", "PackageSystemInfo")

PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"

def rpm_remote_repository(
    name: str,
    architectures: list[str],
    baseurl: str | None = None,
    rpmrepo_mirror: str | None = None,
    rpmrepo_snapshot: str | None = None,
    **kwargs,
) -> None:
    """Declare an RPM repository, optionally pinned to an rpmrepo compose snapshot."""
    if (rpmrepo_mirror == None) != (rpmrepo_snapshot == None):
        fail("rpm_remote_repository requires rpmrepo_mirror and rpmrepo_snapshot together: {}".format(name))
    pin = None
    if rpmrepo_mirror != None:
        pin = RepositoryPin(
            baseurl = rpmrepo_mirror.rstrip("/") + "/" + rpmrepo_snapshot,
            metadata = {"rpmrepo.mirror": rpmrepo_mirror, "rpmrepo.snapshot": rpmrepo_snapshot},
        )
    declare_remote_repository(
        name = name,
        what = "rpm_remote_repository",
        label = "tine:rpm-remote-repository",
        package_system = PACKAGE_SYSTEM,
        architectures = architectures,
        baseurl = baseurl,
        pin = pin,
        **kwargs,
    )

def _rpm_package_impl(ctx: AnalysisContext) -> list[Provider]:
    base_buildroot = ctx.attrs.buildroot[BuildrootInfo]
    source_tree = ctx.attrs.source_tree
    in_place_spec = ctx.attrs.in_place_spec if source_tree != None else None
    spec_file = ctx.attrs.spec
    if source_tree != None and in_place_spec != None:
        spec_file = source_tree.project(in_place_spec)
    packaging = None
    if source_tree != None and ctx.attrs.in_place_packaging != None:
        packaging = pinned_packaging(
            ctx,
            base_buildroot,
            "git-core",
            ctx.attrs._packaging,
            source_tree.project(ctx.attrs.in_place_packaging),
            ctx.attrs.in_place_packaging_distro,
        )
        spec_file = packaging.project(ctx.attrs.package + ".spec")
    if spec_file == None:
        fail("rpm_package: a regular build requires a spec file")

    return package_build(
        ctx,
        owner = "rpm_package",
        package_system = ctx.attrs._package_system,
        base = base_buildroot,
        build_requires = ctx.attrs.build_requires,
        buildroot_packages = ctx.attrs.buildroot_packages,
        configured_dev = ctx.attrs.configured_dev,
        keep_build_dir = True,
        packages = "rpms",
        subpackages = ctx.attrs.subpackages,
        category = "rpmbuild",
        spec = {
            "dist": ctx.attrs.dist,
            "in_place_rpmbuild_options": ctx.attrs.in_place_rpmbuild_options if source_tree != None else [],
            "packaging": packaging,
            "release": ctx.attrs.release,
            "rpmbuild_options": ctx.attrs.rpmbuild_options,
            "source_date_epoch": ctx.attrs.source_date_epoch,
            "source_tree": source_tree,
            "sources": ctx.attrs.srcs if in_place_spec == None and packaging == None else [],
            "spec_file": spec_file,
        },
    )

_rpm_package = rule(
    impl = _rpm_package_impl,
    attrs = PACKAGE_BUILD_ATTRS
    | {
        "dist": attrs.string(default = ".aos"),
        "in_place_rpmbuild_options": attrs.list(
            attrs.string(),
            default = [],
            doc = "extra rpmbuild CLI options used only with a source-tree override",
        ),
        "in_place_spec": attrs.option(
            attrs.string(),
            default = None,
            doc = "source-tree-relative spec whose directory supplies in-place Source/Patch files",
        ),
        "package": attrs.string(doc = "the rpm package Name: (distinct from the buck target name)"),
        "release": attrs.string(
            doc = "dist-stripped Release base; the build freezes %autorelease = <release>%{?dist}",
        ),
        "rpmbuild_options": attrs.list(
            attrs.string(),
            default = [],
            doc = "extra rpmbuild CLI options (--with=..., --without=..., --define=...)",
        ),
        "spec": attrs.option(attrs.source(), default = None, doc = "the rpm spec file (derived from `package` by the macro)"),
        "_package_system": attrs.dep(providers = [PackageSystemInfo], default = PACKAGE_SYSTEM),
    },
    supports_incoming_transition = True,
)

def rpm_package(
    name: str,
    package: str,
    spec: str | None = None,
    dev: bool | None = None,
    in_place_packaging: str | None = None,
    in_place_packaging_distro: str | Select | None = None,
    in_place_rpmbuild_options: list[str] | None = None,
    in_place_spec: str | None = None,
    source_tree: str | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build an RPM from archives or a prepared Git tree, with a local checkout override.

    `distro` names the distribution of the buildroot's release. The target is then incompatible with an
    architecture that the release does not serve.
    """
    source_tree, configured_dev = package_build_source(
        "rpm_package",
        name,
        source_tree,
        dev,
        "in_place_spec",
        in_place_spec,
        in_place_packaging,
        in_place_packaging_distro,
    )
    if spec == None and (source_tree == None or (in_place_spec == None and in_place_packaging == None)):
        spec = package + ".spec"

    # Without `distro`, distribution.distributed() would declare an alias for every release of the catalog,
    # and each alias would build the package on the buildroot of one release for another one. An imported
    # package names no distribution.
    distributed = {"target_compatible_with": target_compatible_with, "visibility": visibility}
    if distro != None:
        distributed = distribution.distributed(name, distro, visibility, target_compatible_with)
    _rpm_package(
        name = name,
        package = package,
        spec = spec,
        configured_dev = configured_dev,
        source_tree = source_tree,
        in_place_packaging = in_place_packaging,
        in_place_packaging_distro = in_place_packaging_distro,
        in_place_rpmbuild_options = in_place_rpmbuild_options,
        in_place_spec = in_place_spec,
        **(distributed | kwargs),
    )
