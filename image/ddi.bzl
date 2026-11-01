# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a DDI, a GPT disk image, from partition definitions with systemd-repart."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load(
    ":image.bzl",
    "GPT_LABEL_LIMIT",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "declare_out",
    "pkgdb_paths",
    "spec_path",
    "terminal_image_command",
)
load(
    ":sign.bzl",
    "SigningKeyInfo",
    "VERITY_KEY_ATTR",
    "external_signing_execution",
    "merge_signing_access",
    "signing_key_spec",
)

Partition = dict[str, typing.Any]

# A byte count with an optional systemd size suffix, which every consumer reads base-1024.
SIZE_PATTERN = "^[0-9]+(\\.[0-9]+)?[KMGTPE]?$"

def partition(
    type: str,
    name: str | None = None,
    label: str | None = None,
    filesystem: str | None = None,
    copy_files: list[str] = [],
    size_min: str | int | None = None,
    size_max: str | int | None = None,
    minimize: str | None = None,
    compression: str | None = None,
    verity: str | None = None,
    verity_match_key: str | None = None,
) -> Partition:
    """Describe one partition without exposing a repart definition file."""
    if not type:
        fail("partition: type cannot be empty")
    if name == None:
        name = type
    check_name("partition artifact name", name)
    if label != None and len(label) > 36:
        fail("partition: label {!r} exceeds GPT's limit of 36 characters".format(label))
    if minimize not in (None, "off", "best", "guess"):
        fail("partition: invalid minimize value {!r}".format(minimize))
    if verity not in (None, "data", "hash", "signature"):
        fail("partition: invalid verity value {!r}".format(verity))
    if (verity == None) != (verity_match_key == None):
        fail("partition: verity and verity_match_key must be specified together")
    for copy in copy_files:
        if not copy:
            fail("partition: copy_files entries cannot be empty")

    # The driver derives partition UUIDs from these bytes, so the key order is part of the
    # contract: changing it re-identifies every partition of every unchanged image.
    # @unsorted-dict-items
    return {
        "name": name,
        "type": type,
        "label": label,
        "filesystem": filesystem,
        "copy_files": copy_files,
        "size_min": size_min,
        "size_max": size_max,
        "minimize": minimize,
        "compression": compression,
        "verity": verity,
        "verity_match_key": verity_match_key,
    }

_ESP_PARTITION = partition(
    type = "esp",
    filesystem = "vfat",
    copy_files = ["/boot:/", "/efi:/"],
    size_min = "512M",
    size_max = "512M",
)

DEFAULT_ROOT_PARTITIONS = [
    _ESP_PARTITION,
    partition(
        type = "root",
        filesystem = "ext4",
        copy_files = ["/"],
        minimize = "guess",
    ),
]

def verity_partitions(data: Partition, signed: bool) -> list[Partition]:
    """Return `data`, its verity hash partition and, with `signed`, its verity signature partition.

    The labels of the hash and signature partitions are the label of `data` with a suffix.
    """
    kind = data["type"]
    label = data["label"]
    definitions = [
        data,
        partition(
            type = kind + "-verity",
            label = label + "_verity" if label != None else None,
            minimize = "best",
            verity = "hash",
            verity_match_key = kind,
        ),
    ]
    if signed:
        definitions.append(
            partition(
                type = kind + "-verity-sig",
                label = label + "_verity_sig" if label != None else None,
                verity = "signature",
                verity_match_key = kind,
            ),
        )
    return definitions

# systemd-sysupdate matches A/B partitions by their exact label, so the labels of the default layouts
# contain the image ID and version as placeholders. `disk()` fills them in from its image_id
# and version attributes.
def _usr_verity_partitions(signed: bool) -> list[Partition]:
    usr = partition(
        type = "usr",
        label = "{image_id}_{version}",
        filesystem = "erofs",
        copy_files = ["/usr:/"],
        minimize = "best",
        compression = "zstd",
        verity = "data",
        verity_match_key = "usr",
    )
    return verity_partitions(usr, signed) + [_ESP_PARTITION]

DEFAULT_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = False)
DEFAULT_SIGNED_USR_VERITY_PARTITIONS = _usr_verity_partitions(signed = True)

def format_partition_labels(definitions: list[Partition], image_id: str, version: str) -> list[Partition]:
    """Render {image_id} and {version} placeholders in partition labels."""
    formatted = []
    for definition in definitions:
        label = definition["label"]
        if label == None or "{" not in label:
            formatted.append(definition)
            continue
        formatted.append(
            definition
            | {
                "label": label.format(image_id = image_id, version = version),
            }
        )

    # Rendering can only lengthen a label, so re-check GPT's limit on the final value.
    for definition in formatted:
        if definition["label"] != None and len(definition["label"]) > GPT_LABEL_LIMIT:
            fail(
                "partition: label {!r} exceeds GPT's limit of {} characters".format(
                    definition["label"],
                    GPT_LABEL_LIMIT,
                )
            )
    return formatted

def _check_definitions(
    definitions: list[Partition],
    *,
    imports: bool,
    disk: bool,
    split: bool,
    signed: bool,
) -> None:
    if not definitions and (split or not imports):
        fail("ddi: definitions may be empty only when assembling partition inputs")
    if not disk and not split:
        fail("ddi: at least one of disk or split must be enabled")
    for definition in definitions:
        label = definition["label"]
        if label != None and "{" in label:
            fail("ddi: unrendered placeholder in label {!r}; render with format_partition_labels()".format(label))
    signatures = [it for it in definitions if it["verity"] == "signature"]
    if bool(signatures) != signed:
        fail("ddi: Verity=signature and signing credentials must be specified together")

PartitionInfo = record(
    definition = Partition,
    blocks = Artifact,
    metadata = Artifact,
    # The manifest lists the files of the partition. It is None for a partition that copies no
    # files. The invocation that wrote an imported partition also wrote its manifest, and the
    # invocation that copies the partition in uses that manifest to list the whole disk.
    manifest = Artifact | None,
)

RootHashInfo = provider(
    doc = "A dm-verity root hash generated while creating independent partitions.",
    fields = {
        "hash": provider_field(Artifact),
        "kind": provider_field(str),
    },
)

DdiInfo = provider(
    doc = "A DDI: an optional composed disk and any independent partition artifacts.",
    fields = {
        "disk": provider_field(Artifact | None, default = None),
        # What the composed disk carries, its own partitions and its imported ones taken together.
        "manifest": provider_field(Artifact | None, default = None),
        # manifests maps each partition that this invocation writes to the manifest of its files.
        # Imported partitions are in `partitions`, with the manifest of the invocation that wrote them.
        "manifests": provider_field(dict[str, Artifact], default = {}),
        "partitions": provider_field(list[PartitionInfo]),
        "root_hash": provider_field(RootHashInfo | None, default = None),
    },
)

DdiOutput = record(
    info = DdiInfo,
    outputs = list[Artifact],
    sub_targets = dict[str, list[Provider]],
)

def _verity_kind(definitions: list[Partition]) -> str | None:
    kind = None
    for definition in definitions:
        if definition["verity"] != "data":
            continue
        if kind != None:
            fail("ddi: one invocation cannot produce more than one verity root hash")
        if definition["type"].startswith("usr"):
            kind = "usr"
        elif definition["type"].startswith("root"):
            kind = "root"
        else:
            fail("ddi: Verity=data requires a usr or root partition type")
    return kind

def _partition_sub_targets(partitions: list[PartitionInfo]) -> dict[str, list[Provider]]:
    return {
        partition.definition["name"]: [
            DefaultInfo(default_output = partition.blocks, other_outputs = [partition.metadata]),
        ]
        for partition in partitions
    }

def _output(artifact: Artifact | None) -> OutputArtifact | None:
    return artifact.as_output() if artifact != None else None

def manifest_sub_targets(manifests: dict[str, Artifact]) -> dict[str, list[Provider]]:
    """What each partition carries, under the name of the definition that carries it."""
    if not manifests:
        return {}
    return {
        "manifests": [
            DefaultInfo(
                default_outputs = manifests.values(),
                sub_targets = {name: [DefaultInfo(default_output = out)] for name, out in manifests.items()},
            ),
        ],
    }

def declare_ddi(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    systemd_arch: str,
    definitions: list[Partition],
    disk: bool = True,
    split: bool = False,
    imported: list[DdiInfo] = [],
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
    strip_pkgdb: bool = False,
    mkfs_options: dict[str, list[str]] = {},
    basename: str = "image",
    identifier: str | None = None,
    extension: dict[str, typing.Any] | None = None,
    tmpfiles: list[str] | None = None,
    exclude: list[str] = [],
) -> DdiOutput:
    """Declare the systemd-repart action that builds a DDI from an image and partition definitions.

    With strip_pkgdb, the partitions omit the package database wherever the image's package system
    keeps it, for a system nothing ever resolves packages in. The image's `[pkgdb]` and `[sbom]`
    subtargets still capture it from the tree itself. `extension` makes the driver package the tree
    as an extension (see extension.bzl). `tmpfiles` replaces the tmpfiles lines of `image`. The
    partitions leave out the contents of the directories in `exclude`.
    """
    _check_definitions(
        definitions,
        disk = disk,
        imports = bool(imported),
        signed = verity_key != None,
        split = split,
    )

    # repart hands mkfs one variable per filesystem and splits it on whitespace, so an option
    # carrying any would silently arrive as two.
    for filesystem, options in mkfs_options.items():
        for option in options:
            if " " in option or "\t" in option:
                fail(
                    "ddi: {} mkfs option {!r} holds whitespace, which repart splits on".format(
                        filesystem,
                        option,
                    )
                )

    imported_partitions = []
    for info in imported:
        imported_partitions.extend(info.partitions)

    # A DDI has at most one verity root hash. This invocation generates it, or an imported DdiInfo
    # contains it, such as the split /usr partitions that a bootable disk copies in.
    root_hashes = [info.root_hash for info in imported if info.root_hash != None]
    kind = _verity_kind(definitions)
    if len(root_hashes) + (1 if kind != None else 0) > 1:
        fail("ddi: cannot combine multiple verity root hashes")

    names = [definition["name"] for definition in definitions] + [partition.definition["name"] for partition in imported_partitions]
    if len(names) != len({name: True for name in names}):
        fail("ddi: new and imported partition names must be unique")

    definition_specs = []
    outputs = []
    new_partitions = []
    manifests = {}
    for definition in definitions:
        name = definition["name"]

        # A definition that copies nothing carries nothing to list: verity partitions hold a hash
        # tree, and a filesystem created empty is populated by the system that grows it rather than
        # by this build.
        listing = None
        if definition["copy_files"]:
            listing = declare_out(ctx, identifier, "manifests/{}.Uapi16Manifest".format(name))

        # A partition leaves this invocation as an artifact of its own only when it splits them
        # back out; otherwise it exists solely inside the disk they are composed into.
        blocks = None
        metadata = None
        if split:
            blocks = declare_out(ctx, identifier, "partitions/{}.raw".format(name))
            metadata = declare_out(ctx, identifier, "partitions/{}.json".format(name))
            outputs.append(blocks)
            new_partitions.append(
                PartitionInfo(
                    definition = definition,
                    blocks = blocks,
                    manifest = listing,
                    metadata = metadata,
                ),
            )

        definition_specs.append({
            "blocks": _output(blocks),
            "definition": definition,
            "manifest": _output(listing),
            "metadata": _output(metadata),
        })
        if listing != None:
            manifests[name] = listing

    spec = {
        # repart has no partition type for any architecture, so an `_any` DDI gets the build host's types.
        "architecture": None if systemd_arch == "_any" else systemd_arch,
        "basename": basename,
        "definitions": definition_specs,
        "exclude": exclude,
        "extension": extension,
        # The default seed derives from the identity, so changing "repart" here changes the partition
        # UUIDs of every target that declares no identifier.
        "identity": "{}[{}]".format(ctx.label, identifier or "repart"),
        "manifest": None,
        "mkfs_options": mkfs_options,
        "out": None,
        "partitions": [
            {
                "blocks": value.blocks,
                "definition": value.definition,
                "manifest": value.manifest,
                "metadata": value.metadata,
            }
            for value in imported_partitions
        ],
        "pkgdb_paths": pkgdb_paths(image) if strip_pkgdb else [],
        "root_hash_out": None,
        "seed": seed,
        "signing": signing_key_spec(verity_key),
    }

    if tmpfiles != None:
        spec["tmpfiles"] = tmpfiles

    out = None
    manifest = None
    if disk:
        out = declare_out(ctx, identifier, basename + ".raw")
        spec["out"] = out.as_output()

        # Nothing to assemble a listing of the whole from when no part of this disk was listed,
        # which is a disk of imported verity partitions and nothing else.
        if manifests or [it for it in imported_partitions if it.manifest != None]:
            manifest = declare_out(ctx, identifier, basename + ".Uapi16Manifest")
            spec["manifest"] = manifest.as_output()

    root_hash = root_hashes[0] if root_hashes else None
    if kind != None:
        # The root hash file is named after the disk, so that a release can publish the root
        # hashes of several DDIs.
        hash_output = declare_out(ctx, identifier, "{}.{}hash".format(basename, kind))
        spec["root_hash_out"] = hash_output.as_output()
        root_hash = RootHashInfo(hash = hash_output, kind = kind)

    available_partitions = imported_partitions + new_partitions
    info = DdiInfo(
        disk = out,
        manifest = manifest,
        partitions = available_partitions,
        manifests = manifests,
        root_hash = root_hash,
    )
    sub_targets = manifest_sub_targets(manifests)
    if manifest != None:
        # Qualified, unlike the provider field: a logical image publishes a `manifest` subtarget of
        # its own (image.bzl:image_metadata_subtargets) listing its whole tree, and a composition
        # merges both sets of subtargets into one namespace.
        sub_targets["disk-manifest"] = [DefaultInfo(default_output = manifest)]
    if root_hash != None:
        sub_targets["roothash"] = [DefaultInfo(default_output = root_hash.hash), info]

    if out != None:
        outputs = [out]
        if available_partitions:
            sub_targets["partitions"] = [
                DefaultInfo(
                    default_outputs = [partition.blocks for partition in available_partitions],
                    sub_targets = _partition_sub_targets(available_partitions),
                ),
                info,
            ]
    else:
        sub_targets.update(_partition_sub_targets(new_partitions))
    signing_access = merge_signing_access([verity_key])
    ctx.actions.run(
        terminal_image_command(
            ctx,
            signing_access = signing_access,
            driver = "ddi",
            exe = tools.ddi,
            identifier = identifier,
            image = image,
            spec = spec,
        ),
        category = "ddi_split" if split else "ddi",
        identifier = identifier or "ddi",
        **external_signing_execution(signing_access),
    )
    return DdiOutput(info = info, outputs = outputs, sub_targets = sub_targets)

# image.disk passes these attributes on to declare_ddi().
DDI_ATTRS = {
    "definitions": attrs.list(attrs.string(), doc = "serialized partition definitions"),
    "mkfs_options": attrs.dict(
        attrs.string(),
        attrs.list(attrs.string()),
        default = {},
        doc = "filesystem -> the options mkfs is given when formatting a partition of that type",
    ),
    "strip_pkgdb": attrs.bool(
        default = False,
        doc = "leave the package database out of the partitions; the image still carries it",
    ),
    "verity_key": VERITY_KEY_ATTR,
}

# The disk conversion output formats, doubling as each artifact's extension.
DISK_FORMATS = ["qcow2", "raw.zst", "vmdk"]

# The formats whose disk has a size of its own, so it can leave room past the partitions for first boot to
# claim. The raw itself stays as small as its partitions.
VIRTUAL_SIZE_FORMATS = ["qcow2", "vmdk"]

DiskConversionInfo = provider(
    doc = "A composed raw disk re-encoded into one distributable format.",
    fields = {
        "format": provider_field(str),
        "image": provider_field(Artifact),
    },
)

def declare_disk_conversion(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    disk: DdiInfo,
    box_dep: Dependency,
    format: str,
    virtual_size: str | None,
    basename: str,
) -> DiskConversionInfo:
    """Declare a disk format conversion from a resolved raw disk provider.

    `virtual_size` sizes the disk of a format in VIRTUAL_SIZE_FORMATS, which otherwise is as large as
    the raw.
    """
    out = declare_out(ctx, format, basename + "." + format)
    cmd = cmd_args(
        box.run(box = box_dep[box.Info], exe = tools.convert),
        specs.args(
            ctx.actions,
            spec_path(format, "convert"),
            {
                "format": format,
                "input": disk.disk,
                "out": out.as_output(),
                "virtual_size": virtual_size,
            },
        ),
    )
    ctx.actions.run(cmd, category = "disk_convert", identifier = format)
    return DiskConversionInfo(format = format, image = out)
