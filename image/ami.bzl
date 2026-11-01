# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Amazon Machine Images: a bootable disk with what EC2 needs to register it."""

load("//box:defs.bzl", "box")
load("//platform:architecture.bzl", "architecture")
load(":publish.bzl", "PublishedFile", "PublishedInfo")

# What `RegisterImage` accepts in an image name besides letters and digits.
_NAME_PUNCTUATION = "()[] ./-'@_"

# Owner of the Secure Boot signature list entries, all zeros as in the auto-enrollment files bootctl writes
# for the same key.
_SIGNATURE_OWNER = "00000000-0000-0000-0000-000000000000"

# EC2's label for the root volume, which ties its BlockDeviceMappings to the AMI's RootDeviceName. This
# is the conventional (but arbitrary) value of most Linux AMIs. Does not appear in guests, that's
# `/dev/nvme<N>n1` on Nitro instances.
_ROOT_DEVICE = "/dev/xvda"

def _ami_name(basename: str) -> str:
    return "".join([c if c.isalnum() or c in _NAME_PUNCTUATION else "-" for c in basename.elems()])

def _gib(size: str) -> int:
    """`size`, a SIZE_PATTERN string read base-1024 as systemd does, in GiB rounded up."""
    exponent = "KMGTPE".find(size[-1]) + 1
    number = float(size[:-1] if exponent else size)

    # Starlark has no math.ceil; flooring the negation rounds up.
    return int(-(-number * (1 << 10 * exponent) // (1 << 30)))

def declare_ami(
    actions: AnalysisActions,
    *,
    arch: str,
    basename: str,
    box_info: box.Info,
    disk: Artifact,
    importer: RunInfo,
    secure_boot_certificate: Artifact | None,
    version: str,
    virtual_size: str | None,
) -> list[Provider]:
    """Declare the AMI RegisterImage parameters of a bootable disk, for its `[ami]` subtarget.

    `aws ec2 register-image --cli-input-json` takes them as they are once the import adds the snapshot.
    The subtarget publishes them as `<basename>.ami.json`, so that a release carries everything an import
    needs besides the disk. Running the subtarget imports the disk with `importer`. With a
    `secure_boot_certificate`, the parameters include the UEFI variable store that enrolls it; `box_info`
    provides the tool that writes it.
    """
    register = {
        "Architecture": architecture.ARCHITECTURES[arch].ec2,
        "BlockDeviceMappings": [
            {
                "DeviceName": _ROOT_DEVICE,
                "Ebs": {"DeleteOnTermination": True, "VolumeType": "gp3"},
            },
        ],
        # Not "uefi-preferred": instance types without UEFI would boot the disk in legacy BIOS mode.
        "BootMode": "uefi",
        "Description": basename,
        "EnaSupport": True,
        "ImdsSupport": "v2.0",
        "Name": _ami_name(basename),
        "RootDeviceName": _ROOT_DEVICE,
        # Limits the AMI to instance types with a NitroTPM, which leaves out bare metal ones.
        "TpmSupport": "v2.0",
        "VirtualizationType": "hvm",
    }

    # Without it, the volume is as large as the snapshot, which is as large as the disk.
    if virtual_size != None:
        register["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] = _gib(virtual_size)

    name = "{}.ami.json".format(basename)
    if secure_boot_certificate == None:
        parameters = actions.write_json(name, register)
    else:
        parameters = actions.declare_output(name)
        actions.dynamic_output_new(
            _with_uefi_data(
                out = parameters.as_output(),
                register = register,
                uefi_data = _uefi_data(actions, box_info, secure_boot_certificate),
            ),
        )
    return [
        # Building an AMI builds the disk, as for any other format; only the parameters are published,
        # as a release carries the disk already.
        DefaultInfo(default_output = parameters, other_outputs = [disk]),
        PublishedInfo(artifacts = {name: PublishedFile(artifact = parameters, kind = "ami")}, image = basename, version = version),
        RunInfo(args = cmd_args(importer, disk, parameters)),
    ]

def _with_uefi_data_impl(
    actions: AnalysisActions,
    out: OutputArtifact,
    register: dict[str, typing.Any],
    uefi_data: ArtifactValue,
) -> list[Provider]:
    # The store is base64 already, which is what RegisterImage takes.
    actions.write_json(out, register | {"UefiData": uefi_data.read_string().strip()})
    return []

_with_uefi_data = dynamic_actions(
    impl = _with_uefi_data_impl,
    attrs = {
        "out": dynattrs.output(),
        "register": dynattrs.value(dict[str, typing.Any]),
        "uefi_data": dynattrs.artifact_value(),
    },
)

def _uefi_data(actions: AnalysisActions, box_info: box.Info, certificate: Artifact) -> Artifact:
    """The instance's initial UEFI variable store, which trusts `certificate` as PK, KEK, and db.

    Without it, an instance boots in Secure Boot setup mode, and the first thing it boots may enroll
    whichever keys it likes.
    """
    uefi_data = actions.declare_output("ami-uefi-data")
    actions.run(
        cmd_args(
            box.run(box = box_info),
            "virt-fw-vars",
            "--set-pk",
            _SIGNATURE_OWNER,
            certificate,
            "--add-kek",
            _SIGNATURE_OWNER,
            certificate,
            "--add-db",
            _SIGNATURE_OWNER,
            certificate,
            "--output-aws",
            uefi_data.as_output(),
        ),
        category = "uefi_data",
    )
    return uefi_data
