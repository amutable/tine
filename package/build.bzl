# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The attributes, the checks and the outputs that the package-build rules of all package systems share."""

load("//:specs.bzl", "spec_args")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//git:defs.bzl", "git")
load("//project:defs.bzl", "project")
load(":buildroot.bzl", "BuildrootInfo", "package_buildroot")
load(":manager.bzl", "PackageManagerInfo")
load(":repository.bzl", "LocalPackageInfo")
load(":system.bzl", "PackageSystemInfo")

_PRIVATE = "__tine"

PACKAGE_BUILD_ATTRS = {
    "build_requires": attrs.list(
        attrs.string(),
        default = [],
        doc = "the packages that the build needs beyond the shared base buildroot, installed as a delta over it",
    ),
    "buildroot": attrs.dep(
        providers = [BuildrootInfo],
        doc = "the shared base buildroot",
    ),
    "buildroot_packages": attrs.list(
        attrs.dep(providers = [LocalPackageInfo]),
        default = [],
        doc = "our packages that the solve of build_requires prefers over upstream ones",
    ),
    "configured_dev": attrs.bool(
        doc = "whether project configuration selects this package for dev mode",
    ),
    "in_place_packaging": attrs.option(
        attrs.string(),
        default = None,
        doc = "source-tree-relative JSON file that pins, per distro, the packaging commit whose recipe and local sources a source-tree build uses",
    ),
    "in_place_packaging_distro": attrs.option(
        attrs.string(),
        default = None,
        doc = "the distro whose entry of the in_place_packaging file the build uses",
    ),
    "source_date_epoch": attrs.int(doc = "the SOURCE_DATE_EPOCH of the build"),
    "source_tree": attrs.option(
        attrs.source(allow_directory = True),
        default = None,
        doc = "a prepared tree built in place of the declared sources",
    ),
    "srcs": attrs.list(attrs.source(), default = [], doc = "the source and patch files that the recipe names"),
    "subpackages": attrs.list(
        attrs.string(),
        doc = "the declared binary package names",
    ),
    "_packaging": attrs.exec_dep(providers = [RunInfo], default = "tine//package:packaging"),
}

def package_build(
    ctx: AnalysisContext,
    *,
    owner: str,
    package_system: Dependency,
    base: BuildrootInfo,
    build_requires: list[str],
    buildroot_packages: list[Dependency],
    configured_dev: bool,
    keep_build_dir: bool,
    packages: str,
    subpackages: list[str],
    category: str,
    spec: dict[str, typing.Any],
) -> list[Provider]:
    """Install `build_requires` over `base`, and run the `build` driver of `package_system` on the result.

    `spec` contains the fields of the driver spec that the calling rule owns. `packages` names the output
    directory of the packages. A dev build keeps its build directory between runs if `keep_build_dir` is
    set. `owner` starts a failure message.
    """
    installs = base.package_manager[PackageManagerInfo].package_system
    if installs.label.raw_target() != package_system.label.raw_target():
        fail(
            "{}: the package manager {} of the buildroot installs packages of {}, not {}".format(
                owner,
                base.package_manager.label.raw_target(),
                installs.label.raw_target(),
                package_system.label.raw_target(),
            ),
        )
    system = package_system[PackageSystemInfo]
    reserved = [name for name in ("build", "buildroot") if name in subpackages]
    if reserved:
        fail("{}: subpackages {} have the names of built-in sub-targets".format(owner, reserved))
    stack = package_buildroot(ctx, owner, base, build_requires, buildroot_packages)

    out = project.kept_dir(ctx.actions, packages)
    build_dir = project.kept_dir(ctx.actions, _PRIVATE + "/build") if configured_dev and keep_build_dir else None

    # Each binary subpackage is a file in one output directory rather than an output of its own. The build
    # then needs a single writable mount, and the rest of the project stays read-only.
    subpackages_dir, files = project.binaries_dir(
        ctx.actions,
        _PRIVATE + "/subpackages",
        [name + system.package_suffix for name in subpackages],
        "{} {}: subpackages".format(owner, ctx.label.name),
    )

    ctx.actions.run(
        cmd_args(
            box_run(box = base.box[BoxInfo], exe = system.build),
            spec_args(
                ctx.actions,
                "build.spec.json",
                spec
                | {
                    "build_dir": build_dir.as_output() if build_dir != None else None,
                    # The base root is the lowest layer, and the layer of build_requires is above it.
                    "lower": stack,
                    "out": out.as_output(),
                    "subpackages": subpackages,
                    "subpackages_out": subpackages_dir.as_output(),
                },
            ),
        ),
        category = category,
        # A dev build keeps its build directory between runs, and its result depends on that state.
        allow_cache_upload = not configured_dev,
        no_outputs_cleanup = configured_dev,
    )

    sub_targets = {name: [DefaultInfo(default_output = files[name + system.package_suffix])] for name in subpackages}
    sub_targets["buildroot"] = [DefaultInfo(default_outputs = stack)]
    if build_dir != None:
        sub_targets["build"] = [DefaultInfo(default_output = build_dir)]
    return [
        DefaultInfo(default_output = out, sub_targets = sub_targets),
        LocalPackageInfo(package_system = package_system, packages = out),
    ]

def package_build_source(
    owner: str,
    name: str,
    source_tree: str | None,
    dev: bool | None,
    in_place_recipe: str,
    in_place_recipe_value: str | None,
    in_place_packaging: str | None,
    in_place_packaging_distro: str | Select | None,
) -> (str | None, bool):
    """Check the in-place arguments of a package-build macro, and return its source tree and dev mode.

    `in_place_recipe` names the macro's argument for a recipe in the source tree, and
    `in_place_recipe_value` is its value. Without `source_tree`, the macro builds the checkout slot
    `<name>.source` once the slot contains files. `owner` starts a failure message.
    """
    if in_place_packaging != None and in_place_recipe_value != None:
        fail("{}: {} sets both in_place_packaging and {}".format(owner, name, in_place_recipe))
    if (in_place_packaging == None) != (in_place_packaging_distro == None):
        fail("{}: {} must set both in_place_packaging and in_place_packaging_distro, or neither".format(owner, name))
    if source_tree == None:
        source = name + ".source"
        populated = git.checkout(name = source)
        source_tree = ":" + source if populated else None
    return source_tree, project.is_dev(name, source = source_tree, override = dev)
