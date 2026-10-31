# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Logical filesystem images built as ordered overlay deltas."""

load("//:specs.bzl", "executable", "spec_args", "spec_argument")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//distribution:defs.bzl", "TOOLS_BOX_ATTR", "default_package_manager", "distribution")
load("//package:install.bzl", "resolve_packages")
load("//package:manager.bzl", "PackageManagerInfo")
load("//package:system.bzl", "PackageSystemInfo")
load("//platforms:architecture.bzl", "ARCHITECTURES", "architecture")
load(":sign.bzl", "SigningAccess", "SigningKeyInfo", "external_signing_execution", "key_source_arguments", "merge_signing_access")

ImageToolsInfo = provider(
    doc = "The pinned drivers every image rule and terminal output runs.",
    fields = {
        "archive": provider_field(Dependency),
        "artifacts": provider_field(Dependency),
        "boot": provider_field(Dependency),
        "convert": provider_field(Dependency),
        "ddi": provider_field(Dependency),
        "layer": provider_field(Dependency),
        "manifest": provider_field(Dependency),
        "python": provider_field(Dependency),
        "sbom": provider_field(Dependency),
        "syft": provider_field(Dependency),
        "uki": provider_field(Dependency),
    },
)

_TOOLS = [
    "archive",
    "artifacts",
    "boot",
    "convert",
    "ddi",
    "layer",
    "manifest",
    "python",
    "sbom",
    "syft",
    "uki",
]

def _image_tools_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        ImageToolsInfo(**{name: getattr(ctx.attrs, name) for name in _TOOLS}),
    ]

image_tools = rule(
    impl = _image_tools_impl,
    attrs = {name: attrs.dep(providers = [RunInfo]) for name in _TOOLS},
)

# Every rule resolves its drivers through one bundle instead of a private attribute each.
IMAGE_TOOLS_ATTR = {
    "_tools": attrs.exec_dep(providers = [ImageToolsInfo], default = "tine//image:tools"),
}

NAME_PATTERN = "^[a-zA-Z0-9._-]+$"

# "+" would collide with sd-boot's boot-counting suffixes and "~" is systemd's pre-release
# separator, so filename components accept the latter but never the former.
FILENAME_PATTERN = "^[a-zA-Z0-9._~-]+$"

# Versions additionally accept "^", systemd's post-release separator.
VERSION_PATTERN = "^[a-zA-Z0-9._~^-]+$"

# What a GPT partition label holds, which is what a rendered label has to stay inside.
GPT_LABEL_LIMIT = 36

# The version a caller asks to have rendered from build configuration instead of declaring outright,
# on its own or before the base to render it around. A version accepts no ":", so neither spelling
# can collide with one a caller means literally. `version.bzl` resolves them.
AUTO = "auto"
AUTO_PREFIX = "auto:"

def check_name(what: str, value: str, pattern: str = NAME_PATTERN) -> str:
    """Reject a name that cannot survive the path, label, or filename it ends up in."""
    if not regex_match(pattern, value):
        fail("invalid {}: {!r}".format(what, value))
    return value

def check_version(what: str, value: str) -> str:
    """Reject a version that is not one, an unresolved `auto` above all.

    A rule sees a version once its declaration macro has resolved it, so a sentinel arriving here
    came from a rule that resolves nothing, or through a `select()`, which cannot be read where the
    resolving happens. Left alone it would pass as a literal and name partitions and files `auto`.
    """
    if value == AUTO or value.startswith(AUTO_PREFIX):
        fail(
            "{}: version {!r} reached the build unresolved. Declare it on a rule that renders it ".format(what, value)
            + "(archive, sysext, confext, extension, portable, initrd_image, disk) and not through a "
            + 'select(), or name the version outright; see "Image versioning" in images.md.',
        )
    return check_name(what, value, VERSION_PATTERN)

def _path(identifier: str | None, name: str) -> str:
    return identifier + "/" + name if identifier != None else name

def declare_out(ctx: AnalysisContext, identifier: str | None, name: str, dir: bool = False) -> Artifact:
    """Declare an output, scoped to `identifier` when one composition declares several."""
    return ctx.actions.declare_output(_path(identifier, name), dir = dir)

def spec_path(identifier: str | None, driver: str) -> str:
    """Name a driver's spec, scoped like the outputs of the same composition step."""
    return _path(identifier, driver + ".spec.json")

ImageSbomInfo = provider(
    doc = "SPDX and CycloneDX SBOMs generated from one logical image.",
    fields = {
        "cyclonedx": provider_field(Artifact),
        "spdx": provider_field(Artifact),
    },
)

ImageInfo = provider(
    doc = "A logical filesystem image represented by an ordered delta stack and lazy metadata.",
    fields = {
        "box": provider_field(Dependency),
        # Accumulated install specs; they seed the local-packages closure of every derived layer
        # so lower-layer packages keep their local backing in later solves.
        "install_specs": provider_field(list[str], default = []),
        "layers": provider_field(list[Artifact]),
        # Every path the assembled tree holds, as a UAPI.16 file manifest.
        "manifest": provider_field(Artifact),
        "package_manager": provider_field(Dependency | None, default = None),
        # Absent only when the image installs no packages and so has no package system.
        "pkgdb": provider_field(Artifact | None, default = None),
        "sbom": provider_field(ImageSbomInfo),
        # Ownership is deliberately not represented: image outputs use uid/gid 0.
        "tmpfiles": provider_field(list[str]),
    },
)

def _image_command(
    ctx: AnalysisContext,
    *,
    box: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    exe: Dependency,
    driver: str,
    identifier: str | None,
    spec: dict[str, typing.Any],
    signing_access: SigningAccess | None = None,
) -> cmd_args:
    return cmd_args(
        box_run(
            box = box[BoxInfo],
            exe = exe,
            ro_binds = signing_access.ro_binds if signing_access else {},
            setenv = signing_access.setenv if signing_access else {},
        ),
        spec_args(
            ctx.actions,
            spec_path(identifier, driver),
            {"lower": layers, "tmpfiles": tmpfiles} | spec,
        ),
    )

def terminal_image_command(
    ctx: AnalysisContext,
    *,
    image: ImageInfo,
    exe: Dependency,
    driver: str,
    spec: dict[str, typing.Any],
    identifier: str | None = None,
    signing_access: SigningAccess | None = None,
) -> cmd_args:
    """Run a terminal driver against one finalized logical-image stack.

    The stack and its deferred tmpfiles join the driver's own fields in one spec. `signing_access`
    grants the driver what an externally held signing key needs from the host.
    """
    return _image_command(
        ctx,
        signing_access = signing_access,
        driver = driver,
        box = image.box,
        exe = exe,
        identifier = identifier,
        layers = image.layers,
        spec = spec,
        tmpfiles = image.tmpfiles,
    )

def pkgdb_paths(image: ImageInfo) -> list[str]:
    """Where the image's package system keeps its database.

    Empty for an image with no package manager, which installed no packages and so has no
    database to strip.
    """
    if image.package_manager == None:
        return []
    return image.package_manager[PackageManagerInfo].package_system[PackageSystemInfo].database_paths

def image_metadata_subtargets(image: ImageInfo) -> dict[str, list[Provider]]:
    """Expose the canonical metadata carried by a logical image."""
    sub_targets = {
        "manifest": [DefaultInfo(default_output = image.manifest)],
        "sbom": [
            DefaultInfo(
                default_outputs = [image.sbom.spdx, image.sbom.cyclonedx],
                sub_targets = {
                    "cyclonedx": [DefaultInfo(default_output = image.sbom.cyclonedx)],
                    "spdx": [DefaultInfo(default_output = image.sbom.spdx)],
                },
            ),
            image.sbom,
        ],
    }
    if image.pkgdb != None:
        sub_targets["pkgdb"] = [DefaultInfo(default_output = image.pkgdb)]
    return sub_targets

def image_providers(
    *,
    image: ImageInfo,
    default_outputs: list[Artifact] = [],
    other_outputs: list[Artifact] = [],
    sub_targets: dict[str, list[Provider]] = {},
    extra: list[Provider] = [],
) -> list[Provider]:
    """Publish a terminal result together with its logical image and lazy metadata views.

    A caller's own entry for a view wins, which is how a rule that knows the name its artifacts are
    published under lets the views publish themselves under it too.
    """
    merged = image_metadata_subtargets(image)
    merged.update(sub_targets)
    return [
        DefaultInfo(
            default_outputs = default_outputs,
            other_outputs = other_outputs,
            sub_targets = merged,
        ),
        image,
        image.sbom,
    ] + extra

ImageInstallInfo = provider(
    doc = "Packages and operations a target contributes to any image installing it.",
    fields = {
        "operations": provider_field(list[typing.Any]),
        "packages": provider_field(list[str], default = []),
    },
)

LayerOperation = tuple

# Recursive type aliases are unavailable, so nested lists become dynamic at this boundary.
LayerOperationTree = LayerOperation | list[typing.Any]

# An operation argument, or a `select()` over it: Buck resolves the select where the rule reads the
# operation, so the helpers pass it through untouched.
Selectable = str | Select
SelectableArtifact = str | Artifact | Select

# Operations replayed by the layer driver.

def run(cmd: list[SelectableArtifact] | Select, env: dict[str, Selectable] = {}, chroot: bool | Select = False) -> LayerOperation:
    """Run `cmd` against the image, either with the box's tooling or the image's own.

    By default the box supplies the userspace and the image is mounted at /buildroot. With
    `chroot`, the command runs inside the image instead, and the project is bind-mounted at a
    fixed path which becomes the working directory, so an artifact argument resolves the same
    either way and a script the repository owns can simply be run.

    An argument names a build artifact by being one, or by spelling `$(location //target)` in a
    BUCK file. Buck's macro parser claims `$(...)`, so a shell substitution has to be written
    `\\$(...)`; an unescaped one fails to parse rather than reaching the shell.
    """
    return ("run", cmd, _environment(env), chroot)

def python(cmd: list[SelectableArtifact], env: dict[str, Selectable] = {}, chroot: bool | Select = False) -> LayerOperation:
    """Run a python script against the image, with the interpreter Buck already pins.

    `chroot` picks the image's view exactly as it does for `run`: mounted at /buildroot by default,
    or the script's own root. Either way the interpreter is the relocatable one Buck fetches for its
    own bootstrap, so the image never needs a python of its own. `cmd` is the script and its
    arguments, each naming artifacts as a `run` argument does.

    The rule prepends the interpreter rather than this naming it: a driver runs on the machine
    building, so it is the one the rule already holds for the execution platform. Reaching for it
    here, where only a target-configured `$(location)` is available, would fetch a second copy.
    """
    if not cmd:
        fail("python: cmd must not be empty")
    return ("python", cmd, _environment(env), chroot)

def _environment(env: dict[str, Selectable]) -> dict[str, Selectable]:
    return {name: env[name] for name in sorted(env)}

def mkdir(path: Selectable) -> LayerOperation:
    """Create a directory in the image."""
    return ("mkdir", path)

def symlink(target: Selectable, path: Selectable) -> LayerOperation:
    """Create a symlink at `path` pointing at `target`."""
    return ("symlink", target, path)

def write_file(path: Selectable, content: Selectable = "") -> LayerOperation:
    """Write a file at `path` in the image holding exactly `content`."""
    return ("write_file", path, content)

def remove(path: Selectable) -> LayerOperation:
    """Remove every image path matching an absolute glob pattern."""
    return ("remove", path)

def copy(source: SelectableArtifact | None, destination: Selectable) -> LayerOperation:
    """Copy a declared artifact to an absolute path in the image.

    A `select()` may resolve `source` to `None`, in which case nothing is copied: that is how a
    tree only some distributions ship is one operation rather than a target per distribution.
    """
    return ("copy", source, destination)

def install_from(target: str) -> LayerOperation:
    """Install what an `image_install()` target needs and apply the operations it attaches."""
    return ("install_from", target)

def expand_install_from(ops: list[typing.Any]) -> (list[str], list[typing.Any]):
    """Collect each install_from target's packages and splice its operations in place.

    One pass suffices at every level: a target's provider already carries expanded operations, and
    Starlark has neither recursion nor a while loop to do it any other way.
    """
    packages = []
    expanded = []
    for operation in ops:
        if operation[0] == "install_from":
            info = operation[1][ImageInstallInfo]
            packages += info.packages
            expanded += info.operations
        elif operation[0] == "copy" and operation[1] == None:
            # A select chose no source for this configuration.
            pass
        else:
            expanded.append(operation)
    return packages, expanded

def merge_os_release(fields: dict[str, str]) -> LayerOperation:
    """Merge quoted KEY="value" assignments into the image's /usr/lib/os-release."""
    return ("os_release", fields)

def depmod() -> LayerOperation:
    """Rebuild the module indexes for every kernel the image installs."""
    return ("depmod",)

def hwdb(usr: bool = True, strict: bool = True) -> LayerOperation:
    """Compile the image's hwdb.d into the binary database udev reads.

    It lands in /usr, where the image ships it and nothing writable shadows it; `usr = False`
    writes the /etc copy instead. `strict` refuses a source file the image cannot parse.
    """
    return ("hwdb", usr, strict)

def locale_gen() -> LayerOperation:
    """Generate the locales the image's /etc/locale.gen asks for, with its own locale-gen."""
    return ("locale_gen",)

# Every generator, for the compositions: they declare a whole product rather than one layer, so they
# generate the state its packages only describe instead of leaving that to their caller. Each is a
# no-op on an image carrying none of what it acts on, so the same three fit every composition.
_GENERATORS = [depmod(), hwdb(), locale_gen()]

def generated(ops: list[LayerOperation], installs: list[str]) -> list[LayerOperation]:
    """Append the generators `ops` does not already place, which is what a composition ends with."""

    # A layer that installs nothing and does nothing is never declared, and has nothing to generate
    # from either.
    if not ops and not installs:
        return ops

    # A generator the caller placed itself stays the only one of its kind: it was placed there, and
    # configured, on purpose.
    placed = {operation[0]: True for operation in ops}
    return ops + [generator for generator in _GENERATORS if generator[0] not in placed]

def sign_systemd_boot(key: SigningKeyInfo, arch: str) -> list[LayerOperation]:
    """Return operations that sign the image's systemd-boot binary as a `.signed` sibling.

    Sign before anything seals /usr, e.g. a verity partition; architecture.md explains why the signed
    binary must live in the image's own /usr. The key material never enters the image.
    """
    binary = "/buildroot/usr/lib/systemd/boot/efi/systemd-boot{}.efi".format(ARCHITECTURES[arch].efi)
    return [
        # systemd-sbsign lives outside PATH in the box.
        run(
            [
                "/usr/lib/systemd/systemd-sbsign",
                "sign",
                "--private-key",
                key.private_key,
                "--certificate",
                key.certificate,
            ]
            + key_source_arguments(key)
            + [
                "--output=" + binary + ".signed",
                binary,
            ],
        ),
    ]

def install_systemd_boot(key: SigningKeyInfo | None = None) -> list[LayerOperation]:
    """Return operations that install systemd-boot into the image ESP staging paths.

    With a signing key, bootctl prefers the `.signed` binaries (see sign_systemd_boot) and
    writes loader/keys/auto enrollment variables, which sd-boot enrolls on firmware in
    setup mode. The key material never enters the image.
    """
    enroll = []
    if key != None:
        enroll = [
            "--secure-boot-auto-enroll=yes",
            "--certificate",
            key.certificate,
            "--private-key",
            key.private_key,
        ] + key_source_arguments(key)
    return [
        mkdir("/efi"),
        run(
            [
                "bootctl",
                "install",
                "--root=/buildroot",
                "--install-source=image",
                "--all-architectures",
                "--no-variables",
            ]
            + enroll,
            env = {
                "SYSTEMD_ESP_PATH": "/efi",
                "SYSTEMD_XBOOTLDR_PATH": "/boot",
            },
        ),
        remove("/efi/loader/random-seed"),
    ]

def _encode_operation(operation: LayerOperation, python: Dependency) -> LayerOperation:
    if operation[0] not in ("run", "python"):
        return operation

    # -B: the script and its imports are project sources, which no build may write bytecode into.
    prefix = [executable(python), "-B"] if operation[0] == "python" else []

    # A box argument is a plain string, an artifact, or a resolved $(location) macro.
    return (
        "run",
        prefix + [spec_argument(argument) for argument in operation[1]],
        operation[2],
        operation[3],
    )

def _install_specs(
    packages: list[str],
    package_sets: list[str],
    available: dict[str, list[str]] | None,
) -> list[str]:
    """Resolve a layer's requested packages and symbolic sets into one deduplicated request."""
    specs = list(packages)
    for name in package_sets:
        if available == None:
            fail("image: package_sets requires an image with a package manager")
        members = available.get(name)
        if members == None:
            fail("image: unknown package set {!r}".format(name))
        specs += members
    for spec in specs:
        if not spec:
            fail("image: package names must not be empty")
    return sorted({spec: None for spec in specs})

def _declare_pkgdb(
    ctx: AnalysisContext,
    *,
    box: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    package_manager: Dependency,
    identifier: str | None,
) -> Artifact:
    system = package_manager[PackageManagerInfo].package_system[PackageSystemInfo]
    out = declare_out(ctx, identifier, "pkgdb." + system.database_format)
    cmd = _image_command(
        ctx,
        driver = "pkgdb",
        box = box,
        exe = system.pkgdb,
        identifier = identifier,
        layers = layers,
        spec = {"out": out.as_output()},
        tmpfiles = tmpfiles,
    )
    ctx.actions.run(cmd, category = "image_pkgdb", identifier = identifier or "pkgdb")
    return out

def _declare_manifest(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    box: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    identifier: str | None,
) -> Artifact:
    # The name the format reserves for itself, so the listing can be dropped beside a tree of the
    # image it describes and be found there.
    out = declare_out(ctx, identifier, "Uapi16Manifest")
    cmd = _image_command(
        ctx,
        driver = "manifest",
        box = box,
        exe = tools.manifest,
        identifier = identifier,
        layers = layers,
        spec = {"out": out.as_output()},
        tmpfiles = tmpfiles,
    )
    ctx.actions.run(cmd, category = "image_manifest", identifier = identifier or "manifest")
    return out

def _declare_sbom(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    box: Dependency,
    layers: list[Artifact],
    tmpfiles: list[str],
    source_name: str,
    version: str,
    identifier: str | None,
) -> ImageSbomInfo:
    spdx = declare_out(ctx, identifier, "sbom.spdx.json")
    cdx = declare_out(ctx, identifier, "sbom.cdx.json")
    cmd = _image_command(
        ctx,
        driver = "sbom",
        box = box,
        exe = tools.sbom,
        identifier = identifier,
        layers = layers,
        spec = {
            "cdx": cdx.as_output(),
            "source_name": source_name,
            "source_version": version,
            "spdx": spdx.as_output(),
            "syft": executable(tools.syft),
        },
        tmpfiles = tmpfiles,
    )
    ctx.actions.run(cmd, category = "image_sbom", identifier = identifier or "sbom")

    # One syft run emits both formats; selecting either format still executes the shared action.
    return ImageSbomInfo(cyclonedx = cdx, spdx = spdx)

def declare_image(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    ops: list[LayerOperation],
    packages: list[str] = [],
    package_sets: list[str] = [],
    identifier: str | None = None,
    parent: ImageInfo | None = None,
    box: Dependency | None = None,
    package_manager: Dependency | None = None,
    tmpfiles: list[str] = [],
    install_docs: bool = True,
    install_langs: list[str] = [],
    source_name: str | None = None,
    version: str = "0",
    keys: list[SigningKeyInfo | None] = [],
) -> ImageInfo:
    """Declare one logical image layer from resolved providers, packages, and operations.

    The layer installs `packages` and `package_sets` as one request before its operations run, so
    every operation sees the packages this layer adds. `keys` names the signing keys this layer's
    operations use, so that the action can reach one held outside the build. An initial image runs in
    `box`, and a derived image runs in the box of its parent.
    """
    # Every image rule's version reaches its layers through here, which makes this the one place
    # that has to catch one no declaration macro resolved.
    check_version("image version", version)
    if parent != None:
        if package_manager != None:
            fail("image: parent cannot be combined with package_manager")
        box = parent.box
        package_manager = parent.package_manager
        layers = parent.layers
        tmpfiles = parent.tmpfiles + tmpfiles
        parent_install_specs = parent.install_specs
    else:
        if box == None:
            fail("image: an image without a parent requires a box")
        layers = []
        parent_install_specs = []

    available_sets = None
    if package_manager != None:
        available_sets = package_manager[PackageManagerInfo].package_sets
    from_targets, operations = expand_install_from(ops)
    install_specs = _install_specs(packages + from_targets, package_sets, available_sets)

    if operations or install_specs:
        closure = None
        package_manager_info = None
        if install_specs:
            if package_manager == None:
                fail("image: installing packages requires an image with a package manager")
            closure = resolve_packages(
                ctx,
                package_manager,
                box,
                install_specs,
                layers,
                identifier = identifier,
                local_seed = parent_install_specs + install_specs,
            )
            package_manager_info = package_manager[PackageManagerInfo]

        delta = declare_out(ctx, identifier, "delta", dir = True)
        work = declare_out(ctx, identifier, "overlay.work", dir = True) if layers else None
        spec = {
            "install": None,
            "lower": layers,
            "operations": [_encode_operation(operation, tools.python) for operation in operations],
            "out": delta.as_output(),
            "work": work.as_output() if work != None else None,
        }
        if package_manager_info != None:
            system = package_manager_info.package_system[PackageSystemInfo]
            spec["install"] = {
                "arch": architecture.spelling(package_manager_info.arch, system.arch_schema),
                "docs": install_docs,
                "installer": executable(system.install),
                "langs": install_langs,
                "packages_dir": closure,
            }
        signing_access = merge_signing_access(keys)
        cmd = cmd_args(
            box_run(
                box = box[BoxInfo],
                exe = tools.layer,
                ro_binds = signing_access.ro_binds,
                setenv = signing_access.setenv,
            ),
            spec_args(ctx.actions, spec_path(identifier, "layer"), spec),
        )
        ctx.actions.run(cmd, category = "image", identifier = identifier or "layer", **external_signing_execution(signing_access))

        layers = layers + [delta]
        install_specs = parent_install_specs + install_specs
    else:
        install_specs = parent_install_specs

    pkgdb = None
    if package_manager != None:
        pkgdb = _declare_pkgdb(
            ctx,
            box = box,
            identifier = identifier,
            layers = layers,
            package_manager = package_manager,
            tmpfiles = tmpfiles,
        )
    return ImageInfo(
        box = box,
        install_specs = install_specs,
        layers = layers,
        manifest = _declare_manifest(
            ctx,
            tools = tools,
            box = box,
            identifier = identifier,
            layers = layers,
            tmpfiles = tmpfiles,
        ),
        package_manager = package_manager,
        pkgdb = pkgdb,
        sbom = _declare_sbom(
            ctx,
            tools = tools,
            box = box,
            identifier = identifier,
            layers = layers,
            source_name = source_name or ctx.label.name,
            tmpfiles = tmpfiles,
            version = version,
        ),
        tmpfiles = tmpfiles,
    )

IMAGE_OPERATION_ATTR = attrs.one_of(
    # A command takes attrs.arg() whether or not it chroots, so an artifact argument reads the
    # same either way; a shell substitution is escaped as `\\$(...)` at the call site. "python" is
    # the same command with the interpreter the rule holds in front of it.
    attrs.tuple(
        attrs.enum(["run", "python"]),
        attrs.list(attrs.arg()),
        attrs.dict(attrs.string(), attrs.string()),
        attrs.bool(),
    ),
    attrs.tuple(
        attrs.enum(["mkdir"]),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["symlink"]),
        attrs.string(),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["write_file"]),
        attrs.string(),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["remove"]),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["copy"]),
        attrs.option(attrs.source(allow_directory = True)),
        attrs.string(),
    ),
    attrs.tuple(
        attrs.enum(["install_from"]),
        attrs.dep(providers = [ImageInstallInfo]),
    ),
    attrs.tuple(
        attrs.enum(["os_release"]),
        attrs.dict(attrs.string(), attrs.string()),
    ),
    attrs.tuple(attrs.enum(["depmod"])),
    attrs.tuple(
        attrs.enum(["hwdb"]),
        attrs.bool(),
        attrs.bool(),
    ),
    attrs.tuple(attrs.enum(["locale_gen"])),
)

# The layer attributes every rule that builds a logical image from operations shares.
IMAGE_ATTRS = {
    "install_docs": attrs.bool(
        default = True,
        doc = "keep documentation; licenses are kept either way (default: keep it)",
    ),
    "install_langs": attrs.list(
        attrs.string(),
        default = [],
        doc = "keep translations only for these languages (default: keep all)",
    ),
    "ops": attrs.list(
        IMAGE_OPERATION_ATTR,
        default = [],
        doc = "ordered operations generated by the public helpers, applied after the install",
    ),
    "package_sets": attrs.list(
        attrs.string(),
        default = [],
        doc = "symbolic package sets the image's OS release names, installed with `packages`",
    ),
    "packages": attrs.list(
        attrs.string(),
        default = [],
        doc = "native packages to install before this layer's operations run",
    ),
    "tmpfiles": attrs.list(
        attrs.string(),
        default = [],
        doc = "deferred tmpfiles.d lines for paths, modes, and xattrs; ownership is ignored",
    ),
    "version": attrs.string(default = "0", doc = "SBOM source version"),
    "_box": TOOLS_BOX_ATTR,
} | IMAGE_TOOLS_ATTR

def _image_impl(ctx: AnalysisContext) -> list[Provider]:
    image = declare_image(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        box = ctx.attrs._box,
        install_docs = ctx.attrs.install_docs,
        install_langs = ctx.attrs.install_langs,
        ops = ctx.attrs.ops,
        package_manager = ctx.attrs.package_manager,
        package_sets = ctx.attrs.package_sets,
        packages = ctx.attrs.packages,
        parent = ctx.attrs.parent[ImageInfo] if ctx.attrs.parent != None else None,
        tmpfiles = ctx.attrs.tmpfiles,
        version = ctx.attrs.version,
    )
    return image_providers(
        default_outputs = image.layers[-1:],
        image = image,
    )

_image = rule(
    impl = _image_impl,
    supports_incoming_transition = True,
    attrs = IMAGE_ATTRS
    | {
        "package_manager": attrs.option(
            attrs.dep(providers = [PackageManagerInfo]),
            default = None,
            doc = "the native package manager fixed for an initial image",
        ),
        "parent": attrs.option(
            attrs.dep(providers = [ImageInfo]),
            default = None,
            doc = "the logical image to extend instead of starting an initial image",
        ),
    },
)

def flatten_operations(ops: list[LayerOperationTree]) -> list[LayerOperation]:
    """Flatten nested operation groups so helpers can return ordered sequences."""
    flattened = []
    for operation in ops:
        if type(operation) == "list":
            flattened.extend(flatten_operations(operation))
        else:
            flattened.append(operation)
    return flattened

def initial_package_manager(package_manager: str | Select | None, parent: str | Select | None, distro: str | None) -> str | Select | None:
    """Return the package manager that an image declaration starts its image from.

    An image that names no `package_manager` and no `parent` gets the package manager of its
    distribution. An image with a `parent` gets None, because it uses the package manager of its parent.
    """
    if package_manager != None or parent != None:
        return package_manager
    return default_package_manager(distro)

def layer(
    name: str,
    ops: list[LayerOperationTree] = [],
    package_manager: str | Select | None = None,
    parent: str | Select | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Create an initial image or apply one delta to a parent image."""
    _image(
        name = name,
        ops = flatten_operations(ops),
        package_manager = initial_package_manager(package_manager, parent, distro),
        parent = parent,
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

def _image_install_impl(ctx: AnalysisContext) -> list[Provider]:
    from_targets, operations = expand_install_from(ctx.attrs.ops)

    # A convenience view: building this target builds what it would copy into an image.
    copies = {}
    for operation in operations:
        if operation[0] == "copy":
            copies[operation[1].short_path] = operation[1]
    return [
        DefaultInfo(default_outputs = [copies[path] for path in sorted(copies)]),
        ImageInstallInfo(
            operations = operations,
            packages = sorted({package: None for package in ctx.attrs.packages + from_targets}),
        ),
    ]

_image_install = rule(
    impl = _image_install_impl,
    attrs = {
        "ops": attrs.list(IMAGE_OPERATION_ATTR, default = [], doc = "operations an image applies to install this target"),
        "packages": attrs.list(attrs.string(), default = [], doc = "native packages this target needs installed"),
    },
)

def image_install(name: str, ops: list[LayerOperationTree] = [], packages: list[str] = [], **kwargs) -> None:
    """Attach packages and install operations to a target, applied by `install_from()` in an image.

    An image installing this target adds its packages to its own install request, so a reusable
    target declares what it needs rather than leaving it to every caller.
    """
    if not ops and not packages:
        fail("image_install {}: declare the packages or operations that install it".format(name))
    _image_install(name = name, ops = flatten_operations(ops), packages = packages, **(distribution.attrs() | kwargs))
