# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Package a logical image as an extension DDI or as a portable service DDI."""

load("//platform:architecture.bzl", "architecture")
load(
    ":ddi.bzl",
    "declare_ddi",
    "partition",
    "verity_partitions",
)
load(
    ":image.bzl",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "image_providers",
)
load(":publish.bzl", "PublishedFile", "PublishedInfo", "published_metadata_subtargets")
load(
    ":sign.bzl",
    "SigningKeyInfo",
    "VERITY_KEY_ATTR",
)

ExtensionInfo = provider(
    doc = "The extension classes and scope of an extension DDI, beside the DDI's own provider.",
    fields = {
        "classes": provider_field(list[str]),
        # scope lists where systemd accepts the extension: "system", "initrd" or "portable".
        "scope": provider_field(list[str]),
    },
)

PortableInfo = provider(
    doc = "Marks the DDI beside it as a portable service image.",
    fields = {},
)

def system_extension(rule: str, dep: Dependency) -> ExtensionInfo:
    """Return the ExtensionInfo of `dep`, which `rule` hands to a host to merge."""
    extension = dep[ExtensionInfo]
    if "system" not in extension.scope:
        fail("{}: {} is not an extension with the system scope".format(rule, dep.label))
    return extension

def check_portable(rule: str, dep: Dependency) -> None:
    """Fail unless `dep`, which `rule` hands to portabled, is a portable image or an extension of one."""
    extension = dep.get(ExtensionInfo)
    if dep.get(PortableInfo) == None and (extension == None or "portable" not in extension.scope):
        fail("{}: {} is neither a portable image nor an extension with the portable scope".format(rule, dep.label))

def ddi_image_name(disk: Artifact) -> str:
    """The image name systemd takes from the file name of `disk`: everything up to the first "_".

    systemd looks up an extension's release file by it, and portablectl attaches the units of a
    portable image by it.
    """
    return disk.basename.split("_")[0]

# _CLASSES is a list and not a dict, because its order is the order of the CopyFiles= lines of the
# root partition.
_CLASSES = [
    ("sysext", struct(copy_files = ["/usr/", "/opt/"], prefix = "SYSEXT", release = "usr/lib/extension-release.d", suffix = ".sysext")),
    ("confext", struct(copy_files = ["/etc/"], prefix = "CONFEXT", release = "etc/extension-release.d", suffix = ".confext")),
]

EXTENSION_CLASSES = [name for name, _ in _CLASSES]

EXTENSION_SCOPES = ["system", "initrd", "portable"]

# systemd's path_extract_image_name() cuts an image name at the first "_" or "+", so the
# extension-release file of such a name is not found. portablectl cuts a name at the first "_" and
# refuses "+".
_NAME_PATTERN = "^[a-zA-Z0-9][a-zA-Z0-9.-]*$"

# erofs dedup keys off the filesystem UUID, so combined with a stable seed these keep the DDI
# reproducible.
_MKFS_OPTIONS = {"erofs": ["--quiet", "-zzstd,level=3", "-C524288", "-Efragments,ztailpacking,dedupe"]}

def ddi_names(what: str, name: str, version: str, arch: str) -> (str, str):
    """Return the name a DDI is published under, and the architecture its release file names.

    The DDI leaves the build as an update artifact, so it is named the way it is published:
    systemd-sysupdate matches a transfer against <name>_<version>_<arch>. arch "_any" opts out of a
    specific architecture: the DDI is published under the "all" arch (a literal "_any" would double
    the name separator and break vpick's version parse), and its extension-release ARCHITECTURE is
    "_any". systemd-vpick treats the unknown "all" as the any bucket, and later systemd-sysext skips
    the arch check for "_any".
    """
    check_name("{} name".format(what), name, _NAME_PATTERN)

    # systemd strips .sysext.raw and .confext.raw from a file name. A DDI named foo.sysext.raw has
    # the image name foo, and systemd does not find its extension-release.foo.sysext.
    if name.endswith(".sysext") or name.endswith(".confext"):
        fail("{}: the name {!r} ends in a suffix systemd strips from DDI file names".format(what, name))

    if arch == "_any":
        return "{}_{}_all".format(name, version), "_any"
    systemd_arch = architecture.ARCHITECTURES[arch].systemd
    return "{}_{}_{}".format(name, version, systemd_arch), systemd_arch

def _declare_preset(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    systemd_arch: str,
    basename: str,
    filename: str,
    version: str,
    kind: str,
    subkind: str | None,
    copy_files: list[str],
    seed: str | None,
    verity_key: SigningKeyInfo | None,
    extension: dict[str, typing.Any] | None = None,
    tmpfiles: list[str] | None = None,
    extra: list[Provider] = [],
) -> list[Provider]:
    """Declare a DDI of one verity protected root partition, and return the providers of its rule.

    `basename` is <name>_<version>_<arch>, which names the metadata of the logical image. `filename`
    names the DDI and adds the class suffix of an extension to `basename`. It is also the DDI's image
    in the release's description, so that a sysext and a confext of one name are two images. `version`
    is the version in `basename`. The release's description gives it to every file of the DDI.
    """
    # The partitions match the definitions systemd-repart --make-ddi uses, so that a DDI does not
    # depend on the systemd version of the box that builds it.
    root = partition(
        type = "root",
        filesystem = "erofs",
        copy_files = copy_files,
        minimize = "best",
        verity = "data",
        verity_match_key = "root",
    )
    output = declare_ddi(
        ctx,
        tools = tools,
        image = image,
        systemd_arch = systemd_arch,
        definitions = verity_partitions(root, signed = verity_key != None),
        basename = filename,
        mkfs_options = _MKFS_OPTIONS,
        seed = seed,
        # A merged extension must not replace the host's package database.
        strip_pkgdb = extension != None,
        verity_key = verity_key,
        extension = extension,
        tmpfiles = tmpfiles,
    )
    info = output.info
    published = PublishedInfo(
        artifacts = {
            info.disk.basename: PublishedFile(artifact = info.disk, kind = kind, subkind = subkind),
            info.manifest.basename: PublishedFile(artifact = info.manifest, kind = "listing", subkind = "uapi16manifest"),
            info.root_hash.hash.basename: PublishedFile(artifact = info.root_hash.hash, kind = "roothash"),
        },
        image = filename,
        version = version,
    )
    return image_providers(
        default_outputs = [info.disk],
        extra = [info, published] + extra,
        image = image,
        sub_targets = published_metadata_subtargets(image, basename, version, filename) | output.sub_targets,
    )

def declare_extension(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    name: str,
    classes: list[str],
    scope: list[str],
    arch: str,
    version: str,
    parent: ImageInfo | None = None,
    release: dict[str, str] = {},
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
) -> list[Provider]:
    """Declare an extension DDI with one extension-release file per class.

    With `parent`, the DDI contains only the delta that `image` layers on top of it.
    """
    if not classes:
        fail("{}: an extension needs at least one class of {}".format(name, EXTENSION_CLASSES))
    if not scope:
        fail("{}: an extension needs at least one scope of {}".format(name, EXTENSION_SCOPES))
    for key in release:
        if key.endswith("_SCOPE"):
            fail("{}: set {} through scope, not release; image.vm checks the scope".format(name, key))
    basename, systemd_arch = ddi_names("extension", name, version, arch)

    layers = 0
    tmpfiles = image.tmpfiles
    if parent != None:
        if len(parent.layers) >= len(image.layers):
            fail("{}: image must layer a delta on top of parent".format(name))
        layers = len(parent.layers)

        # The tmpfiles lines of the parent belong to the parent. Applied to the delta, they would
        # create files of the parent in the extension, which then replace the parent's own on merge.
        tmpfiles = image.tmpfiles[len(parent.tmpfiles) :]

    copy_files = []
    releases = []
    suffixes = []
    for class_name, spec in _CLASSES:
        if class_name not in classes:
            continue
        copy_files += spec.copy_files
        suffixes.append(spec.suffix)

        # systemd refuses an extension without ID. Without a parent, ID=_any matches any host. With a
        # parent, the driver pins the identity of the parent (see pin_release).
        fields = {}
        if parent == None:
            fields["ID"] = "_any"
        fields[spec.prefix + "_SCOPE"] = " ".join(scope)
        fields["EXTENSION_RELOAD_MANAGER"] = "1"

        # The rule fills in the name, version and architecture of the DDI, so a caller does not
        # repeat them. `release` overrides every field. Not IMAGE_VERSION: in an extension-release,
        # that is the image version of the *host* the extension requires, not our own version.
        fields["ARCHITECTURE"] = systemd_arch
        fields[spec.prefix + "_ID"] = name
        fields[spec.prefix + "_VERSION_ID"] = version
        fields.update(release)
        releases.append({
            "fields": {key: fields[key] for key in sorted(fields)},
            "path": "{}/extension-release.{}".format(spec.release, name),
        })

    return _declare_preset(
        ctx,
        tools = tools,
        image = image,
        systemd_arch = systemd_arch,
        basename = basename,
        # systemd recognizes the suffixes .sysext.raw and .confext.raw. No suffix names a DDI with
        # the two classes, so it gets the plain .raw suffix.
        filename = basename + (suffixes[0] if len(suffixes) == 1 else ""),
        version = version,
        kind = "extension",
        subkind = "+".join([class_name for class_name, _ in _CLASSES if class_name in classes]),
        copy_files = copy_files,
        seed = seed,
        verity_key = verity_key,
        tmpfiles = tmpfiles,
        extension = {
            "identity": str(ctx.label),
            "name": name,
            "parent": layers,
            "releases": releases,
        },
        extra = [
            ExtensionInfo(
                classes = [class_name for class_name, _ in _CLASSES if class_name in classes],
                scope = scope,
            ),
        ],
    )

def declare_portable(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    name: str,
    arch: str,
    version: str,
    seed: str | None = None,
    verity_key: SigningKeyInfo | None = None,
) -> list[Provider]:
    """Declare a portable service DDI that contains the whole tree of `image`."""
    basename, systemd_arch = ddi_names("portable", name, version, arch)
    return _declare_preset(
        ctx,
        tools = tools,
        image = image,
        systemd_arch = systemd_arch,
        basename = basename,
        # portablectl takes the image name from the file name up to the first "_", and
        # systemd-sysupdate matches a transfer against <name>_<version>_<arch>.raw.
        filename = basename,
        version = version,
        kind = "portable",
        subkind = None,
        copy_files = ["/"],
        seed = seed,
        verity_key = verity_key,
        extra = [PortableInfo()],
    )

_COMMON_ATTRS = {
    "arch": attrs.enum(
        architecture.ARCHITECTURES.keys() + ["_any"],
        default = architecture.configured(),
        doc = "architecture the image runs on, in its name and the ARCHITECTURE of an extension, "
        + "\"_any\" publishes the DDI as '..._all' and sets ARCHITECTURE=_any",
    ),
    "seed": attrs.option(
        attrs.string(),
        default = None,
        doc = "explicit GPT/partition UUID seed; by default derive one from the target identity",
    ),
    "verity_key": VERITY_KEY_ATTR,
}

PORTABLE_ATTRS = _COMMON_ATTRS

EXTENSION_ATTRS = _COMMON_ATTRS | {
    "classes": attrs.list(
        attrs.enum(EXTENSION_CLASSES),
        doc = 'the extension classes: "sysext" for /usr and /opt, "confext" for /etc',
    ),
    "release": attrs.dict(
        key = attrs.string(),
        value = attrs.string(),
        default = {},
        doc = "extension-release fields, overriding the defaults",
    ),
    "scope": attrs.list(
        attrs.enum(EXTENSION_SCOPES),
        default = ["system"],
        doc = "where systemd accepts the extension: on the host, in the initrd, for portable services",
    ),
}
