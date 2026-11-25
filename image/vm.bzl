# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Virtual machines of images, for an interactive runner and for harnesses that start them."""

load("@prelude//platforms:defs.bzl", "host_configuration")
load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load(":boot.bzl", "DirectBootInfo")
load(":ddi.bzl", "DdiInfo", "DiskConversionInfo", "SIZE_PATTERN")
load(":extension.bzl", "ExtensionInfo", "check_portable", "ddi_image_name", "system_extension")

# vmspawn takes the kernel command line as its own trailing arguments, and only the build knows the
# one a direct kernel boot needs, as the UKI's carries the root hash. So the runner reads it from the
# file at run time, and VmInfo's spec at build time. Word splitting is harmless: vmspawn joins the
# arguments with spaces again. Globbing is not, as a command line can carry patterns such as
# systemd.image_filter=usr=AOS_*.
_CMDLINE_FILE_RUNNER = 'set -f; cmdline=$(cat "$1"); shift; exec systemd-vmspawn $cmdline "$@"'

VmInfo = provider(
    doc = """The VM configuration that `image.vm` boots.

`spec` is a JSON file with:

- `vmspawn_args`: the `systemd-vmspawn` options of the machine, ending with the target's own, which
  therefore are machine options as well. The interactive runner specifics are left out: console,
  network and `autologin`.
- `credentials`: the credentials by name, for `--set-credential=`.
- `cmdline`: the kernel command line arguments, the words of `cmdline_file` and then `cmdline_extra`.
- `image`: the disk the machine boots.
- `coco`, `linux`, `secure_boot` and `tpm`: the attributes, for a harness to know what to expect, with
  `linux` resolved from a disk without an ESP and `tpm` from `secure_boot` when unset. An override in
  the target's `vmspawn_args` doesn't change them.

A harness runs `systemd-vmspawn` in the tools box of the image. It passes `vmspawn_args`, then its own
options, which override those except for vmspawn's additive ones. Then it passes the credentials merged
with its own (note that vmspawn refuses a credential name twice) and last `cmdline` and its own kernel
command line arguments. Paths are relative to the project root, and the files they name are hidden
inputs of `spec`.
""",
    fields = {"spec": provider_field(cmd_args)},
)

def _image(image: Dependency, image_format: str) -> Artifact:
    if image_format == "raw":
        disk = image[DdiInfo].disk
        if disk == None:
            fail("image_vm: {} is not a disk".format(image.label))
        return disk

    # The qcow is the intended VM format, and it has virtual_size; the raw doesn't.
    qcow2 = image[DefaultInfo].sub_targets.get("qcow2")
    if qcow2 == None:
        fail("image_vm: {} is not a disk with a qcow2".format(image.label))
    return qcow2[DiskConversionInfo].image

def _spec_impl(
    actions: AnalysisActions,
    cmdline_extra: list[str],
    cmdline_file: ArtifactValue | None,
    fields: dict[str, typing.Any],
    spec: OutputArtifact,
) -> list[Provider]:
    cmdline = cmdline_file.read_string().split() if cmdline_file != None else []
    actions.write_json(spec, fields | {"cmdline": cmdline + cmdline_extra}, pretty = True)
    return []

# Dynamic to read cmdline_file; see _CMDLINE_FILE_RUNNER.
_spec = dynamic_actions(
    impl = _spec_impl,
    attrs = {
        "cmdline_extra": dynattrs.value(list[str]),
        "cmdline_file": dynattrs.option(dynattrs.artifact_value()),
        "fields": dynattrs.value(dict[str, typing.Any]),
        "spec": dynattrs.output(),
    },
)

def _image_vm_impl(ctx: AnalysisContext) -> list[Provider]:
    for name, size in (("grow", ctx.attrs.grow), ("RAM", ctx.attrs.ram)):
        if size != None and not regex_match(SIZE_PATTERN, size):
            fail("image_vm: invalid {} size {!r}".format(name, size))
    if ctx.attrs.cpus != None and ctx.attrs.cpus < 1:
        fail("image_vm: cpus must be positive, got {}".format(ctx.attrs.cpus))
    for name in ctx.attrs.credentials:
        if not name or ":" in name:
            fail("image_vm: invalid credential name {!r}".format(name))
    for argument in ctx.attrs.cmdline_extra:
        if not argument:
            fail("image_vm: cmdline_extra arguments cannot be empty")

    linux, initrd, cmdline_file = ctx.attrs.linux, ctx.attrs.initrd, ctx.attrs.cmdline_file

    # A disk without an ESP has nothing for the firmware to boot, so its own kernel boots directly
    # unless the target names another.
    direct = ctx.attrs.image.get(DirectBootInfo)
    if direct != None:
        linux = linux if linux != None else direct.kernel
        initrd = initrd if initrd != None else direct.initrd
        cmdline_file = cmdline_file if cmdline_file != None else direct.cmdline

    image = _image(ctx.attrs.image, ctx.attrs.image_format)

    # Unset, Secure Boot brings a TPM for the expected-PCR policy to be measured into. Always say
    # which: left to itself, vmspawn attaches one whenever swtpm is installed, which the tools box does.
    tpm = ctx.attrs.tpm if ctx.attrs.tpm != None else ctx.attrs.secure_boot

    # The options that make the machine. An attribute that changes the machine belongs here, which both
    # the runner and VmInfo's spec take, so that a harness that starts the VM itself boots the same
    # machine as the runner. The credentials and the kernel command line stay apart, as a harness adds
    # its own to them.
    options = cmd_args(
        "--image",
        image,
        "--image-format={}".format(ctx.attrs.image_format),
        # Necessary rather than a default: the image is a build output, which the guest must not write.
        "--ephemeral",
        "--kvm=yes",
    )

    if linux != None:
        options.add(cmd_args(linux, format = "--linux={}"))
    if initrd != None:
        options.add(cmd_args(initrd, format = "--initrd={}"))

    if ctx.attrs.grow != None:
        # vmspawn creates the ephemeral overlay at this size, and leaves the qcow2 alone.
        options.add("--grow-image={}".format(ctx.attrs.grow))
    if ctx.attrs.ram != None:
        options.add("--ram={}".format(ctx.attrs.ram))
    if ctx.attrs.cpus != None:
        options.add("--cpus={}".format(ctx.attrs.cpus))

    if ctx.attrs.secure_boot:
        # OVMF variable store starts in setup mode and sd-boot enrolls the
        # image's loader/keys/auto keys on first boot.
        options.add("--secure-boot=yes")

    options.add("--tpm={}".format("yes" if tpm else "no"))

    if ctx.attrs.coco != None:
        options.add("--coco={}".format(ctx.attrs.coco))

    directories = {}
    for dep in ctx.attrs.extensions:
        extension = system_extension("image_vm", dep)
        for class_name, directory in (("sysext", "extensions"), ("confext", "confexts")):
            if class_name in extension.classes:
                directories.setdefault(directory, []).append(dep[DdiInfo].disk)
    for dep in ctx.attrs.portables:
        check_portable("image_vm", dep)
        directories.setdefault("portables", []).append(dep[DdiInfo].disk)

    for directory in sorted(directories):
        files = {}
        for disk in directories[directory]:
            # The guest gets each DDI as <image name>.raw, so that systemd finds it by that name.
            name = ddi_image_name(disk) + ".raw"
            if name in files:
                fail("image_vm: duplicate {} DDI {!r}".format(directory, name))
            files[name] = disk
        options.add(
            cmd_args(
                ctx.actions.copied_dir(directory, files),
                # The binds can't go below /run. vmspawn marks binds x-initrd.mount, so the initrd
                # mounts them at /sysroot/<target>. switch-root then moves the /run tmpfs of the
                # initrd onto /run of the new root, which hides a mount below /run.
                format = "--bind={{}}:/var/lib/{}".format(directory),
            ),
        )

    # After everything tine passes, so that an option vmspawn takes the last value of overrides it.
    options.add(ctx.attrs.vmspawn_args)

    # See VmInfo.
    spec = ctx.actions.declare_output("spec.json")
    ctx.actions.dynamic_output_new(
        _spec(
            cmdline_extra = ctx.attrs.cmdline_extra,
            cmdline_file = cmdline_file,
            fields = {
                "coco": ctx.attrs.coco,
                "credentials": ctx.attrs.credentials,
                "image": image,
                "linux": linux,
                "secure_boot": ctx.attrs.secure_boot,
                "tpm": tpm,
                "vmspawn_args": options,
            },
            spec = spec.as_output(),
        ),
    )

    if cmdline_file != None:
        run = cmd_args(
            box.run(box = ctx.attrs._box[box.Info], exe = "sh", relaxed = True),
            "-c",
            _CMDLINE_FILE_RUNNER,
            "sh",
            cmdline_file,
        )
    else:
        run = cmd_args(box.run(box = ctx.attrs._box[box.Info], exe = "systemd-vmspawn", relaxed = True))

    # The interactive options come first, so that the target's vmspawn_args override them, too.
    run.add("--console=native", "--network-user-mode")
    if ctx.attrs.autologin != None:
        if not ctx.attrs.autologin:
            fail("image_vm: autologin user cannot be empty")
        for name in ("agetty.autologin", "login.noauth", "passwd.hashed-password.root"):
            if name in ctx.attrs.credentials:
                fail("image_vm: credential {!r} is managed by autologin".format(name))
        run.add(
            "--set-credential=agetty.autologin:{}".format(ctx.attrs.autologin),
            "--set-credential=login.noauth:yes",
            "--set-credential=passwd.hashed-password.root:!*",
        )
    for name in sorted(ctx.attrs.credentials):
        run.add("--set-credential={}:{}".format(name, ctx.attrs.credentials[name]))
    run.add(options)

    # vmspawn takes extra kernel command line arguments as its own trailing arguments, and picks how
    # to deliver them from what it is booting: an SMBIOS OEM string each for the stub and the boot
    # loader for a UKI, `-append` for a bare kernel image. Handing it the arguments rather than the
    # OEM strings also lets it read them, which is how it knows not to add a root= of its own.
    run.add(ctx.attrs.cmdline_extra)
    return [
        DefaultInfo(sub_targets = {"spec": [DefaultInfo(default_output = spec, other_outputs = [options])]}),
        RunInfo(args = run),
        # Joined, so that a consumer's write_json renders it as a path rather than a list.
        VmInfo(spec = cmd_args(spec, delimiter = "", hidden = options)),
    ]

_image_vm = rule(
    impl = _image_vm_impl,
    attrs = {
        "autologin": attrs.option(
            attrs.string(),
            default = None,
            doc = "user to log in automatically without authentication",
        ),
        "cmdline_extra": attrs.list(
            attrs.string(),
            default = [],
            doc = "kernel command line arguments appended to the ones the image boots with",
        ),
        "cmdline_file": attrs.option(
            attrs.source(),
            default = None,
            doc = "file holding the kernel command line to boot `linux` with, such as a disk's `[boot][cmdline]`, its default for a disk without an ESP; `cmdline_extra` follows it",
        ),
        "coco": attrs.option(
            attrs.string(),
            default = None,
            doc = 'confidential computing technology to run the guest with, as vmspawn\'s --coco=, e.g. "sev-snp" or "tdx"',
        ),
        "cpus": attrs.option(
            attrs.int(),
            default = None,
            doc = "number of virtual CPUs exposed to the guest",
        ),
        "credentials": attrs.dict(
            key = attrs.string(),
            value = attrs.string(),
            default = {},
            doc = "non-secret system credentials passed to systemd-vmspawn",
        ),
        "extensions": attrs.list(
            attrs.dep(providers = [DdiInfo, ExtensionInfo]),
            default = [],
            doc = "extensions merged at boot, from /var/lib/extensions and /var/lib/confexts by class",
        ),
        "grow": attrs.option(
            attrs.string(),
            default = None,
            doc = 'size of the disk the guest sees, e.g. "8G", for more room than the virtual_size of the image',
        ),
        "image": attrs.dep(providers = [DdiInfo], doc = "the disk image the guest boots from, ephemerally"),
        "image_format": attrs.enum(
            ["qcow2", "raw"],
            default = "qcow2",
            doc = "which encoding of `image` the guest gets, as vmspawn's --image-format=",
        ),
        "initrd": attrs.option(
            attrs.source(),
            default = None,
            doc = "initrd for a direct kernel boot of `linux`, such as a disk's `[boot][initrd]`, its default for a disk without an ESP",
        ),
        "linux": attrs.option(
            attrs.source(),
            default = None,
            doc = "kernel to boot directly rather than through the image's ESP, such as a disk's `[boot][kernel]`, its default for a disk without an ESP",
        ),
        "portables": attrs.list(
            attrs.dep(providers = [DdiInfo]),
            default = [],
            doc = "portable service images and their extensions, exposed under /var/lib/portables",
        ),
        "ram": attrs.option(
            attrs.string(),
            default = None,
            doc = 'guest RAM size passed to vmspawn, e.g. "4G"',
        ),
        "secure_boot": attrs.bool(
            default = False,
            doc = "boot with Secure Boot capable firmware",
        ),
        "tpm": attrs.option(
            attrs.bool(),
            default = None,
            doc = "whether to attach a software TPM; unset, the guest has one with `secure_boot` and none without",
        ),
        "vmspawn_args": attrs.list(
            attrs.arg(),
            default = [],
            doc = "further systemd-vmspawn options of the machine, after the ones tine passes, so they can override those vmspawn takes the last value of",
        ),
        # The tools box of the image's release built the image and contains the VM stack. Booting the image
        # in that box builds no other box.
        "_box": distribution.TOOLS_BOX_ATTR,
    },
    supports_incoming_transition = True,
)

def image_vm(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare one, compatible with the distributions its package serves."""

    # The runner passes --kvm=yes to vmspawn, and KVM only runs guests of the host's architecture. A
    # disk of another architecture therefore cannot boot, and the runner is incompatible with it.
    compatible_with = ([] if target_compatible_with == None else target_compatible_with) + [host_configuration.cpu]
    _image_vm(name = name, **(distribution.distributed(name, distro, visibility, compatible_with) | kwargs))
