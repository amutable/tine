# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Import a disk into EC2: upload it as an EBS snapshot, then register it with its AMI parameters.

Every AWS call goes through the `aws` CLI, so this uses whatever login and region the CLI is
configured with, and never handles credentials itself.
"""

import argparse
import base64
import compression.zstd
import concurrent.futures
import datetime
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# The only block size the EBS direct APIs take.
BLOCK_SIZE = 512 * 1024
GIB = 1024**3
# Biggest possible zstd frame header, for reading the decompressed size.
ZSTD_FRAME_HEADER_MAX = 18
# EC2 takes deprecation times up to ten years ahead.
DEPRECATE_DAYS_MAX = 3650


def zstd_size(disk: Path) -> int | None:
    """The decompressed size of a zstd-compressed `disk`, or None for an uncompressed one."""
    with disk.open("rb") as f:
        header = f.read(ZSTD_FRAME_HEADER_MAX)
    try:
        size = compression.zstd.get_frame_info(header).decompressed_size
    except compression.zstd.ZstdError:
        return None
    if size is None:
        sys.exit(f"ami-import: {disk} does not record its decompressed size")
    return size


def disk_size(disk: Path) -> int:
    """The size of `disk`, decompressed if it is zstd-compressed."""
    size = zstd_size(disk)
    return disk.stat().st_size if size is None else size


def _allocated_blocks(disk: Path) -> Iterator[tuple[int, bytes]]:
    with disk.open("rb") as f:
        fd = f.fileno()
        size = os.fstat(fd).st_size
        offset = 0
        while offset < size:
            try:
                start = os.lseek(fd, offset, os.SEEK_DATA)
            except OSError as e:
                # nothing but a trailing hole past `offset`
                if e.errno == errno.ENXIO:
                    return
                raise
            end = os.lseek(fd, start, os.SEEK_HOLE)
            for index in range(start // BLOCK_SIZE, (end + BLOCK_SIZE - 1) // BLOCK_SIZE):
                yield index, os.pread(fd, BLOCK_SIZE, index * BLOCK_SIZE).ljust(BLOCK_SIZE, b"\0")
            offset = (end + BLOCK_SIZE - 1) // BLOCK_SIZE * BLOCK_SIZE


def _decompressed_blocks(disk: Path, size: int) -> Iterator[tuple[int, bytes]]:
    with compression.zstd.open(disk) as f:
        index = 0
        while block := f.read(BLOCK_SIZE):
            yield index, block.ljust(BLOCK_SIZE, b"\0")
            index += 1
        # The snapshot was started with the size the first frame records.
        if f.tell() != size:
            sys.exit(f"ami-import: {disk} decompresses to {f.tell()} bytes, not the {size} its header says")


def data_blocks(disk: Path) -> Iterator[tuple[int, bytes]]:
    """Yield each block of `disk` that holds anything but zeros, with its index.

    A zstd-compressed disk is decompressed on the fly. Holes of an uncompressed one are never read,
    so a sparse disk costs its allocated size rather than its apparent one.
    """
    size = zstd_size(disk)
    blocks = _allocated_blocks(disk) if size is None else _decompressed_blocks(disk, size)
    for index, block in blocks:
        if block.count(0) != BLOCK_SIZE:
            yield index, block


def checksum(block: bytes) -> str:
    """The per-block checksum `PutSnapshotBlock` requires: base64 of its SHA256."""
    return base64.b64encode(hashlib.sha256(block).digest()).decode()


class Aws:
    """The `aws` CLI, with JSON output and an optional region for every call."""

    def __init__(self, region: str | None) -> None:
        self.region = ["--region", region] if region else []
        # The CLI records a session ID in an SQLite database; the parallel uploads contend for it and
        # the losing ones print a traceback.
        self.env = os.environ | {"AWS_CLI_SESSION_ID_DISABLED": "true"}

    def __call__(self, *args: str) -> dict[str, Any]:
        out = subprocess.run(
            ["aws", *args, *self.region, "--output", "json"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            env=self.env,
        ).stdout
        return json.loads(out) if out.strip() else {}


def upload(aws: Aws, disk: Path, name: str, jobs: int) -> str:
    """Write the non-zero blocks of `disk` into a new snapshot and return its ID once completed."""
    # An encrypted snapshot reads back garbage, not zeros, for blocks never written; skipping zero
    # blocks is only correct for an unencrypted one, and the account default would encrypt it.
    if aws("ec2", "get-ebs-encryption-by-default")["EbsEncryptionByDefault"]:
        sys.exit("ami-import: EBS encryption by default is enabled for this account and region; disable it")

    size = (disk_size(disk) + GIB - 1) // GIB
    snapshot = str(
        aws(
            "ebs",
            "start-snapshot",
            f"--volume-size={size}",
            "--no-encrypted",
            f"--description={name}",
            f"--tags=Key=Name,Value={name}",
            # minutes until EBS cancels it should this die before completing it
            "--timeout=60",
        )["SnapshotId"]
    )
    print(f"ami-import: uploading {disk} into {snapshot} ({size} GiB)", file=sys.stderr)

    def put(index: int, block: bytes, scratch: Path) -> None:
        data = scratch / str(index)
        data.write_bytes(block)
        aws(
            "ebs",
            "put-snapshot-block",
            f"--snapshot-id={snapshot}",
            f"--block-index={index}",
            f"--block-data={data}",
            f"--data-length={BLOCK_SIZE}",
            f"--checksum={checksum(block)}",
            "--checksum-algorithm=SHA256",
        )
        data.unlink()

    written = 0
    with tempfile.TemporaryDirectory() as tmp, concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        # Bounded, so that memory holds a few blocks per job rather than the whole disk.
        pending: set[concurrent.futures.Future[None]] = set()
        for index, block in data_blocks(disk):
            if len(pending) >= 2 * jobs:
                done, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    future.result()
            pending.add(pool.submit(put, index, block, Path(tmp)))
            written += 1
            if written % 100 == 0:
                print(f"ami-import: {written} blocks", file=sys.stderr)
        for future in concurrent.futures.wait(pending).done:
            future.result()
    print(f"ami-import: {written} blocks written, completing", file=sys.stderr)

    aws("ebs", "complete-snapshot", f"--snapshot-id={snapshot}", f"--changed-blocks-count={written}")
    aws("ec2", "wait", "snapshot-completed", f"--snapshot-ids={snapshot}")
    return snapshot


def register(aws: Aws, parameters: dict[str, Any], name: str, snapshot: str, tpm: bool) -> str:
    """Register the image with `parameters`, booting `snapshot`, and return its ID."""
    if not tpm:
        del parameters["TpmSupport"]
    parameters["Name"] = name
    parameters["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"] = snapshot
    parameters["TagSpecifications"] = [{"ResourceType": "image", "Tags": [{"Key": "Name", "Value": name}]}]
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump(parameters, f)
        f.flush()
        return str(aws("ec2", "register-image", "--cli-input-json", f"file://{f.name}")["ImageId"])


def deprecate_days(value: str) -> int:
    """`--deprecate-days`, checked against what EC2 takes before spending the upload."""
    days = int(value)
    if not 1 <= days <= DEPRECATE_DAYS_MAX:
        raise argparse.ArgumentTypeError(f"{days} is not between 1 and {DEPRECATE_DAYS_MAX} days")
    return days


def deprecation_time(now: datetime.datetime, days: int) -> str:
    """The time `days` after `now`, in the format `EnableImageDeprecation` takes."""
    return (now + datetime.timedelta(days=days)).astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("disk", type=Path, help="the .raw[.zst] disk to upload")
    parser.add_argument("parameters", type=Path, help="the disk's RegisterImage parameters (.ami.json)")
    parser.add_argument("--name", help="the image name, instead of the parameters'; EC2 requires it unique")
    parser.add_argument("--region", help="the region to import into, instead of the CLI's configured one")
    parser.add_argument("--jobs", type=int, default=16, help="blocks to upload in parallel")
    parser.add_argument(
        "--no-tpm",
        dest="tpm",
        action="store_false",
        help="register without a NitroTPM, which bare metal instance types lack",
    )
    parser.add_argument(
        "--deprecate-days",
        type=deprecate_days,
        metavar="DAYS",
        help="deprecate the image this many days after registering it",
    )
    args = parser.parse_args()

    aws = Aws(args.region)
    parameters = json.loads(args.parameters.read_text())
    name = args.name or parameters["Name"]
    # Names are unique per account and region; find out before spending the upload.
    if aws("ec2", "describe-images", "--owners=self", f"--filters=Name=name,Values={name}")["Images"]:
        sys.exit(f"ami-import: an image named {name!r} exists already; pass another --name")

    snapshot = upload(aws, args.disk, name, args.jobs)
    image = register(aws, parameters, name, snapshot, args.tpm)
    if args.deprecate_days:
        at = deprecation_time(datetime.datetime.now(datetime.UTC), args.deprecate_days)
        aws("ec2", "enable-image-deprecation", f"--image-id={image}", f"--deprecate-at={at}")
    print(image)


if __name__ == "__main__":
    main()
