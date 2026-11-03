# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public quarry API, exported as the `quarry` namespace.

A quarry release is a release folder (`image.artifacts`) plus the unsigned TUF targets that describe it
for quarry. See docs/user/quarry.md.
"""

load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//image:defs.bzl", "image")
load("//platform:architecture.bzl", "architecture")

def hardhat_compatible_with() -> Select:
    """`target_compatible_with` of hardhat and of everything that runs it.

    hardhat binds libpathrs through cgo. Debian packages only its Rust crate, not the C library.
    """
    return distribution.select({
        "DEFAULT": [],
        "debian": ["prelude//:none"],
    })

def _assemble_impl(
    actions: AnalysisActions,
    description: ArtifactValue,
    description_name: str,
    directory: OutputArtifact,
    files: Artifact,
    targets: Artifact,
) -> list[Provider]:
    names = description.read_json()["files"]
    if targets.basename in names:
        fail("quarry.release: {} is already published by a target of the release".format(targets.basename))
    release = {name: files.project(name) for name in names}
    release[description_name] = files.project(description_name)
    release[targets.basename] = targets

    # Copies, as in the folder that image.artifacts gathers: systemd-sysupdate skips symlinks.
    actions.copied_dir(directory, release)
    return []

# The names come from the description, because image.artifacts names the partitions only once they are built.
_assemble = dynamic_actions(
    impl = _assemble_impl,
    attrs = {
        "description": dynattrs.artifact_value(),
        "description_name": dynattrs.value(str),
        "directory": dynattrs.output(),
        "files": dynattrs.value(Artifact),
        "targets": dynattrs.value(Artifact),
    },
)

def _release_impl(ctx: AnalysisContext) -> list[Provider]:
    arch = architecture.spelling(ctx.attrs._arch, "systemd")
    files = ctx.attrs.files[DefaultInfo].default_outputs[0]
    description_name = "artifacts_{}.json".format(arch)
    description = files.project(description_name)
    targets = ctx.actions.declare_output("targets_{}.json".format(arch))
    args = cmd_args(
        box.run(box = ctx.attrs._box[box.Info], exe = ctx.attrs._driver),
        "--artifacts",
        description,
        "--files",
        files,
        "--output",
        targets.as_output(),
        "--hardhat",
        ctx.attrs.hardhat[DefaultInfo].default_outputs[0],
    )
    for selector in ctx.attrs.exclude:
        args.add("--exclude", selector)
    for directory in sorted(ctx.attrs.transfers):
        args.add("--transfer-dir", cmd_args(directory, ctx.attrs.transfers[directory], delimiter = "="))
    ctx.actions.run(args, category = "quarry_targets")

    directory = ctx.actions.declare_output("artifacts", dir = True)
    ctx.actions.dynamic_output_new(
        _assemble(
            description = description,
            description_name = description_name,
            directory = directory.as_output(),
            files = files,
            targets = targets,
        ),
    )
    return [DefaultInfo(default_output = directory)]

_release = rule(
    impl = _release_impl,
    attrs = {
        # A sparse raw disk is never meant to go over HTTP. Its qcow2 and raw.zst subtargets are.
        "exclude": attrs.list(
            attrs.string(),
            default = ["disk/raw"],
            doc = "files that the targets leave out, as `<kind>` or `<kind>/<subkind glob>` of the release's description",
        ),
        "files": attrs.dep(doc = "the image.artifacts folder that the targets describe"),
        # A plain dependency, so hardhat is built for the release of hardhat.box, whose libpathrs it binds.
        "hardhat": attrs.dep(default = "tine//quarry:hardhat", doc = "quarry's hardhat, which writes the targets"),
        "transfers": attrs.dict(
            attrs.string(),
            attrs.source(allow_directory = True),
            default = {},
            doc = "a component dir (sysupdate.d or sysupdate.<component>.d) to the source directory of its definitions",
        ),
        # The description and the targets are named after the architecture, as image.artifacts names them.
        "_arch": attrs.string(default = architecture.configured()),
        "_box": attrs.dep(providers = [box.Info], default = "tine//quarry:hardhat.box"),
        "_driver": attrs.exec_dep(default = "tine//quarry:targets"),
    },
    supports_incoming_transition = True,
)

def _release_macro(
    name: str,
    targets: list[str],
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select = [],
    **kwargs,
) -> None:
    """Gather the published artifacts of `targets` into a release folder, with the TUF targets that describe it.

    The folder without the targets is `<name>.files`.
    """
    files = name + ".files"
    image.artifacts(name = files, targets = targets, distro = distro, visibility = visibility)
    compatible_with = hardhat_compatible_with() + target_compatible_with
    _release(name = name, files = ":" + files, **(distribution.distributed(name, distro, visibility, compatible_with) | kwargs))

quarry = struct(
    compatible_with = hardhat_compatible_with,
    release = _release_macro,
)
