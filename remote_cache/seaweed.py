# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""A real S3 server to test against: SeaweedFS from the pinned binary, for as long as a test needs it.

Its filer serves at `/buckets/<bucket>/<key>` exactly what its S3 endpoint stored, which is the
two-protocol claim the bucket layout rests on. It checks no signature, so any key pair works.
"""

import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

STARTUP_TIMEOUT = 10


def free_ports(count: int) -> list[int]:
    """Ports nothing is listening on, which is the best a test can do before starting a server."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for one in sockets:
            one.bind(("127.0.0.1", 0))
        return [one.getsockname()[1] for one in sockets]
    finally:
        for one in sockets:
            one.close()


class Seaweed:
    """SeaweedFS instance with an S3 endpoint and a filer.

    Creates the requested bucket name. Stays around until .close(), then cleans up after itself.
    """

    def __init__(self, binary: Path, directory: Path, bucket: str) -> None:
        self.bucket = bucket
        master, master_grpc, volume, volume_grpc, filer, filer_grpc, s3, s3_grpc = free_ports(8)
        self.s3_endpoint = f"127.0.0.1:{s3}"
        self.read_url = f"http://127.0.0.1:{filer}/buckets/{bucket}"
        self.log = directory / "weed.log"
        self.output = self.log.open("wb")
        (directory / "weed").mkdir()
        self.process = subprocess.Popen(
            [
                binary,
                "server",
                f"-dir={directory / 'weed'}",
                "-ip=127.0.0.1",
                "-s3",
                f"-s3.port={s3}",
                f"-master.port={master}",
                f"-volume.port={volume}",
                f"-filer.port={filer}",
                # Every gRPC port stated outright: unstated, SeaweedFS derives it as the port plus
                # 10000, which is an invalid port for anything the kernel hands out above 55535.
                f"-s3.port.grpc={s3_grpc}",
                f"-master.port.grpc={master_grpc}",
                f"-volume.port.grpc={volume_grpc}",
                f"-filer.port.grpc={filer_grpc}",
                # Its S3 command also starts an Iceberg catalog and a Lance namespace on fixed
                # ports, and it dies if either is taken, so two instances cannot coexist.
                "-s3.port.iceberg=0",
                "-s3.port.lance=0",
                "-volume.max=3",
            ],
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )
        try:
            self.wait_for(f"http://{self.s3_endpoint}/")
            self.wait_for(f"http://127.0.0.1:{filer}/")
            with urllib.request.urlopen(
                urllib.request.Request(f"http://{self.s3_endpoint}/{bucket}", method="PUT"), timeout=10
            ):
                pass
            # Its ports answer before it can store anything: a write is a 500 until the volume
            # server has registered and a volume has been assigned, so wait for the first object
            # to go in and come back out.
            self.wait_for(f"{self.read_url}/probe", self.probe)
        except BaseException:
            self.close()
            raise

    def probe(self) -> None:
        request = urllib.request.Request(f"http://{self.s3_endpoint}/{self.bucket}/probe", data=b"ready")
        request.method = "PUT"
        with urllib.request.urlopen(request, timeout=2):
            pass

    def wait_for(self, url: str, first: Callable[[], None] | None = None) -> None:
        """Wait for a URL to answer, and say why if the server died instead of opening it."""
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                tail = self.log.read_text(errors="replace")[-2000:]
                raise AssertionError(f"weed exited with {self.process.returncode}:\n{tail}")
            try:
                if first is not None:
                    first()
                with urllib.request.urlopen(url, timeout=2):
                    return
            except urllib.error.URLError, OSError:
                time.sleep(0.3)
        raise AssertionError(f"{url} never answered; see {self.log}")

    def close(self) -> None:
        # weed keeps running for more than a minute after SIGTERM, because its filer retries a
        # metadata write that fails once the volume server has stopped. The caller removes the
        # directory afterwards, so nothing in it needs a graceful stop.
        self.process.kill()
        self.process.wait()
        self.output.close()
