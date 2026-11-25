# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The specs that image.vm hands out.

See VmInfo in image/vm.bzl. The environment names the `[spec]` of each VM.
"""

import json
import os
import unittest
from pathlib import Path
from typing import Any

_FIELDS = {"cmdline", "coco", "credentials", "image", "linux", "secure_boot", "tpm", "vmspawn_args"}


class VmSpecTest(unittest.TestCase):
    def spec(self, variable: str) -> dict[str, Any]:
        """Return the spec that `variable` names, after asserting its fields."""
        spec: dict[str, Any] = json.loads(Path(os.environ[variable]).read_text())
        self.assertEqual(set(spec), _FIELDS)
        return spec

    def assert_build_output(self, path: str, suffix: str) -> None:
        """Assert that `path` names a built output ending in `suffix`, relative to the project root."""
        self.assertTrue(path.startswith("buck-out/"), path)
        self.assertTrue(path.endswith(suffix), path)
        self.assertTrue(Path(path).exists(), path)

    def assert_image(self, spec: dict[str, Any]) -> None:
        """Assert that the `spec`'s options start with its qcow2 disk, booted ephemerally."""
        self.assert_build_output(spec["image"], ".qcow2")
        self.assertEqual(
            spec["vmspawn_args"][:5],
            ["--image", spec["image"], "--image-format=qcow2", "--ephemeral", "--kvm=yes"],
        )

    def test_boot_from_esp(self) -> None:
        spec = self.spec("BOOT_DEMO_VM")
        self.assertIsNone(spec["coco"])
        self.assertIsNone(spec["linux"])
        self.assertFalse(spec["secure_boot"])
        self.assertFalse(spec["tpm"])
        self.assertEqual(spec["credentials"], {"firstboot.locale": "C.UTF-8", "firstboot.timezone": "UTC"})
        self.assertEqual(spec["cmdline"], ["tine.smoke=1"])

        self.assert_image(spec)
        args = spec["vmspawn_args"]
        self.assertEqual(args[5:7], ["--ram=2G", "--tpm=no"])
        for arg, name in zip(args[7:10], ("confexts", "extensions", "portables"), strict=True):
            self.assertTrue(arg.startswith("--bind="), arg)
            directory, target = arg.removeprefix("--bind=").split(":")
            self.assertEqual(target, "/var/lib/" + name)
            self.assert_build_output(directory, "/" + name)
        # The target's vmspawn_args follow tine's options.
        self.assertEqual(args[10:], ["--smbios11=tine.vmspawn-args=1"])

    def test_secure_boot_brings_a_tpm(self) -> None:
        spec = self.spec("SECUREBOOT_VM")
        self.assertTrue(spec["secure_boot"])
        self.assertTrue(spec["tpm"])
        self.assertEqual(spec["vmspawn_args"][5:7], ["--secure-boot=yes", "--tpm=yes"])

    def test_direct_boot(self) -> None:
        spec = self.spec("BOOT_DEMO_VM_DIRECT")
        self.assert_build_output(spec["linux"], "/vmlinuz")
        self.assertEqual(spec["credentials"], {"firstboot.locale": "C.UTF-8", "firstboot.timezone": "UTC"})

        self.assert_image(spec)
        args = spec["vmspawn_args"]
        self.assertEqual(args[5], f"--linux={spec['linux']}")
        self.assertTrue(args[6].startswith("--initrd="), args[6])
        self.assert_build_output(args[6].removeprefix("--initrd="), "/initrd")
        self.assertEqual(args[7:], ["--tpm=no"])

        # The words of the cmdline_file, which carries the root hash, then cmdline_extra.
        cmdline = spec["cmdline"]
        self.assertEqual(cmdline[:3], ["root=tmpfs", "mount.usr=dissect", "rw"])
        self.assertRegex(cmdline[3], r"^usrhash=[0-9a-f]{64}$")
        self.assertEqual(cmdline[4:], ["tine.smoke=1"])

    def test_direct_boot_of_a_disk_without_esp(self) -> None:
        spec = self.spec("BOOT_DEMO_DIRECT_VM")
        self.assert_image(spec)
        # The disk's own boot files, as the target names none.
        self.assert_build_output(spec["linux"], "/vmlinuz")
        self.assertRegex(spec["cmdline"][-1], r"^usrhash=[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
