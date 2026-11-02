# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public image API.

Load the `image` namespace from here; the modules behind it are implementation structure and may be
rearranged.
"""

load("//kmod:initrd_modules.toml", _INITRD_MODULES = "value")
load(
    ":archive.bzl",
    "COMPRESSIONS",
    "ImageArchiveInfo",
    "ImageDirectoryInfo",
    "image_directory",
)
load(":boot.bzl", "UkiInfo", "bootable", "uki", "uki_profile")
load(
    ":compose.bzl",
    "InitrdInfo",
    "archive",
    "confext",
    "disk",
    "extension",
    "initrd_image",
    "portable",
    "sysext",
)
load(
    ":ddi.bzl",
    "DEFAULT_ROOT_PARTITIONS",
    "DEFAULT_SIGNED_USR_VERITY_PARTITIONS",
    "DEFAULT_USR_VERITY_PARTITIONS",
    "DISK_FORMATS",
    "DdiInfo",
    "DiskConversionInfo",
    "RootHashInfo",
    "partition",
)
load(":extension.bzl", "ExtensionInfo")
load(
    ":image.bzl",
    "ImageInfo",
    "ImageInstallInfo",
    "ImageSbomInfo",
    "image_install",
    "layer",
    "ops",
)
load(":publish.bzl", "PublishedFile", "PublishedInfo", "image_artifacts")
load(
    ":sign.bzl",
    "SigningKeyInfo",
    "generate_signing_key",
    "pem_signing_key",
    "pkcs11_signing_key",
)
load(":substitute.bzl", "substitute")
load(":version.bzl", "resolve_version")
load(":vm.bzl", "image_vm")

image = struct(
    # Logical images and the operations that build them.
    layer = layer,
    ops = ops,
    # Terminal outputs.
    directory = image_directory,
    uki = uki,
    uki_profile = uki_profile,
    resolve_version = resolve_version,
    bootable = bootable,
    partition = partition,
    vm = image_vm,
    generate_signing_key = generate_signing_key,
    install = image_install,
    substitute = substitute,
    pem_signing_key = pem_signing_key,
    pkcs11_signing_key = pkcs11_signing_key,
    SigningKeyInfo = SigningKeyInfo,
    # Compositions.
    archive = archive,
    sysext = sysext,
    confext = confext,
    extension = extension,
    portable = portable,
    disk = disk,
    artifacts = image_artifacts,
    initrd = initrd_image,
    # The core kernel modules a UKI carries unless a target says otherwise; extend it with `+`.
    DEFAULT_INITRD_MODULES = _INITRD_MODULES["patterns"],
    # Conventional partition layouts.
    DEFAULT_ROOT_PARTITIONS = DEFAULT_ROOT_PARTITIONS,
    DEFAULT_USR_VERITY_PARTITIONS = DEFAULT_USR_VERITY_PARTITIONS,
    DEFAULT_SIGNED_USR_VERITY_PARTITIONS = DEFAULT_SIGNED_USR_VERITY_PARTITIONS,
    COMPRESSIONS = COMPRESSIONS,
    DISK_FORMATS = DISK_FORMATS,
    # Providers.
    ImageInfo = ImageInfo,
    ImageInstallInfo = ImageInstallInfo,
    ImageSbomInfo = ImageSbomInfo,
    ImageArchiveInfo = ImageArchiveInfo,
    ImageDirectoryInfo = ImageDirectoryInfo,
    ExtensionInfo = ExtensionInfo,
    UkiInfo = UkiInfo,
    DdiInfo = DdiInfo,
    RootHashInfo = RootHashInfo,
    DiskConversionInfo = DiskConversionInfo,
    InitrdInfo = InitrdInfo,
    PublishedInfo = PublishedInfo,
    # The record PublishedInfo keys by published name.
    PublishedFile = PublishedFile,
)
