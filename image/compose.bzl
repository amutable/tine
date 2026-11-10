# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Convenience compositions for image products."""

load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//package:manager.bzl", "PackageManagerInfo")
load("//platform:architecture.bzl", "architecture")
load(":ami.bzl", "AMI_IMPORT_ATTR", "declare_ami")
load(
    ":archive.bzl",
    "ARCHIVE_ATTRS",
    "COMPRESSIONS",
    "ImageArchiveInfo",
    "declare_image_archive",
    "declare_image_directory",
)
load(
    ":boot.bzl",
    "UKI_ATTRS",
    "UkiProfile",  # @unused Used as a type.
    "boot_subtargets",
    "declare_boot_artifacts",
    "declare_uki",
    "encode_profiles",
    "uki_subtargets",
)
load(
    ":ddi.bzl",
    "DDI_ATTRS",
    "DISK_FORMATS",
    "Partition",  # @unused Used as a type.
    "SIZE_PATTERN",
    "VIRTUAL_SIZE_FORMATS",
    "declare_ddi",
    "declare_disk_conversion",
    "format_partition_labels",
    "manifest_sub_targets",
)
load(
    ":extension.bzl",
    "EXTENSION_ATTRS",
    "ExtensionInfo",
    "PORTABLE_ATTRS",
    "declare_extension",
    "declare_portable",
)
load(
    ":image.bzl",
    "IMAGE_ATTRS",
    "ImageInfo",
    "ImageToolsInfo",
    "LayerOperationTree",  # @unused Used as a type.
    "VERSION_PATTERN",
    "check_name",
    "declare_image",
    "flatten_operations",
    "generated",
    "image_providers",
    "initial_package_manager",
    "ops",
    "sign_systemd_boot",
)
load(":publish.bzl", "PublishedFile", "PublishedInfo", "published_metadata_subtargets")
load(
    ":sign.bzl",
    "resolve_signing_key",
)
load(":version.bzl", "resolve_version")

InitrdInfo = provider(
    doc = "A logical initrd image and its derived cpio archive.",
    fields = {
        "cpio": provider_field(ImageArchiveInfo),
        "image": provider_field(ImageInfo),
    },
)

# As of systemd 262, systemd-imds-early-network.service only writes a .network file, but doesn't start
# networkd. The IMDS generator pulls the unit in only on a cloud it detects in the hardware database, so
# networkd runs nowhere else, and `systemd.imds.network=off` opts out. mkosi does that via a preset call.
_IMDS_NETWORKD = ops.write_file(
    "/usr/lib/systemd/system/systemd-imds-early-network.service.d/tine-networkd.conf",
    "[Unit]\nWants=systemd-networkd.service\n",
)

# EBS volumes are NVMe devices whose I/O can stall for longer than the kernel's default 30 s timeout,
# which then fails the root file system. AWS recommends the maximum, see
# https://docs.aws.amazon.com/ebs/latest/userguide/nvme-ebs-volumes.html#timeout-nvme-ebs-volumes.
# This is the rule of amazon-ec2-utils, 70-ec2-nvme-devices.rules: unlike `nvme_core.io_timeout` on the
# kernel command line, it leaves the timeout of every other NVMe disk alone. The root volume keeps the
# timeout set here after switching root.
_EBS_IO_TIMEOUT = ops.write_file(
    "/usr/lib/udev/rules.d/70-ebs-io-timeout.rules",
    'KERNEL=="nvme[0-9]*n[0-9]*", ENV{DEVTYPE}=="disk", ATTRS{model}=="Amazon Elastic Block Store", ATTR{queue/io_timeout}="4294967295"\n',
)

# The portable profiles bind /etc/machine-id and /etc/resolv.conf from the host. systemd cannot create
# mount points on the read-only root partition, so the image must contain empty /etc/machine-id and
# /etc/resolv.conf files. The caller's operations run after _PORTABLE_MOUNT_POINTS, so an operation
# that writes /etc/machine-id or /etc/resolv.conf replaces the empty file.
_PORTABLE_MOUNT_POINTS = [
    ops.write_file("/etc/machine-id"),
    ops.write_file("/etc/resolv.conf"),
]

def _initrd_image_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    image = declare_image(
        ctx,
        box_dep = ctx.attrs._box,
        tools = tools,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(
            [
                ops.symlink("/usr/lib/systemd/systemd", "/init"),
                ops.symlink("/etc/os-release", "/etc/initrd-release"),
                _IMDS_NETWORKD,
                _EBS_IO_TIMEOUT,
            ]
            + ctx.attrs.ops,
            ["initrd"],
        )
        # The removals run after the generators, because the hwdb generator reads hwdb.d. At run time
        # udev reads only the compiled hardware database. Nothing in the initrd shows catalog
        # messages or resolves service names, so the initrd does not need the catalogs or
        # /etc/services.
        + [
            ops.remove("/usr/lib/udev/hwdb.d"),
            ops.remove("/usr/lib/systemd/catalog"),
            ops.remove("/var/lib/systemd/catalog"),
            ops.remove("/etc/services"),
        ],
        package_manager = ctx.attrs.package_manager,
        # Each release lists the packages of its "initrd" package set, so the initrd names the set
        # instead of packages.
        package_sets = ["initrd"] + ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    cpio = declare_image_archive(
        ctx,
        tools = tools,
        compression = ctx.attrs.compression,
        format = "cpio",
        image = image,
        strip_pkgdb = ctx.attrs.strip_pkgdb,
    )
    return image_providers(
        default_outputs = [cpio.archive],
        extra = [InitrdInfo(cpio = cpio, image = image), cpio],
        image = image,
    )

_initrd_image = rule(
    impl = _initrd_image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | {
        "compression": attrs.enum(COMPRESSIONS, default = "zstd"),
        # An initrd is small, short-lived, and read once, so the defaults an OS image wants are the
        # wrong way round here: documentation and translations only cost boot memory.
        "install_docs": attrs.bool(default = False),
        "install_langs": attrs.list(attrs.string(), default = ["C.UTF-8"]),
        "package_manager": attrs.dep(providers = [PackageManagerInfo]),
        "strip_pkgdb": attrs.bool(
            default = True,
            doc = "leave the package database out of the cpio; the image still carries it",
        ),
    },
)

def initrd_image(
    name: str,
    ops: list[LayerOperationTree] = [],
    package_manager: str | Select | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare the conventional initrd, extended by `packages`, `package_sets`, and `ops`.

    The initrd installs the release's `initrd` package set and runs the operations every initrd
    needs first. The packages and operations of the target are added after them. A bootable `disk`
    declares an initrd with `initrd_image` when it is given no `initrd`.
    """
    _initrd_image(
        name = name,
        ops = flatten_operations(ops),
        package_manager = initial_package_manager(package_manager, None, distro),
        version = resolve_version("initrd_image {}".format(name), version),
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

_ROOT_ATTRS = {
    "package_manager": attrs.option(
        attrs.dep(providers = [PackageManagerInfo]),
        default = None,
    ),
    "parent": attrs.option(
        attrs.dep(providers = [ImageInfo]),
        default = None,
        doc = "the logical image to build on instead of starting one of its own",
    ),
}

def _archive_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    image = declare_image(
        ctx,
        box_dep = ctx.attrs._box,
        tools = tools,
        identifier = "image",
        package_manager = ctx.attrs.package_manager,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(ctx.attrs.ops, ctx.attrs.packages + ctx.attrs.package_sets),
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    archive = declare_image_archive(
        ctx,
        tools = tools,
        compression = ctx.attrs.compression,
        format = ctx.attrs.format,
        image = image,
    )
    return image_providers(
        default_outputs = [archive.archive],
        extra = [archive],
        image = image,
    )

_archive = rule(
    impl = _archive_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | ARCHIVE_ATTRS | _ROOT_ATTRS,
)

def _extension_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    # An extension runs no generators. A database generated from the full stack would replace the
    # database of the parent in the merged tree. It would also be out of date when the parent changes.
    parent = None
    if ctx.attrs.parent != None:
        # An extension of an extension contains only its own delta. It works only when the
        # extension below it is merged too, and neither DDI records that dependency.
        if ctx.attrs.parent.get(ExtensionInfo) != None:
            fail("{}: parent {} is an extension itself; extend the image it extends".format(ctx.label.name, ctx.attrs.parent.label))
        parent = ctx.attrs.parent[ImageInfo]
    image = declare_image(
        ctx,
        box_dep = ctx.attrs._box,
        tools = tools,
        identifier = "image",
        package_manager = ctx.attrs.package_manager,
        parent = parent,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return declare_extension(
        ctx,
        tools = tools,
        arch = ctx.attrs.arch,
        parent = parent,
        classes = ctx.attrs.classes,
        image = image,
        name = ctx.label.name,
        release = ctx.attrs.release,
        scope = ctx.attrs.scope,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )

_extension = rule(
    impl = _extension_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | EXTENSION_ATTRS | _ROOT_ATTRS,
)

def _portable_impl(ctx: AnalysisContext) -> list[Provider]:
    image = declare_image(
        ctx,
        box_dep = ctx.attrs._box,
        tools = ctx.attrs._tools[ImageToolsInfo],
        identifier = "image",
        package_manager = ctx.attrs.package_manager,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = _PORTABLE_MOUNT_POINTS + generated(ctx.attrs.ops, ctx.attrs.packages + ctx.attrs.package_sets),
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return declare_portable(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        arch = ctx.attrs.arch,
        image = image,
        name = ctx.label.name,
        seed = ctx.attrs.seed,
        verity_key = resolve_signing_key(ctx.attrs.verity_key),
        version = ctx.attrs.version,
    )

_portable = rule(
    impl = _portable_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS | PORTABLE_ATTRS | _ROOT_ATTRS,
)

def _disk_impl(ctx: AnalysisContext) -> list[Provider]:
    tools = ctx.attrs._tools[ImageToolsInfo]
    bootable = ctx.attrs._bootable
    image_id = check_name(
        "disk image_id",
        ctx.attrs.image_id or ctx.label.name,
    )

    # The version lands in partition labels and the UKI filename.
    version = check_name("disk version", ctx.attrs.version, VERSION_PATTERN)

    secure_boot_key = resolve_signing_key(ctx.attrs.secure_boot_key) if bootable else None

    root = declare_image(
        ctx,
        box_dep = ctx.attrs._box,
        tools = tools,
        identifier = "root",
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = generated(ctx.attrs.ops, ctx.attrs.packages + ctx.attrs.package_sets),
        package_manager = ctx.attrs.package_manager,
        package_sets = ctx.attrs.package_sets,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        packages = ctx.attrs.packages,
        tmpfiles = ctx.attrs.tmpfiles,
        version = version,
    )

    # The identity stamp lives in its own thin layer so that a changing version only re-runs the
    # artifacts that embed it, never package installation or the caller's operations. systemd-boot
    # signing joins it for the same reason (a key change must not re-run the caller's operations):
    # it must precede the verity partitions below so that the booted /usr carries the signed
    # binary (see sign_systemd_boot).
    identity_ops = [ops.merge_os_release({"IMAGE_ID": image_id, "IMAGE_VERSION": version})]
    if secure_boot_key != None:
        identity_ops += sign_systemd_boot(secure_boot_key, ctx.attrs.arch)
    identity = declare_image(
        ctx,
        tools = tools,
        identifier = "identity",
        keys = [secure_boot_key],
        ops = identity_ops,
        parent = root,
        version = version,
    )

    definitions = format_partition_labels(
        [json.decode(value) for value in ctx.attrs.definitions],
        image_id,
        version,
    )

    # An option set for a filesystem this disk never formats would go nowhere: repart reads the
    # variable named after the filesystem it is about to create and ignores the rest.
    formatted = {definition["filesystem"]: True for definition in definitions if definition["filesystem"]}
    unknown = [name for name in sorted(ctx.attrs.mkfs_options) if name not in formatted]
    if unknown:
        fail(
            "disk: mkfs_options for {}, which no partition of this disk formats; it formats {}".format(
                unknown,
                sorted(formatted),
            )
        )

    # Everything this composition publishes is named after the image identity: the disk, the UKI,
    # and every split partition, which repart names after the image it writes them out of.
    basename = "{}_{}_{}".format(image_id, version, architecture.ARCHITECTURES[ctx.attrs.arch].systemd)

    if bootable:
        # The disk() macro makes a disk bootable only when its definitions contain an ESP partition.
        system_definitions = [definition for definition in definitions if definition["type"] != "esp"]
        boot_definitions = [definition for definition in definitions if definition["type"] == "esp"]
        if not system_definitions:
            fail("disk: a bootable disk needs system partitions besides the ESP")
        initrd_info = ctx.attrs.initrd[InitrdInfo]

        # The UKI contains the verity root hash of the system partitions, and the ESP contains the
        # UKI. repart therefore writes the system partitions first, and copies them into the disk
        # after the ESP is built.
        system = declare_ddi(
            ctx,
            tools = tools,
            systemd_arch = architecture.ARCHITECTURES[ctx.attrs.arch].systemd,
            basename = basename,
            definitions = system_definitions,
            disk = False,
            # repart leaves the directories that the ESP copies out of a root partition that copies
            # "/", but only when the ESP is in the same run. The ESP is in a run of its own, so this
            # run leaves them out itself. Otherwise the files that ops copy to /boot are in both
            # partitions.
            exclude = [entry.split(":")[0] for definition in boot_definitions for entry in definition["copy_files"]],
            identifier = "system",
            image = identity,
            mkfs_options = ctx.attrs.mkfs_options,
            seed = ctx.attrs.disk_seed,
            split = True,
            strip_paths = ctx.attrs.strip_paths,
            strip_pkgdb = ctx.attrs.strip_pkgdb,
            verity_key = resolve_signing_key(ctx.attrs.verity_key),
        )
        uki = declare_uki(
            ctx,
            tools = tools,
            arch = ctx.attrs.arch,
            cmdline = ctx.attrs.cmdline,
            identifier = "uki",
            image = identity,
            image_id = image_id,
            initrd_modules = ctx.attrs.initrd_modules,
            initrds = [initrd_info.cpio],
            profiles = ctx.attrs.profiles,
            root_hash = system.info.root_hash,
            secure_boot_key = secure_boot_key,
            sign_expected_pcr_key = resolve_signing_key(ctx.attrs.sign_expected_pcr_key),
            splash = ctx.attrs.splash,
            version = version,
        )
        top = declare_image(
            ctx,
            tools = tools,
            identifier = "esp",
            keys = [secure_boot_key],
            ops = [ops.copy(uki.ukis, "/boot/EFI/Linux")] + ops.install_systemd_boot(secure_boot_key),
            parent = identity,
            version = version,
        )
        composed = declare_ddi(
            ctx,
            tools = tools,
            systemd_arch = architecture.ARCHITECTURES[ctx.attrs.arch].systemd,
            basename = basename,
            definitions = boot_definitions,
            identifier = "disk",
            image = top,
            imported = [system.info],
            mkfs_options = ctx.attrs.mkfs_options,
            seed = ctx.attrs.disk_seed,
            virtual_size = ctx.attrs.virtual_size,
        )

        # repart lists the files of the system partitions in the run that writes them. The disk
        # manifest therefore combines the manifests of both runs.
        manifests = system.info.manifests | composed.info.manifests

        # What this disk boots, as files rather than as PE sections: a direct kernel boot needs the
        # kernel and the initrd the UKI carries, and publishing needs the one UKI out of the set the
        # image ships. Declared here so a caller does not have to point a second target at the ESP.
        boot = declare_boot_artifacts(ctx, tools = tools, identifier = "boot", image = top)
        boot_sub_targets = {
            "boot": [
                DefaultInfo(
                    default_outputs = [boot.kernel, boot.initrd],
                    sub_targets = boot_subtargets(boot),
                ),
            ],
            "initrd": [
                DefaultInfo(
                    default_output = initrd_info.cpio.archive,
                    sub_targets = published_metadata_subtargets(initrd_info.image, "{}.initrd".format(basename), version),
                ),
                initrd_info,
            ],
            "uki": [DefaultInfo(default_output = uki.ukis, sub_targets = uki_subtargets(uki)), uki],
        }
        boot_artifacts = {
            "{}.efi".format(basename): PublishedFile(artifact = boot.uki, kind = "uki"),
            "{}.initrd".format(basename): PublishedFile(artifact = boot.initrd, kind = "initrd"),
            "{}.vmlinuz".format(basename): PublishedFile(artifact = boot.kernel, kind = "kernel"),
        }
        boot_providers = [initrd_info, uki]
    else:
        top = identity
        composed = declare_ddi(
            ctx,
            tools = tools,
            systemd_arch = architecture.ARCHITECTURES[ctx.attrs.arch].systemd,
            basename = basename,
            definitions = definitions,
            identifier = "disk",
            image = top,
            mkfs_options = ctx.attrs.mkfs_options,
            seed = ctx.attrs.disk_seed,
            split = True,
            strip_paths = ctx.attrs.strip_paths,
            strip_pkgdb = ctx.attrs.strip_pkgdb,
            verity_key = resolve_signing_key(ctx.attrs.verity_key),
            virtual_size = ctx.attrs.virtual_size,
        )
        manifests = composed.info.manifests
        boot_sub_targets = {}
        boot_artifacts = {}
        boot_providers = []

    raw = composed.info.disk
    directory = declare_image_directory(ctx, tools = tools, identifier = "directory", image = top)
    if ctx.attrs.virtual_size != None and not regex_match(SIZE_PATTERN, ctx.attrs.virtual_size):
        fail("disk: invalid virtual_size {!r}".format(ctx.attrs.virtual_size))
    conversions = [
        declare_disk_conversion(
            ctx,
            tools = tools,
            basename = basename,
            disk = composed.info,
            box_dep = top.box,
            format = format,
            virtual_size = ctx.attrs.virtual_size if format in VIRTUAL_SIZE_FORMATS else None,
        )
        for format in DISK_FORMATS
    ]

    sub_targets = dict(composed.sub_targets)
    sub_targets.update(manifest_sub_targets(manifests))
    sub_targets.update(boot_sub_targets)
    sub_targets["directory"] = [DefaultInfo(default_output = directory.directory), directory]
    if bootable:
        sub_targets["ami"] = declare_ami(
            ctx.actions,
            arch = ctx.attrs.arch,
            basename = basename,
            box_info = ctx.attrs._ami_box[box.Info],
            disk = raw,
            importer = ctx.attrs._ami_import[RunInfo],
            # A key held by a token has a URI, not a file, for a certificate; every key rule publishes the
            # file as `[cert]`.
            secure_boot_certificate = (
                ctx.attrs.secure_boot_key[DefaultInfo].sub_targets["cert"][DefaultInfo].default_outputs[0] if secure_boot_key != None else None
            ),
            version = version,
            virtual_size = ctx.attrs.virtual_size,
        )
    for conversion in conversions:
        sub_targets[conversion.format] = [
            DefaultInfo(default_output = conversion.image),
            # A re-encoding is publishable in its own right, so a release can gather the subtarget
            # rather than the disk; it is not in the disk's own set, which would build every
            # encoding whenever anything materializes the release.
            PublishedInfo(
                artifacts = {conversion.image.basename: PublishedFile(artifact = conversion.image, kind = "disk", subkind = conversion.format)},
                image = basename,
                version = version,
            ),
            conversion,
        ]

    # An update carries the partitions and the UKI; the disk itself is the installation medium, and
    # the kernel and initrd are what boots one without a boot loader. The ESP is none of those: what
    # it holds arrives as transfers of its own, so it is left out rather than published as a
    # partition nothing ever writes back. The listings join them: one per partition, naming what
    # that partition puts on a machine and nothing the image happens to hold besides, and one for
    # the disk, which is what an installation of it leaves behind.
    artifacts = {raw.basename: PublishedFile(artifact = raw, kind = "disk", subkind = "raw")} | boot_artifacts
    for name, listing in manifests.items():
        artifacts["{}.{}.Uapi16Manifest".format(basename, name)] = PublishedFile(artifact = listing, kind = "listing", subkind = "uapi16manifest")
    whole = composed.info.manifest
    if whole != None:
        artifacts[whole.basename] = PublishedFile(artifact = whole, kind = "listing", subkind = "uapi16manifest")

    published = PublishedInfo(
        artifacts = artifacts,
        image = basename,
        version = version,
        partitions = [partition for partition in composed.info.partitions if partition.definition["type"] != "esp"],
    )

    return image_providers(
        default_outputs = [raw],
        extra = [directory, composed.info, published] + boot_providers,
        image = top,
        sub_targets = sub_targets | published_metadata_subtargets(top, basename, version),
    )

_DISK_ATTRS = (
    IMAGE_ATTRS
    | DDI_ATTRS
    | _ROOT_ATTRS
    | {
        "arch": UKI_ATTRS["arch"],
        "disk_seed": attrs.option(attrs.string(), default = None),
        "image_id": attrs.option(attrs.string(), default = None),
        "virtual_size": attrs.option(
            attrs.string(),
            default = None,
            doc = 'size of the disk a qcow2 or vmdk describes, e.g. "20G"; the room past the partitions stays free',
        ),
    }
)

# Only a bootable disk has the attributes in _BOOTABLE_ATTRS. Buck refuses them on a disk that
# doesn't boot.
_BOOTABLE_ATTRS = (
    UKI_ATTRS
    | {
        "cmdline": attrs.list(
            attrs.string(),
            default = ["root=tmpfs", "mount.usr=dissect", "rw"],
        ),
        "initrd": attrs.dep(
            providers = [InitrdInfo],
            doc = "the initrd to boot; the declaration macro declares a conventional one when given none",
        ),
        "_ami_box": attrs.dep(providers = [box.Info], default = "tine//tools:aws.box"),
    }
    | AMI_IMPORT_ATTR
)

_disk = rule(
    impl = _disk_impl,
    supports_incoming_transition = True,
    attrs = _DISK_ATTRS | {"_bootable": attrs.bool(default = False)},
)

_bootable_disk = rule(
    impl = _disk_impl,
    supports_incoming_transition = True,
    attrs = _DISK_ATTRS | _BOOTABLE_ATTRS | {"_bootable": attrs.bool(default = True)},
)

def archive(
    name: str,
    ops: list[LayerOperationTree] = [],
    package_manager: str | Select | None = None,
    parent: str | Select | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and emit it as an archive."""
    _archive(
        name = name,
        ops = flatten_operations(ops),
        package_manager = initial_package_manager(package_manager, parent, distro),
        parent = parent,
        version = resolve_version("archive {}".format(name), version),
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

def extension(
    name: str,
    ops: list[LayerOperationTree] = [],
    package_manager: str | Select | None = None,
    parent: str | Select | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and package it as an extension DDI.

    `classes` lists the extension classes the DDI contains: "sysext" for /usr and /opt, "confext"
    for /etc. With `parent`, the DDI contains only the delta above that image.
    """
    _extension(
        name = name,
        ops = flatten_operations(ops),
        package_manager = initial_package_manager(package_manager, parent, distro),
        parent = parent,
        version = resolve_version("extension {}".format(name), version),
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

def sysext(name: str, **kwargs) -> None:
    """Build a system extension: an extension DDI that contains /usr and /opt."""
    extension(name = name, classes = ["sysext"], **kwargs)

def confext(name: str, **kwargs) -> None:
    """Build a configuration extension: an extension DDI that contains /etc."""
    extension(name = name, classes = ["confext"], **kwargs)

def portable(
    name: str,
    ops: list[LayerOperationTree] = [],
    package_manager: str | Select | None = None,
    parent: str | Select | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build one logical image from operations and package it as a portable service DDI."""
    _portable(
        name = name,
        ops = flatten_operations(ops),
        package_manager = initial_package_manager(package_manager, parent, distro),
        parent = parent,
        version = resolve_version("portable {}".format(name), version),
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

def disk(
    name: str,
    definitions: list[Partition],
    bootable: bool | None = None,
    ops: list[LayerOperationTree] = [],
    profiles: list[UkiProfile] = [],
    initrd: str | Select | None = None,
    package_manager: str | Select | None = None,
    parent: str | Select | None = None,
    image_id: str | None = None,
    version: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Compose partitions into a disk, which boots when `bootable` is set.

    `bootable` defaults to whether `definitions` contain an ESP partition. Without `initrd`, a
    bootable disk declares a conventional `initrd_image()` as `<name>.initrd`. A bootable disk with
    `parent` must be given an `initrd`, because the conventional initrd needs a package manager.
    """
    esp = [definition for definition in definitions if definition["type"] == "esp"]
    if bootable == None:
        bootable = bool(esp)
    elif bootable and not esp:
        fail("disk {}: a bootable disk needs an ESP partition in definitions".format(name))
    if not bootable and (profiles or initrd != None):
        fail("disk {}: profiles and initrd apply only to a bootable disk".format(name))

    # Resolved once, against the labels this disk renders, and passed on: the initrd below carries
    # the version of the disk it boots rather than one rendered against its own empty budget.
    version = resolve_version(
        "disk {}".format(name),
        version,
        labels = [d["label"] for d in definitions if d["label"] != None],
        image_id = image_id or name,
    )
    package_manager = initial_package_manager(package_manager, parent, distro)
    common = dict(
        name = name,
        image_id = image_id,
        # The rule renders label placeholders from its own identity during analysis.
        definitions = [json.encode(definition) for definition in definitions],
        ops = flatten_operations(ops),
        package_manager = package_manager,
        parent = parent,
        version = version,
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )
    if not bootable:
        _disk(**common)
        return

    if initrd == None:
        if package_manager == None:
            fail(
                "disk {}: declare an `initrd_image()` and pass it as `initrd`; ".format(name)
                + "a disk extending `parent`, or in a package that declares no distribution, has no package manager to declare one from",
            )
        initrd = ":{}.initrd".format(name)
        initrd_image(
            name = name + ".initrd",
            package_manager = package_manager,
            version = version,
            visibility = visibility,
            # The image naming a distribution of its own makes its initrd that distribution too;
            # otherwise the leaf pulling it in decides, exactly as a parent chain does.
            distro = distro,
        )
    _bootable_disk(initrd = initrd, profiles = encode_profiles(profiles), **common)
