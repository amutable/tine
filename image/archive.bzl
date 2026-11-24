# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Archive and snapshot image outputs."""

load("//distribution:defs.bzl", "distribution")
load(
    ":image.bzl",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "declare_out",
    "declare_stripped",
    "terminal_image_command",
)

COMPRESSIONS = ["none", "zstd"]
_COMPRESSION_EXT = {"none": "", "zstd": ".zst"}

ImageArchiveInfo = provider(
    doc = "A deterministic tar or newc cpio archive of a logical image, compressed as declared when compression is on.",
    fields = {
        "archive": provider_field(Artifact),
        "format": provider_field(str),
        "manifest": provider_field(Artifact),
    },
)

ImageSnapshotInfo = provider(
    doc = "The stored snapshot of a logical image, which `buck run` exports as a directory.",
    fields = {
        "store": provider_field(Artifact),
    },
)

def declare_image_archive(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    format: str,
    compression: str,
    strip_pkgdb: bool = False,
    identifier: str | None = None,
) -> ImageArchiveInfo:
    """Declare an archive action from a resolved logical image.

    With strip_pkgdb, the archive omits the package database wherever the image's package system
    keeps it, for a root nothing ever resolves packages in. The image's `[pkgdb]` subtarget still
    captures the database from the tree itself.
    """
    image = declare_stripped(ctx, tools = tools, image = image, name = "strip-" + format, identifier = identifier, strip_pkgdb = strip_pkgdb)
    out = declare_out(ctx, identifier, "image." + format + _COMPRESSION_EXT[compression])
    listing = declare_out(ctx, identifier, "image." + format + ".Uapi16Manifest")
    cmd = terminal_image_command(
        ctx,
        # One image can be archived in several formats, so the spec is named like the output.
        driver = "archive-" + format,
        exe = tools.archive,
        identifier = identifier,
        image = image,
        spec = {
            "compression": compression,
            "format": format,
            "manifest": listing.as_output(),
            "out": out.as_output(),
        },
    )
    ctx.actions.run(cmd, category = "image_" + format, identifier = identifier or format)
    return ImageArchiveInfo(archive = out, format = format, manifest = listing)

ARCHIVE_ATTRS = {
    "compression": attrs.enum(COMPRESSIONS, default = "none"),
    "format": attrs.enum(["tar", "cpio"], default = "tar"),
}

def declare_image_snapshot(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    identifier: str | None = None,
) -> ImageSnapshotInfo:
    """Declare the stored snapshot of a resolved logical image."""
    out = declare_out(ctx, identifier, "image.snapshot", dir = True)
    cmd = terminal_image_command(
        ctx,
        driver = "snapshot",
        exe = tools.archive,
        identifier = identifier,
        image = image,
        spec = {
            "compression": "none",
            "format": "snapshot",
            "manifest": None,
            "out": out.as_output(),
        },
    )
    ctx.actions.run(cmd, category = "image_snapshot", identifier = identifier or "snapshot")
    return ImageSnapshotInfo(store = out)

def image_snapshot_providers(tools: ImageToolsInfo, info: ImageSnapshotInfo) -> list[Provider]:
    """Return the providers of a `[snapshot]` target or sub-target for `info`, which export it when run."""
    export = RunInfo(args = cmd_args(tools.archive[RunInfo], "export", "--store", info.store))
    return [DefaultInfo(default_output = info.store), export, info]

def _image_snapshot_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    info = declare_image_snapshot(ctx, tools = tools, image = ctx.attrs.image[ImageInfo])
    return image_snapshot_providers(tools, info)

_image_snapshot = rule(
    impl = _image_snapshot_impl,
    attrs = {
        "image": attrs.dep(providers = [ImageInfo], doc = "the logical image to store"),
    }
    | IMAGE_TOOLS_ATTR,
    supports_incoming_transition = True,
)

def image_snapshot(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _image_snapshot(name = name, **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs))
