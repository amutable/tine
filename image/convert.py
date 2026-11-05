#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Convert a composed raw disk image into a distributable output format."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

import specs
from util import fail


class Spec(TypedDict):
    format: str
    input: str
    out: str
    virtual_size: str | None


def convert(source: Path, out: Path, fmt: str, virtual_size: str | None) -> None:
    """Write the raw disk `source` to `out` in the format `fmt`, one of ddi.bzl's DISK_FORMATS.

    `virtual_size` sizes the disk of a qcow2 or vmdk, which otherwise is as large as `source`.
    """
    if fmt == "raw.zst":
        cmd = [
            "zstd", "-q", "-f",
            # use all physical cores; don't use --adapt, it breaks reproducibility
            "--threads=0",
            # useful for mirroring; conflicts with --long, so don't use that
            "--rsyncable",
            # downloads happen more often than builds, so trade slower for better compression
            "-15",  # well above the default level 3, short of the --ultra maximum
            "-o", str(out), str(source),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
        return

    if fmt == "qcow2":
        options = []
    elif fmt == "vmdk":
        # streamOptimized is compressed and skips zero grains, and is the variant cloud importers
        # such as AWS VM Import take. Not reproducible: qemu-img writes a random content ID.
        options = ["-o", "subformat=streamOptimized"]
    else:
        fail(f"convert: unknown format {fmt!r}")

    with tempfile.TemporaryDirectory() as tmp:
        if size := virtual_size:
            # A VMDK cannot be resized after the fact, and a streamOptimized one cannot be converted
            # into at a larger size either, as the raw ends within a grain. So convert from a qcow2
            # overlay of that size instead, which reads the raw and zeros past its end, and costs no
            # copy. Like vmspawn's --grow-image, the added room sits past the GPT backup
            # header until first boot's repart rewrites the table.
            overlay = Path(tmp) / "overlay.qcow2"
            # absolute: qemu-img resolves a backing file relative to the overlay's directory
            backing = ["-b", str(source.absolute()), "-F", "raw"]
            subprocess.run(
                ["qemu-img", "create", "-q", "-f", "qcow2", *backing, str(overlay), size], check=True
            )
            info = subprocess.run(
                ["qemu-img", "info", "--output=json", str(overlay)], check=True, stdout=subprocess.PIPE
            )
            # an overlay smaller than its backing file silently cuts it off
            if json.loads(info.stdout)["virtual-size"] < source.stat().st_size:
                fail(f"convert: the partitions need {source.stat().st_size} bytes, more than {size}")
            source_format, source = "qcow2", overlay
        else:
            source_format = "raw"
        # convert drops zero clusters, so the output stays compact regardless of the disk size.
        subprocess.run(
            ["qemu-img", "convert", "-f", source_format, "-O", fmt, *options, str(source), str(out)],
            check=True,
        )


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "convert", argv)
    convert(Path(spec["input"]), Path(spec["out"]), spec["format"], spec["virtual_size"])


if __name__ == "__main__":
    main()
