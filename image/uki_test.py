# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the optional ukify arguments.

    buck test tine//image:test

Secure Boot signing and expected-PCR sealing are two independent keys, each held in the build graph
or in a token, and profiles can opt out of the policy, so one image build exercises one combination.
Integration tests only cover a few combinations; the rest are asserted here.
"""

import contextlib
import tempfile
import unittest
from pathlib import Path
from typing import override

import cpio
import uki

SECURE_BOOT: uki.Key = {
    "private_key": "/keys/sb.key",
    "certificate": "/keys/sb.crt",
    "private_key_source": None,
    "certificate_source": None,
}
PCR: uki.Key = {
    "private_key": "/keys/pcr.key",
    "certificate": "/keys/pcr.crt",
    "private_key_source": None,
    "certificate_source": None,
}

SECURE_BOOT_ARGUMENTS = [
    "--signtool", "systemd-sbsign",
    "--secureboot-private-key", "/keys/sb.key",
    "--secureboot-certificate", "/keys/sb.crt",
    "--sign-kernel",
]  # fmt: skip

PCR_ARGUMENTS = [
    "--pcr-banks", "sha256",
    "--pcr-private-key", "/keys/pcr.key",
    "--sign-initrd-pcrs",
]  # fmt: skip

# What the driver found this ukify accepts. Only what a tool takes is passed to it, so the same
# build works either side of an option's arrival.
OPTIONS = {"--sign-initrd-pcrs"}


def _token(key: uki.Key, source: str = "provider:pkcs11", certificate: bool = True) -> uki.Key:
    """The same role held in a PKCS#11 token, as pkcs11_signing_key() spells it.

    Without `certificate` the token holds only the private key, which is the arrangement a key
    given a PEM certificate of its own has.
    """
    return {**key, "private_key_source": source, "certificate_source": source if certificate else None}


def _profiles(*ids: str, unsealed: str = "") -> list[uki.Profile]:
    """Profiles by id, `unsealed` naming the one that opts out of the expected-PCR policy."""
    return [
        {"id": id, "title": id, "cmdline": [], "sign_expected_pcr": id != unsealed, "fields": {}}
        for id in ids
    ]


class TestProfileSection(unittest.TestCase):
    def test_fields_follow_id_and_title(self) -> None:
        # A profile with extra fields must write ID and TITLE first and then each field as
        # its own KEY=VALUE line, in the order the profile declares them.
        profile: uki.Profile = {
            "id": "Profile2",
            "title": "Profile Two",
            "cmdline": [],
            "sign_expected_pcr": True,
            "fields": {"PCRLOCK": "yes", "TOFU": "test.tofu"},
        }
        self.assertEqual(
            uki._profile_section(profile, {}),
            "ID=Profile2\nTITLE=Profile Two\nPCRLOCK=yes\nTOFU=test.tofu\n",
        )

    def test_own_fields_override_shared_fields(self) -> None:
        # A profile must be able to override a field that the UKI sets for every profile, for example
        # to opt out of TOFU= with an empty token. Write a profile whose own TOFU= is empty, with a
        # shared TOFU= and PCRLOCK=, and check that the section has a single, empty TOFU= line.
        profile: uki.Profile = {
            "id": "install",
            "title": "Install",
            "cmdline": [],
            "sign_expected_pcr": True,
            "fields": {"TOFU": ""},
        }
        self.assertEqual(
            uki._profile_section(profile, {"TOFU": "com.example.os", "PCRLOCK": "yes"}),
            "ID=install\nTITLE=Install\nTOFU=\nPCRLOCK=yes\n",
        )

    def test_base_profile_is_main(self) -> None:
        # The base profile must have the id main, which ukify selects it by with --sign-profile, and no
        # TITLE=. Build the section from the shared fields and check that ID=main comes first, followed
        # by each field as its own KEY=VALUE line.
        self.assertEqual(
            uki._base_profile_section({"TOFU": "com.example.os", "PCRLOCK": "yes"}),
            "ID=main\nTOFU=com.example.os\nPCRLOCK=yes\n",
        )


class TestSigningArguments(unittest.TestCase):
    def test_unsigned(self) -> None:
        self.assertEqual(uki._signing_arguments(None, None, [], OPTIONS), [])

    def test_secure_boot_alone(self) -> None:
        self.assertEqual(uki._signing_arguments(SECURE_BOOT, None, [], OPTIONS), SECURE_BOOT_ARGUMENTS)

    def test_expected_pcr_alone(self) -> None:
        """The driver accepts an expected-PCR key alone. declare_uki() requires a Secure Boot key with it."""
        self.assertEqual(uki._signing_arguments(None, PCR, [], OPTIONS), PCR_ARGUMENTS)

    def test_the_initrd_policy_rides_along_with_the_expected_pcr_key(self) -> None:
        """NvPCR initialization only accepts a signature bound to the "initrd" policy reference."""
        self.assertIn("--sign-initrd-pcrs", uki._signing_arguments(SECURE_BOOT, PCR, [], OPTIONS))
        self.assertNotIn("--sign-initrd-pcrs", uki._signing_arguments(SECURE_BOOT, None, [], OPTIONS))

    def test_a_ukify_that_cannot_sign_the_initrd_policy(self) -> None:
        """Passing an option an older ukify does not know would fail the build instead."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, [], set()),
            ["--pcr-banks", "sha256", "--pcr-private-key", "/keys/pcr.key"],
        )

    def test_both_roles(self) -> None:
        self.assertEqual(
            uki._signing_arguments(SECURE_BOOT, PCR, [], OPTIONS), SECURE_BOOT_ARGUMENTS + PCR_ARGUMENTS
        )

    def test_profiles_are_sealed_by_default(self) -> None:
        """ukify signs every profile by default, so the common case passes no --sign-profile at all."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, _profiles("dev", "rescue"), OPTIONS), PCR_ARGUMENTS
        )

    def test_one_profile_opting_out_signs_all_the_others(self) -> None:
        """--sign-profile whitelists what is sealed, so opting one out signs the base and rest by name."""
        self.assertEqual(
            uki._signing_arguments(None, PCR, _profiles("dev", "rescue", unsealed="rescue"), OPTIONS),
            [*PCR_ARGUMENTS, "--sign-profile", "main", "--sign-profile", "dev"],
        )

    def test_profiles_without_the_key_seal_nothing(self) -> None:
        """The profiles the test above signs by name yield nothing when no key seals them."""
        self.assertEqual(
            uki._signing_arguments(
                SECURE_BOOT, None, _profiles("dev", "rescue", unsealed="rescue"), OPTIONS
            ),
            SECURE_BOOT_ARGUMENTS,
        )

    def test_a_token_holds_the_secure_boot_key(self) -> None:
        """The URIs stay the key and certificate; the provider is what loads them."""
        self.assertEqual(
            uki._signing_arguments(_token(SECURE_BOOT), None, [], OPTIONS),
            [*SECURE_BOOT_ARGUMENTS, "--signing-provider", "pkcs11", "--certificate-provider", "pkcs11"],
        )

    def test_a_token_holds_the_expected_pcr_key(self) -> None:
        """systemd-measure cannot derive .pcrpkey from a key it does not hold, so it takes the cert."""
        self.assertEqual(
            uki._signing_arguments(None, _token(PCR), [], OPTIONS),
            [*PCR_ARGUMENTS, "--pcr-certificate", "/keys/pcr.crt"],
        )

    def test_a_token_holds_the_secure_boot_key_beside_a_file_certificate(self) -> None:
        """Each half names its own provider, so a certificate in a file passes none at all."""
        self.assertEqual(
            uki._signing_arguments(_token(SECURE_BOOT, certificate=False), None, [], OPTIONS),
            [*SECURE_BOOT_ARGUMENTS, "--signing-provider", "pkcs11"],
        )

    def test_a_key_source_ukify_cannot_load(self) -> None:
        """ukify only speaks providers, so anything else must fail the build, not the boot."""
        with self.assertRaises(SystemExit):
            uki._signing_arguments(_token(SECURE_BOOT, "box:pkcs11"), None, [], OPTIONS)


class TestSplashArguments(unittest.TestCase):
    """A source is a path Buck handed the driver; an absolute path is a file in the mounted image."""

    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.tree = self.scratch / "buildroot"
        (self.scratch / "boot.bmp").write_bytes(b"BM" + bytes(52))
        self._ship("usr/share/pixmaps/splash.bmp", b"BM" + bytes(52))

    def _ship(self, path: str, content: bytes) -> None:
        file = self.tree / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)

    def test_no_splash(self) -> None:
        self.assertEqual(uki._splash_arguments(None, self.tree), [])

    def test_a_source_passes_through(self) -> None:
        """Buck hands the driver a project-relative path, run at the project root."""
        with contextlib.chdir(self.scratch):
            self.assertEqual(uki._splash_arguments("boot.bmp", self.tree), ["--splash", "boot.bmp"])

    def test_an_image_path_resolves_into_the_mounted_tree(self) -> None:
        self.assertEqual(
            uki._splash_arguments("/usr/share/pixmaps/splash.bmp", self.tree),
            ["--splash", str(self.tree / "usr/share/pixmaps/splash.bmp")],
        )

    def test_an_image_path_the_image_does_not_ship(self) -> None:
        """Named here rather than by ukify, which would only report an argument it cannot open."""
        with self.assertRaises(SystemExit) as raised:
            uki._splash_arguments("/usr/share/pixmaps/missing.bmp", self.tree)
        self.assertIn("/usr/share/pixmaps/missing.bmp", str(raised.exception))

    def test_a_splash_that_is_not_a_bmp(self) -> None:
        """ukify embeds any bytes it is given; the stub would then show nothing at boot."""
        self._ship("usr/share/pixmaps/logo.png", b"\x89PNG\r\n\x1a\n" + bytes(52))
        with self.assertRaises(SystemExit) as raised:
            uki._splash_arguments("/usr/share/pixmaps/logo.png", self.tree)
        self.assertIn("not a BMP", str(raised.exception))


class TestMicrocode(unittest.TestCase):
    """The early loader reads one concatenated file per vendor off the head of an uncompressed cpio."""

    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.tree = self.scratch / "buildroot"

    def _ship(self, path: str, content: bytes) -> None:
        file = self.tree / "usr/lib/firmware" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)

    def _entries(self, archive: Path) -> dict[str, bytes]:
        data = archive.read_bytes()
        return {entry.name: data[entry.data_off : entry.data_off + entry.size] for entry in cpio.read(data)}

    def test_an_image_without_microcode(self) -> None:
        self._ship("amdgpu/vega.bin", b"gpu")
        self.assertIsNone(uki._microcode(self.tree, self.scratch, 0, "x64"))

    def test_an_empty_vendor_directory_ships_nothing(self) -> None:
        (self.tree / "usr/lib/firmware/intel-ucode").mkdir(parents=True)
        self.assertIsNone(uki._microcode(self.tree, self.scratch, 0, "x64"))

    def test_only_x86_loads_microcode_this_way(self) -> None:
        self._ship("amd-ucode/microcode_amd.bin", b"amd")
        self.assertIsNone(uki._microcode(self.tree, self.scratch, 0, "aa64"))

    def test_each_vendor_gets_the_concatenation_of_its_blobs(self) -> None:
        self._ship("amd-ucode/microcode_amd_fam19h.bin", b"fam19")
        self._ship("amd-ucode/microcode_amd_fam17h.bin", b"fam17")
        self._ship("intel-ucode/06-8e-09", b"kaby")
        self._ship("intel-ucode/06-a5-02", b"comet")
        archive = uki._microcode(self.tree, self.scratch, 0, "x64")
        assert archive is not None
        self.assertEqual(
            self._entries(archive),
            {
                "kernel": b"",
                "kernel/x86": b"",
                "kernel/x86/microcode": b"",
                # Sorted by name, so the archive is the same whatever order the directory lists.
                "kernel/x86/microcode/AuthenticAMD.bin": b"fam17fam19",
                "kernel/x86/microcode/GenuineIntel.bin": b"kabycomet",
            },
        )

    def test_the_names_are_not_padded(self) -> None:
        """The kernel's early cpio reader allows 18 bytes past the directory for a name, padding included."""
        name = "kernel/x86/microcode/GenuineIntel.bin"
        self._ship("intel-ucode/06-8e-09", bytes(2 * cpio._BLOCK))
        archive = uki._microcode(self.tree, self.scratch, 0, "x64")
        assert archive is not None
        data = archive.read_bytes()
        entry = next(entry for entry in cpio.read(data) if entry.name == name)
        header = data.rfind(cpio.MAGIC, 0, entry.data_off)
        self.assertEqual(int(data[header + 6 + 11 * 8 : header + 6 + 12 * 8], 16), len(name) + 1)
