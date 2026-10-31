# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""RPM repositories and package-build rules."""

load("//:specs.bzl", "spec_args")
load("//box:build.bzl", "box_layer")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//git:defs.bzl", "git")
load("//package:buildroot.bzl", "BuildrootInfo", "package_buildroot")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:repository.bzl", "LocalPackageInfo", "RepositoryPin", "declare_remote_repository")
load("//package:system.bzl", "PackageSystemInfo")
load("//project:defs.bzl", "project")

PACKAGE_SYSTEM = "@tine//package_system/rpm:package_system"
_PRIVATE = "__tine"

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
    package_manager = base_buildroot.package_manager[PackageManagerInfo]
    system = package_manager.package_system[PackageSystemInfo]
    if system.build == None:
        fail("rpm_package: package system {} builds no packages".format(package_manager.package_system.label))

    buildroot = package_buildroot(ctx, "rpm_package", base_buildroot, ctx.attrs.build_requires, ctx.attrs.buildroot_packages)

    source_tree = ctx.attrs.source_tree
    rpms = project.kept_dir(ctx.actions, "rpms")
    build_dir = project.kept_dir(ctx.actions, _PRIVATE + "/build") if ctx.attrs.configured_dev else None
    in_place_spec = ctx.attrs.in_place_spec if source_tree != None else None
    spec_file = ctx.attrs.spec
    if source_tree != None and in_place_spec != None:
        spec_file = source_tree.project(in_place_spec)
    packaging = None
    if source_tree != None and ctx.attrs.in_place_packaging != None:
        # The pin file is the fetch's only input, so an edit elsewhere in the checkout does not rerun the fetch.
        packaging = ctx.actions.declare_output(_PRIVATE + "/packaging", dir = True)
        ctx.actions.run(
            cmd_args(
                # The tools box contains no git.
                box_run(box = box_layer(ctx, base_buildroot.box, ["git-core"]), exe = ctx.attrs._packaging, network = True),
                spec_args(
                    ctx.actions,
                    "packaging.spec.json",
                    {
                        "distro": ctx.attrs.in_place_packaging_distro,
                        "out": packaging.as_output(),
                        "pin": source_tree.project(ctx.attrs.in_place_packaging),
                    },
                ),
            ),
            allow_cache_upload = True,
            category = "rpm_packaging",
            local_only = True,
        )
        spec_file = packaging.project(ctx.attrs.package + ".spec")
    if spec_file == None:
        fail("rpm_package: a regular build requires a spec file")

    # Each binary subpackage is a file in one output directory rather than an output of its own. The
    # build then needs a single writable mount, and the rest of the project stays read-only.
    subpackages = project.kept_dir(ctx.actions, _PRIVATE + "/subpackages")
    sub_outputs = {s: subpackages.project(s + ".rpm") for s in ctx.attrs.subpackages}

    build = cmd_args(
        box_run(box = base_buildroot.box[BoxInfo], exe = system.build),
        spec_args(
            ctx.actions,
            "build.spec.json",
            {
                "build_dir": build_dir.as_output() if build_dir != None else None,
                "dist": ctx.attrs.dist,
                "in_place_rpmbuild_options": ctx.attrs.in_place_rpmbuild_options if source_tree != None else [],
                # bottom..top: the base lowerdir, then this package's BuildRequires delta
                "lower": buildroot,
                "out": rpms.as_output(),
                "packaging": packaging,
                "release": ctx.attrs.release,
                "rpmbuild_options": ctx.attrs.rpmbuild_options,
                "source_date_epoch": ctx.attrs.source_date_epoch,
                "source_tree": source_tree,
                "sources": ctx.attrs.srcs if in_place_spec == None and packaging == None else [],
                "spec_file": spec_file,
                "subpackages": ctx.attrs.subpackages,
                "subpackages_out": subpackages.as_output(),
            },
        ),
    )
    ctx.actions.run(build, category = "rpmbuild", allow_cache_upload = not ctx.attrs.configured_dev, no_outputs_cleanup = ctx.attrs.configured_dev)

    sub_targets = {s: [DefaultInfo(default_output = out)] for s, out in sub_outputs.items()}
    sub_targets["buildroot"] = [DefaultInfo(default_outputs = buildroot)]
    if build_dir != None:
        sub_targets["build"] = [DefaultInfo(default_output = build_dir)]
    return [
        DefaultInfo(default_output = rpms, sub_targets = sub_targets),
        LocalPackageInfo(
            package_system = package_manager.package_system,
            packages = rpms,
        ),
    ]

_rpm_package = rule(
    impl = _rpm_package_impl,
    attrs = {
        "build_requires": attrs.list(
            attrs.string(),
            default = [],
            doc = "the package's BuildRequires, installed as a delta over the shared base buildroot",
        ),
        "buildroot": attrs.dep(
            providers = [BuildrootInfo],
            doc = "the shared base buildroot",
        ),
        "buildroot_packages": attrs.list(
            attrs.dep(providers = [LocalPackageInfo]),
            default = [],
            doc = "our packages whose rpms overlay the buildroot (self-hosted BRs)",
        ),
        "configured_dev": attrs.bool(
            doc = "whether project configuration selects this package for dev mode",
        ),
        "dist": attrs.string(default = ".aos"),
        "in_place_packaging": attrs.option(
            attrs.string(),
            default = None,
            doc = "source-tree-relative JSON file that pins, per distro, the dist-git commit whose spec and Source/Patch files a source-tree build uses",
        ),
        "in_place_packaging_distro": attrs.option(
            attrs.string(),
            default = None,
            doc = "the distro whose entry of the in_place_packaging file the build uses",
        ),
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
        "source_date_epoch": attrs.int(doc = "per-package SDE from the changelog"),
        "source_tree": attrs.option(
            attrs.source(allow_directory = True),
            default = None,
            doc = "a prepared tree built in place instead of running %prep",
        ),
        "spec": attrs.option(attrs.source(), default = None, doc = "the rpm spec file (derived from `package` by the macro)"),
        "srcs": attrs.list(attrs.source(), default = [], doc = "Source/Patch files"),
        "subpackages": attrs.list(
            attrs.string(),
            doc = "declared binary subpackage names (the %package list)",
        ),
        "_packaging": attrs.exec_dep(providers = [RunInfo], default = "tine//package_system/rpm:packaging"),
    },
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
    **kwargs,
) -> None:
    """Build an RPM from archives or a prepared Git tree, with a local checkout override."""
    if in_place_packaging != None and in_place_spec != None:
        fail("rpm_package: {} sets both in_place_packaging and in_place_spec".format(name))
    if (in_place_packaging == None) != (in_place_packaging_distro == None):
        fail("rpm_package: {} must set both in_place_packaging and in_place_packaging_distro, or neither".format(name))
    if source_tree == None:
        source = name + ".source"
        populated = git.checkout(name = source)
        source_tree = ":" + source if populated else None
    if spec == None and (source_tree == None or (in_place_spec == None and in_place_packaging == None)):
        spec = package + ".spec"
    _rpm_package(
        name = name,
        package = package,
        spec = spec,
        configured_dev = project.is_dev(name, source = source_tree, override = dev),
        source_tree = source_tree,
        in_place_packaging = in_place_packaging,
        in_place_packaging_distro = in_place_packaging_distro,
        in_place_rpmbuild_options = in_place_rpmbuild_options,
        in_place_spec = in_place_spec,
        **kwargs,
    )
