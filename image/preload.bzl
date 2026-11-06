# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""A built disk with preloaded extensions and portable images."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load(":ami.bzl", "AMI_IMPORT_ATTR", "declare_ami_from_base")
load(":ddi.bzl", "DISK_FORMATS", "DdiInfo", "DiskConversionInfo", "SIZE_PATTERN", "VIRTUAL_SIZE_FORMATS")
load(":extension.bzl", "ExtensionInfo", "check_portable", "ddi_image_name", "system_extension")
load(":image.bzl", "IMAGE_TOOLS_ATTR", "ImageToolsInfo", "spec_path")

def _esp_path(directory: str, disk: Artifact) -> str:
    """Where `disk` goes on the ESP: <name>_<version>_<arch>.raw in <name>.raw.v below `directory`.

    That is how systemd-sysupdate and systemd-vpick lay out the versions of one image. vpick reads the
    version and architecture from what sits between the image name and ".raw", so drop the DDI's class
    suffix. The directory gives systemd the image name.
    """
    stem = disk.basename.removesuffix(".raw").removesuffix(".sysext").removesuffix(".confext")
    return "/{}/{}.raw.v/{}.raw".format(directory, ddi_image_name(disk), stem)

def _ddi_preloaded_disk_impl(ctx: AnalysisContext) -> list[Provider]:
    base_info = ctx.attrs.base[DdiInfo]
    base = base_info.disk
    if base == None:
        fail("ddi_preloaded_disk: {} has no composed disk".format(ctx.attrs.base.label))
    if ctx.attrs.virtual_size != None and not regex_match(SIZE_PATTERN, ctx.attrs.virtual_size):
        fail("ddi_preloaded_disk: invalid virtual_size {!r}".format(ctx.attrs.virtual_size))

    # First boot of the base claims the room past its partitions, so the preloaded disk needs as much.
    virtual_size = ctx.attrs.virtual_size or base_info.virtual_size

    sources = []
    for dep in ctx.attrs.extensions:
        system_extension("ddi_preloaded_disk", dep)
        sources.append(("extensions", dep[DdiInfo].disk))
    for dep in ctx.attrs.portables:
        check_portable("ddi_preloaded_disk", dep)
        sources.append(("portables", dep[DdiInfo].disk))
    files = {}
    for directory, disk in sources:
        path = _esp_path(directory, disk)
        if path in files:
            fail("ddi_preloaded_disk: two DDIs go to {}".format(path))
        files[path] = disk

    # Unlike image.disk, this has no raw, and each format is a run of the driver of its own, which edits
    # the ESP and converts in one action. A base can be a disk with gigabytes of holes, such as an
    # imported release, and Buck would hash and cache a raw of it in full.
    disks = {}
    for format in DISK_FORMATS:
        out = ctx.actions.declare_output("{}.{}".format(ctx.label.name, format))
        ctx.actions.run(
            cmd_args(
                box.run(box = ctx.attrs._box[box.Info], exe = ctx.attrs._tools[ImageToolsInfo].preload),
                specs.args(
                    ctx.actions,
                    spec_path(format, "preload"),
                    {
                        "base": base,
                        "files": files,
                        "format": format,
                        "out": out.as_output(),
                        "virtual_size": virtual_size if format in VIRTUAL_SIZE_FORMATS else None,
                    },
                ),
            ),
            category = "preload",
            identifier = format,
        )
        disks[format] = out
    sub_targets = {format: [DefaultInfo(default_output = out), DiskConversionInfo(format = format, image = out)] for format, out in disks.items()}

    # A base that registers as an AMI has `[ami]`, as a bootable image.disk does.
    base_ami = ctx.attrs.base[DefaultInfo].sub_targets.get("ami")
    if base_ami != None:
        sub_targets["ami"] = declare_ami_from_base(
            ctx.actions,
            base_parameters = base_ami[DefaultInfo].default_outputs[0],
            basename = ctx.label.name,
            disk = disks["raw.zst"],
            importer = ctx.attrs._ami_import[RunInfo],
            virtual_size = ctx.attrs.virtual_size,
        )
    return [
        DefaultInfo(default_output = disks["raw.zst"], sub_targets = sub_targets),
        DdiInfo(disk = disks["raw.zst"], partitions = [], virtual_size = virtual_size),
    ]

_ddi_preloaded_disk = rule(
    impl = _ddi_preloaded_disk_impl,
    attrs = {
        "base": attrs.dep(providers = [DdiInfo], doc = "the disk, raw or zstd-compressed, whose ESP gets the DDIs"),
        "extensions": attrs.list(
            attrs.dep(providers = [DdiInfo, ExtensionInfo]),
            default = [],
            doc = "system extensions, put on the ESP below /extensions",
        ),
        "portables": attrs.list(
            attrs.dep(providers = [DdiInfo]),
            default = [],
            doc = "portable service images and their extensions, put on the ESP below /portables",
        ),
        "virtual_size": attrs.option(
            attrs.string(),
            default = None,
            doc = "size of the disk a qcow2 or vmdk describes, and of the AMI's root volume, e.g. \"20G\", instead of the base's",
        ),
        # The tools box of the release has the tools that read the partition table, write the FAT and
        # convert the disk.
        "_box": distribution.TOOLS_BOX_ATTR,
    }
    | AMI_IMPORT_ATTR
    | IMAGE_TOOLS_ATTR,
    supports_incoming_transition = True,
)

def ddi_preloaded_disk(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare one, compatible with the distributions its package serves."""
    _ddi_preloaded_disk(name = name, **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs))
