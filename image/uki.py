#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build the unified kernel image for a logical filesystem image's single kernel.

Box tools operate on the mounted image without chrooting. A kernel-modules cpio
extends the supplied base initrds.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

import specs
from util import fail

import cpio
import finalize
import kmod

# The kernel's early loader (arch/x86/kernel/cpu/microcode) looks for one file per CPU vendor at this
# path, named after the vendor string cpuid reports.
MICROCODE = PurePosixPath("kernel/x86/microcode")
MICROCODE_VENDORS = {"AuthenticAMD": "amd-ucode", "GenuineIntel": "intel-ucode"}
# ukify's EFI architectures whose kernels load microcode from the initrd.
_X86 = ("ia32", "x64")


class Profile(TypedDict):
    id: str
    title: str
    cmdline: list[str]
    sign_expected_pcr: bool


class RootHash(TypedDict):
    path: str
    kind: str


class Key(TypedDict):
    private_key: str
    certificate: str
    # OpenSSL sources in systemd's spelling, each None for material in the build graph.
    private_key_source: str | None
    certificate_source: str | None


class Spec(finalize.ImageSpec):
    out: str
    # Base initrd cpios, in load order.
    initrds: list[str]
    # Patterns selecting the modules the per-kernel initrd carries, and the report of what it got.
    initrd_modules: list[str]
    modules_manifest: str
    cmdline: list[str]
    profiles: list[Profile]
    root_hash: RootHash | None
    # ukify's EFI architecture (e.g. x64) and systemd's spelling in the UKI name (e.g. x86-64).
    efi_arch: str
    systemd_arch: str
    image_id: str
    version: str
    secure_boot: Key | None
    # Seals the expected-PCR policy; None leaves it unsealed.
    sign_expected_pcr: Key | None
    # BMP displayed by the EFI stub while booting, a build artifact or an absolute path into the
    # image; None embeds no splash section.
    splash: str | None


def _kernels(tree: Path) -> list[kmod.Kernel]:
    found = kmod.kernels(tree)
    if not found:
        fail("uki: found no kernel under /usr/lib/modules or /boot")
    return sorted(found)


def _select(tree: Path, kver: str, patterns: list[str]) -> kmod.Selection:
    """Resolve the module patterns, reporting what the image could not satisfy."""
    selection = kmod.initrd_modules(tree, kver, patterns)
    # Counts here, names in the manifest: this stays readable when a broad pattern list meets a kernel
    # that ships a fraction of it, and the manifest is the artifact one debugs a bad UKI from.
    if selection.unmatched:
        print(f"uki: {len(selection.unmatched)} patterns match no module in this image", file=sys.stderr)
    if selection.missing_firmware:
        print(
            f"uki: {len(selection.missing_firmware)} firmware files the selected modules declare are "
            "not installed",
            file=sys.stderr,
        )
    # Named, because a dependency the image does not install is a hole in the initrd, not a mismatch
    # between one list and one kernel.
    for name in selection.missing:
        print(f"uki: {name} is required by the selection but the image does not install it", file=sys.stderr)
    return selection


def _cmdline(arguments: list[str], root_hash: Path | None, kind: str | None) -> str:
    if root_hash is None:
        return " ".join(arguments)
    assert kind is not None
    parameter = f"{kind}hash"
    if any(word.split("=", 1)[0] == parameter for argument in arguments for word in argument.split()):
        fail(f"uki: {parameter}= is both explicit and generated")
    digest = root_hash.read_text().strip()
    if not digest or any(character not in "0123456789abcdefABCDEF" for character in digest):
        fail("uki: invalid verity root hash")
    return " ".join([*arguments, f"{parameter}={digest.lower()}"])


def _provider(source: str) -> str:
    """The provider in a source ukify can load through.

    ukify names a provider rather than taking systemd's `provider:<name>` source, and translates it
    for the systemd-sbsign and systemd-measure calls it makes.
    """
    prefix = "provider:"
    if not source.startswith(prefix):
        fail(f"uki: ukify can only load key material through an OpenSSL provider, got {source!r}")
    return source.removeprefix(prefix)


def _provider_options(key: Key) -> list[str]:
    """ukify's spelling for the halves of a key it loads through an OpenSSL provider.

    Each half names its own provider, so a private key in a token whose certificate is a file
    passes only the one option, and ukify reads the certificate the way it reads any file.
    """
    options = []
    if key["private_key_source"]:
        options += ["--signing-provider", _provider(key["private_key_source"])]
    if key["certificate_source"]:
        options += ["--certificate-provider", _provider(key["certificate_source"])]
    return options


def _ukify_options() -> set[str]:
    """Which options this ukify accepts.

    The measured-boot policy a UKI carries is only useful to a systemd that speaks the same
    dialect, and the box's ukify comes from one generation of systemd while its options come and
    go with it. Asking what this one takes keeps a build working across both sides of a change,
    rather than pinning tine to whichever generation the distributions have caught up to.
    """
    help = subprocess.run(["ukify", "build", "--help"], check=True, capture_output=True, text=True)
    return {word for word in re.findall(r"--[a-z0-9-]+", help.stdout)}


def _splash_arguments(splash: str | None, tree: Path) -> list[str]:
    """ukify's argument for the splash: a build artifact as is, an absolute path inside the image."""
    if not splash:
        return []
    if splash.startswith("/"):
        path = tree / splash.lstrip("/")
        if not path.is_file():
            fail(f"uki: the image ships no splash at {splash}")
    else:
        path = Path(splash)
    # ukify embeds whatever it is handed (with Pillow importable it only refuses a non-BMP), and the
    # stub renders BMP alone, so anything else would surface as a blank screen at boot.
    with path.open("rb") as file:
        if file.read(2) != b"BM":
            fail(f"uki: splash {splash} is not a BMP, the only format the EFI stub displays")
    return ["--splash", str(path)]


def _microcode(tree: Path, scratch: Path, epoch: int, efi_arch: str) -> Path | None:
    """The microcode cpio for the image's CPU vendors, None when it ships microcode for none.

    Every blob a vendor's firmware directory holds goes into that vendor's file, concatenated: the
    early loader picks the one matching the running CPU out of the concatenation, and which CPU
    that is cannot be known at build time. The archive stays uncompressed, because the loader reads
    it before anything decompresses the initrd.
    """
    if efi_arch not in _X86:
        return None
    root = scratch / "microcode"
    destination = root / MICROCODE
    packed = []
    for vendor, directory in sorted(MICROCODE_VENDORS.items()):
        blobs = sorted(path for path in (tree / kmod.FIRMWARE / directory).glob("*") if path.is_file())
        if not blobs:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / f"{vendor}.bin").open("wb") as out:
            for blob in blobs:
                out.write(blob.read_bytes())
        packed.append(vendor)
    if not packed:
        return None
    # The modes are the archive's, so they cannot be left to the umask this happens to run under.
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    archive = scratch / "microcode.cpio"
    cpio.pack_tree(root, archive, epoch)
    print(f"uki: microcode for {', '.join(packed)} in {archive.stat().st_size // 1024} KiB", file=sys.stderr)
    return archive


def _signing_arguments(
    secure_boot: Key | None,
    pcr: Key | None,
    profiles: list[Profile],
    options: set[str],
) -> list[str]:
    """ukify's arguments for the two independent signing roles, empty for an unsigned UKI.

    The Secure Boot key signs the UKI and the kernel in it, the expected-PCR key seals the policy
    the booted system unseals its secrets against. Either can be absent.
    """
    signing: list[str] = []
    if secure_boot:
        signing = [
            "--signtool", "systemd-sbsign",
            "--secureboot-private-key", secure_boot["private_key"],
            "--secureboot-certificate", secure_boot["certificate"],
            "--sign-kernel",
        ]  # fmt: skip
        signing += _provider_options(secure_boot)
    # ukify measures and signs the base and each joined profile separately. All profiles are
    # signed by default; the explicit --sign-profile list is only needed when one opts out. The
    # base profile is named "main": ukify defaults it to that when anything is joined.
    if pcr:
        signing += [
            "--pcr-banks", "sha256",
            "--pcr-private-key", pcr["private_key"],
        ]  # fmt: skip
        # NvPCRs are initialized from the initrd, against a policy bound to the "initrd" policy
        # reference; a signature without that reference does not authorize the write, whatever it
        # covers. Ask for the second signing pass that produces one, so that an image shipping
        # /usr/lib/nvpcr definitions gets past the boot's first TPM step.
        if "--sign-initrd-pcrs" in options:
            signing += ["--sign-initrd-pcrs"]
        if pcr["private_key_source"]:
            # Only the provider path passes the certificate: ukify's systemd-measure call requires
            # one there. Everywhere else ukify derives the public key section (.pcrpkey) from the
            # private key, which cannot mismatch, and passing the certificate instead would be a
            # risk: nothing checks that certificate and key match, and a mismatch poisons .pcrpkey
            # and the sealed policy without failing the build, surfacing only at unsealing.
            signing += ["--pcr-certificate", pcr["certificate"]]
        if not all(profile["sign_expected_pcr"] for profile in profiles):
            signing += ["--sign-profile", "main"]
            for profile in profiles:
                if profile["sign_expected_pcr"]:
                    signing += ["--sign-profile", profile["id"]]
    return signing


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "uki", argv)

    efi_arch = spec["efi_arch"]
    out = Path(spec["out"])
    out.mkdir(parents=True)
    initrds = [Path(initrd) for initrd in spec["initrds"]]
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    secure_boot = spec["secure_boot"]
    pcr = spec["sign_expected_pcr"]

    with (
        finalize.image(spec, program="uki") as tree,
        tempfile.TemporaryDirectory(prefix="boot.") as scratch_dir,
    ):
        scratch = Path(scratch_dir)
        found = _kernels(tree)
        if len(found) > 1:
            fail(
                "uki: one image holds one kernel, found: "
                + ", ".join(installed.release for installed in found)
                + " — split kernel variants into separate images"
            )
        kver, kernel = found[0]
        os_release = tree / "usr/lib/os-release"
        if not os_release.exists():
            fail("uki: the image ships no /usr/lib/os-release (ukify needs it) — install a release package")
        stub = tree / "usr/lib/systemd/boot/efi" / f"linux{efi_arch}.efi.stub"
        if not stub.exists():
            fail("uki: the image ships no systemd-boot stub — install systemd-boot-unsigned")

        root_hash = spec["root_hash"]
        base = _cmdline(
            spec["cmdline"],
            Path(root_hash["path"]) if root_hash else None,
            root_hash["kind"] if root_hash else None,
        )
        cmdline = scratch / "cmdline"
        cmdline.write_text(base + "\x00")

        # Each profile becomes a small PE of .profile and .cmdline sections, joined into every
        # UKI below; the profile arguments extend the shared base cmdline.
        profiles = spec["profiles"]
        profile_pes = []
        addon_stub = tree / "usr/lib/systemd/boot/efi" / f"addon{efi_arch}.efi.stub"
        if profiles and not addon_stub.exists():
            fail("uki: the image ships no addon stub — install systemd-boot-unsigned")
        for profile in profiles:
            section = scratch / f"{profile['id']}.profile"
            section.write_text(f"ID={profile['id']}\nTITLE={profile['title']}\n")
            profile_cmdline = scratch / f"{profile['id']}.cmdline"
            profile_cmdline.write_text(" ".join(([base] if base else []) + profile["cmdline"]) + "\x00")
            pe = scratch / f"{profile['id']}.efi"
            cmd = [
                "ukify", "build",
                "--profile", f"@{section}",
                "--cmdline", f"@{profile_cmdline}",
                "--stub", str(addon_stub),
                "--efi-arch", efi_arch,
                "--output", str(pe),
            ]  # fmt: skip
            subprocess.run(cmd, check=True)
            profile_pes.append(pe)

        signing = _signing_arguments(secure_boot, pcr, profiles, _ukify_options() if pcr else set())

        modules = scratch / f"modules-{kver}.cpio"
        selection = _select(tree, kver, spec["initrd_modules"])
        cpio.pack_paths(tree, modules, epoch, [entry.path for entry in selection.entries])
        Path(spec["modules_manifest"]).write_text(
            kmod.manifest(selection, archive_bytes=modules.stat().st_size), encoding="utf-8"
        )
        print(
            f"uki: {kver}: {len(selection.modules)} modules and {len(selection.firmware)} firmware "
            f"files in {modules.stat().st_size // 1024} KiB",
            file=sys.stderr,
        )

        output = out / f"{spec['image_id']}_{spec['version']}_{spec['systemd_arch']}.efi"
        cmd = ["ukify", "build", "--linux", str(kernel)]
        for initrd in [*initrds, modules]:
            cmd += ["--initrd", str(initrd)]
        # A .ucode section, which the stub hands the kernel ahead of every initrd, as the early loader
        # needs it. Every distribution tine pins ships a ukify and a stub that know the section.
        if microcode := _microcode(tree, scratch, epoch, efi_arch):
            cmd += ["--microcode", str(microcode)]
        cmd += [
            "--cmdline", f"@{cmdline}",
            *(argument for pe in profile_pes for argument in ("--join-profile", str(pe))),
            "--os-release", f"@{os_release}",
            "--uname", kver,
            "--stub", str(stub),
            "--efi-arch", efi_arch,
            *_splash_arguments(spec["splash"], tree),
            *signing,
            "--output", str(output),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
    print(f"uki: built {output.name} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
