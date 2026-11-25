# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Unified kernel images, and the boot artifacts extracted from a completed logical image."""

load("//kmod:initrd_modules.toml", _INITRD_MODULES = "value")
load("//platform:architecture.bzl", "architecture")
load(":archive.bzl", "ImageArchiveInfo")
load(
    ":ddi.bzl",
    "DdiInfo",
    "RootHashInfo",  # @unused Used as a function argument type.
)
load(
    ":image.bzl",
    "FILENAME_PATTERN",
    "IMAGE_TOOLS_ATTR",
    "ImageInfo",
    "ImageToolsInfo",
    "check_name",
    "check_version",
    "declare_out",
    "terminal_image_command",
)
load(":sign.bzl", "SigningKeyInfo", "external_signing_execution", "merge_signing_access", "resolve_signing_key", "signing_key_spec")

UkiInfo = provider(
    doc = "Unified kernel images built for one logical image.",
    fields = {
        # The pieces for a direct kernel boot.
        "cmdline": provider_field(Artifact),
        "initrd": provider_field(Artifact),
        "kernel": provider_field(Artifact),
        "modules": provider_field(Artifact),
        # The UKI, which the directory `ukis` holds.
        "uki": provider_field(Artifact),
        "ukis": provider_field(Artifact),
    },
)

UkiProfile = dict

def _check_profile_fields(what: str, fields: dict[str, str]) -> None:
    for key, value in fields.items():
        check_name(what + " field", key, "^[A-Z][A-Z0-9_]*$")
        if "\n" in value:
            fail("{}: the value of field {} must be a single line".format(what, key))

def uki_profile(id: str, title: str, cmdline: list[str], sign_expected_pcr: bool = True, fields: dict[str, str] = {}) -> UkiProfile:
    """Describe one alternative boot profile embedded in a UKI.

    Profiles whose boot state is not sealed against (e.g. installers or factory reset) can opt
    out of the expected-PCR policy with sign_expected_pcr = False. fields adds further KEY=VALUE
    lines to the .profile section, such as PCRLOCK=yes for systemd-pcrlock.
    """

    # sd-boot derives entry identifiers from the id, so keep it filename- and env-file-safe.
    check_name("uki_profile id", id, "^[A-Za-z0-9._-]+$")
    if not title or "\n" in title:
        fail("uki_profile: title must be a single non-empty line")
    for argument in cmdline:
        if not argument:
            fail("uki_profile: cmdline arguments cannot be empty")
    for key in ("ID", "TITLE"):
        if key in fields:
            fail("uki_profile: pass {} as the {} argument, not as a field".format(key, key.lower()))
    _check_profile_fields("uki_profile", fields)
    return {
        "cmdline": cmdline,
        "fields": fields,
        "id": id,
        "sign_expected_pcr": sign_expected_pcr,
        "title": title,
    }

def encode_profiles(profiles: list[UkiProfile]) -> list[str]:
    """Reject duplicate profile ids and serialize the profiles for an attribute."""
    ids = {}
    for profile in profiles:
        # sd-boot lowercases profile ids in its entry identifiers. "Debug" and "debug" would name the same entry.
        if profile["id"].lower() in ids:
            fail("uki: duplicate profile id {!r}".format(profile["id"]))
        ids[profile["id"].lower()] = True
    return [json.encode(profile) for profile in profiles]

def declare_uki(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    initrds: list[ImageArchiveInfo],
    cmdline: list[str],
    profiles: list[str],
    profile_fields: dict[str, str],
    arch: str,
    image_id: str,
    version: str,
    initrd_modules: list[str],
    splash: Artifact | str | None = None,
    root_hash: RootHashInfo | None = None,
    secure_boot_key: SigningKeyInfo | None = None,
    sign_expected_pcr_key: SigningKeyInfo | None = None,
    identifier: str | None = None,
) -> UkiInfo:
    """Declare UKI generation from resolved image providers."""
    if sign_expected_pcr_key != None and secure_boot_key == None:
        fail("uki: sign_expected_pcr_key needs Secure Boot signing, which signs the UKI it seals")
    # A relative string became a source at coercion, so a string reaching here is what
    # attrs.source() could not read; the only one that means something is an image path.
    if type(splash) == "string" and not splash.startswith("/"):
        fail("uki: splash must be a source or an absolute path in the image, got {!r}".format(splash))

    # ukify has one pair of provider options for both keys, so it cannot load one from a provider and
    # read the other from a file. Private keys and certificates are compared apart, because that is
    # how ukify names them: one key in a token and one certificate in a file is the same arrangement
    # for both roles or for neither.
    if sign_expected_pcr_key != None:
        for what, secure_boot_source, pcr_source in (
            ("private keys", secure_boot_key.private_key_source, sign_expected_pcr_key.private_key_source),
            ("certificates", secure_boot_key.certificate_source, sign_expected_pcr_key.certificate_source),
        ):
            if secure_boot_source != pcr_source:
                fail(
                    "uki: the Secure Boot and expected-PCR {} must come from the same source, got {!r} and {!r}".format(
                        what,
                        secure_boot_source,
                        pcr_source,
                    ),
                )
    out = declare_out(ctx, identifier, "ukis", dir = True)
    # Declared next to the UKIs, not inside them: that directory is copied onto the ESP whole.
    modules = declare_out(ctx, identifier, "modules.json")
    kernel = declare_out(ctx, identifier, "vmlinuz")
    initrd = declare_out(ctx, identifier, "initrd")
    cmdline_file = declare_out(ctx, identifier, "cmdline")
    check_name("uki image_id", image_id, FILENAME_PATTERN)
    for key in ("ID", "TITLE"):
        if key in profile_fields:
            fail("uki: every profile has an id and a title of its own, profile_fields can't set {}".format(key))
    _check_profile_fields("uki profile_fields", profile_fields)
    decoded_profiles = [json.decode(profile) for profile in profiles]
    if profile_fields:
        # ukify only writes a .profile section for the base profile into a UKI that joins further profiles.
        if not decoded_profiles:
            fail("uki: profile_fields needs at least one profile")
        # The base profile has the id main, which ukify also uses to select it with --sign-profile.
        for profile in decoded_profiles:
            if profile["id"].lower() == "main":
                fail("uki: the profile id main names the base profile")
    check_version("uki version", version)
    name = "{}_{}_{}.efi".format(image_id, version, architecture.ARCHITECTURES[arch].systemd)
    for archive in initrds:
        if archive.format != "cpio":
            fail("uki: initrd must be a cpio archive, got {!r}".format(archive.format))

    signing_access = merge_signing_access([secure_boot_key, sign_expected_pcr_key])
    cmd = terminal_image_command(
        ctx,
        signing_access = signing_access,
        driver = "uki",
        exe = tools.uki,
        identifier = identifier,
        image = image,
        spec = {
            "cmdline": cmdline,
            "cmdline_file": cmdline_file.as_output(),
            "efi_arch": architecture.ARCHITECTURES[arch].efi,
            "initrd": initrd.as_output(),
            "initrd_modules": initrd_modules,
            "initrds": [initrd.archive for initrd in initrds],
            "kernel": kernel.as_output(),
            "modules_manifest": modules.as_output(),
            "name": name,
            "out": out.as_output(),
            "profile_fields": profile_fields,
            "profiles": decoded_profiles,
            "root_hash": {
                "kind": root_hash.kind,
                "path": root_hash.hash,
            }
            if root_hash != None
            else None,
            "secure_boot": signing_key_spec(secure_boot_key),
            "sign_expected_pcr": signing_key_spec(sign_expected_pcr_key),
            "splash": splash,
        },
    )
    ctx.actions.run(cmd, category = "uki", identifier = identifier or "uki", **external_signing_execution(signing_access))
    return UkiInfo(
        cmdline = cmdline_file,
        initrd = initrd,
        kernel = kernel,
        modules = modules,
        uki = out.project(name),
        ukis = out,
    )

def uki_subtargets(info: UkiInfo) -> dict[str, list[Provider]]:
    """The selection manifest, exposed wherever the UKIs themselves are."""
    return {"modules": [DefaultInfo(default_output = info.modules)]}

def _uki_impl(ctx: AnalysisContext) -> list[Provider]:
    root_hash = None
    if ctx.attrs.root_hash != None:
        root_hash = ctx.attrs.root_hash[DdiInfo].root_hash
        if root_hash == None:
            fail("uki: root_hash DdiInfo does not contain a verity root hash")
    info = declare_uki(
        ctx,
        tools = ctx.attrs._tools[ImageToolsInfo],
        arch = ctx.attrs.arch,
        cmdline = ctx.attrs.cmdline,
        image = ctx.attrs.image[ImageInfo],
        image_id = ctx.attrs.image_id if ctx.attrs.image_id != None else ctx.label.name,
        initrd_modules = ctx.attrs.initrd_modules,
        initrds = [initrd[ImageArchiveInfo] for initrd in ctx.attrs.initrds],
        profiles = ctx.attrs.profiles,
        profile_fields = ctx.attrs.profile_fields,
        root_hash = root_hash,
        secure_boot_key = resolve_signing_key(ctx.attrs.secure_boot_key),
        sign_expected_pcr_key = resolve_signing_key(ctx.attrs.sign_expected_pcr_key),
        splash = ctx.attrs.splash,
        version = ctx.attrs.version,
    )
    return [DefaultInfo(default_output = info.ukis, sub_targets = uki_subtargets(info)), info]

UKI_ATTRS = {
    "arch": attrs.enum(architecture.ARCHITECTURES.keys(), default = architecture.configured()),
    "cmdline": attrs.list(
        attrs.string(),
        default = [],
        doc = "kernel command-line arguments embedded in the UKI",
    ),
    "initrd_modules": attrs.list(
        attrs.string(),
        default = _INITRD_MODULES["patterns"],
        doc = "glob patterns selecting the kernel modules the UKI's per-kernel initrd carries",
    ),
    "profile_fields": attrs.dict(
        attrs.string(),
        attrs.string(),
        default = {},
        doc = "KEY=VALUE lines of the .profile section of every profile, the base profile included, such as TOFU=; needs profiles",
    ),
    "profiles": attrs.list(
        attrs.string(),
        default = [],
        doc = "serialized alternative boot profiles",
    ),
    "secure_boot_key": attrs.option(
        attrs.dep(providers = [SigningKeyInfo]),
        default = None,
        doc = "key signing the UKI and its kernel",
    ),
    "sign_expected_pcr_key": attrs.option(
        attrs.dep(providers = [SigningKeyInfo]),
        default = None,
        doc = "key sealing the expected-PCR policy; without one the policy is not sealed",
    ),
    "splash": attrs.option(
        # An absolute path is a file in the image, which attrs.source() rejects; everything else is
        # a source, so a declaration reads the same either way.
        attrs.one_of(attrs.source(), attrs.string()),
        default = None,
        doc = "BMP embedded as the UKI splash screen: a source, or the absolute path of one the image ships",
    ),
}

_uki = rule(
    impl = _uki_impl,
    attrs = UKI_ATTRS
    | {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the image supplying the kernel, modules, stub, and os-release",
        ),
        "image_id": attrs.option(
            attrs.string(),
            default = None,
            doc = "first component of the UKI name <image_id>_<version>_<arch>.efi; defaults to the target name",
        ),
        "initrds": attrs.list(
            attrs.dep(providers = [ImageArchiveInfo]),
            default = [],
            doc = "cpio archives to prepend to the per-kernel modules archive",
        ),
        "root_hash": attrs.option(
            attrs.dep(providers = [DdiInfo]),
            default = None,
            doc = "DDI whose verity hash is added to the embedded kernel command line",
        ),
        "version": attrs.string(default = "0", doc = "image version in the UKI name"),
    }
    | IMAGE_TOOLS_ATTR,
)

def uki(name: str, profiles: list[UkiProfile] = [], **kwargs) -> None:
    """Build UKIs, with each profile added as an alternative sd-boot menu entry.

    A profile's cmdline is appended to the base cmdline (kernel arguments are last-wins, so
    profiles can also override it). With secure_boot_key, the UKI and its embedded kernel are signed
    for Secure Boot. With sign_expected_pcr_key, a signed expected-PCR policy covers every profile
    that does not opt out.
    """
    _uki(name = name, profiles = encode_profiles(profiles), **kwargs)

_ARTIFACTS = {
    "cmdline": "cmdline",
    "initrd": "initrd",
    "kernel": "vmlinuz",
    "uki": "image.efi",
}

BootArtifactsInfo = record(
    cmdline = Artifact,
    initrd = Artifact,
    kernel = Artifact,
    uki = Artifact,
)

def declare_boot_artifacts(
    ctx: AnalysisContext,
    *,
    tools: ImageToolsInfo,
    image: ImageInfo,
    identifier: str | None = None,
) -> BootArtifactsInfo:
    """Declare extraction of what a completed image boots: its kernel, initrd, command line and UKI.

    The driver picks the newest kernel the image carries and reads the initrd, kernel and command line
    out of the UKI's PE sections when it boots one, so the results are what a direct kernel boot needs.
    """
    selection = declare_out(ctx, identifier, "boot-artifacts.json")
    select = terminal_image_command(
        ctx,
        driver = "boot",
        exe = tools.boot,
        identifier = identifier,
        image = image,
        spec = {"out": selection.as_output()},
    )
    ctx.actions.run(select, category = "boot_artifact_select", identifier = identifier)

    artifacts = {}
    for kind in sorted(_ARTIFACTS):
        out = declare_out(ctx, identifier, _ARTIFACTS[kind])
        extract = terminal_image_command(
            ctx,
            # The extracted artifacts are named after their kind, so the specs of one composition
            # step are told apart by driver name; scoping them by kind would name a directory
            # after a file already declared beside it.
            driver = "artifacts-" + kind,
            exe = tools.artifacts,
            identifier = identifier,
            image = image,
            spec = {
                "artifact": kind,
                "manifest": selection,
                "out": out.as_output(),
            },
        )
        ctx.actions.run(extract, category = "boot_artifact_" + kind, identifier = identifier)
        artifacts[kind] = out
    return BootArtifactsInfo(
        cmdline = artifacts["cmdline"],
        initrd = artifacts["initrd"],
        kernel = artifacts["kernel"],
        uki = artifacts["uki"],
    )

def boot_subtargets(artifacts: BootArtifactsInfo) -> dict[str, list[Provider]]:
    """The boot artifacts, exposed wherever the image they come from is."""
    return {
        # Of an image.bootable, only a UKI carries one, so like [uki] it extracts on demand and fails
        # for a plain kernel.
        "cmdline": [DefaultInfo(default_output = artifacts.cmdline)],
        "initrd": [DefaultInfo(default_output = artifacts.initrd)],
        "kernel": [DefaultInfo(default_output = artifacts.kernel)],
        "uki": [DefaultInfo(default_output = artifacts.uki)],
    }

def _bootable_impl(ctx: AnalysisContext) -> list[Provider]:
    artifacts = declare_boot_artifacts(ctx, tools = ctx.attrs._tools[ImageToolsInfo], image = ctx.attrs.image[ImageInfo])

    # A UKI is not guaranteed to exist (kernels can ship as plain files), so the default
    # outputs stay limited to the artifacts selection always yields; [uki] extracts on demand.
    return [
        DefaultInfo(
            default_outputs = [artifacts.kernel, artifacts.initrd],
            sub_targets = boot_subtargets(artifacts),
        ),
    ]

bootable = rule(
    impl = _bootable_impl,
    attrs = {
        "image": attrs.dep(
            providers = [ImageInfo],
            doc = "the completed logical image from which to select boot artifacts",
        ),
    }
    | IMAGE_TOOLS_ATTR,
)
