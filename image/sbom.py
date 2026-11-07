#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Generate SPDX and CycloneDX SBOMs from a logical image with syft.

Scanning the whole assembled tree (not just the package database) also catches packages no
package manager knows about, such as Go modules bundled into ELF binaries. Two formats come
from one scan:
  spdx-json:      ISO/IEC 5962 standard, authoritative/compliance SBOM
  cyclonedx-json: modern standard, consumed by grype/trivy, basis for native VEX
The expensive part is the scan, so both formats are always emitted; the second is negligible.

syft ignores SOURCE_DATE_EPOCH and stamps each SBOM with the wall-clock time and a random
document UUID. Both are overwritten afterwards so identical inputs produce identical bytes.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import specs

import finalize


class Spec(finalize.ImageSpec):
    syft: str
    source_name: str
    source_version: str
    spdx: str
    cdx: str


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _rewrite(path: Path, doc: dict[str, object]) -> None:
    # Preserve syft's key order (json.loads/dumps is insertion-ordered); only bytes must be stable.
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize(spdx: Path, cdx: Path, source_name: str, source_version: str, epoch: int) -> None:
    """Pin syft's nondeterministic timestamps and document UUIDs for reproducible output."""
    stamp = datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # One stable id per (name, version); reused for both documents.
    ident = str(uuid5(NAMESPACE_URL, f"{source_name}/{source_version}"))

    spdx_doc = json.loads(spdx.read_text(encoding="utf-8"))
    spdx_doc["creationInfo"]["created"] = stamp
    spdx_doc["documentNamespace"] = _UUID.sub(ident, spdx_doc["documentNamespace"])
    _rewrite(spdx, spdx_doc)

    cdx_doc = json.loads(cdx.read_text(encoding="utf-8"))
    cdx_doc["metadata"]["timestamp"] = stamp
    cdx_doc["serialNumber"] = "urn:uuid:" + ident
    _rewrite(cdx, cdx_doc)


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "sbom", argv)

    syft = Path(spec["syft"])
    source_name = spec["source_name"]
    source_version = spec["source_version"]
    spdx = Path(spec["spdx"])
    cdx = Path(spec["cdx"])
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])

    with (
        finalize.image(spec) as tree,
        tempfile.TemporaryDirectory(prefix="syft.") as home,
    ):
        env = os.environ | {
            # Drop the per-.ko "pkg:generic" module entries (~90% of the SBOM). They carry no
            # CPE/PURL any vulnerability feed keys on; the kernel package still represents the
            # kernel. Revisit if a kernel is ever installed by other means than a package.
            "SYFT_LINUX_KERNEL_CATALOG_MODULES": "false",
            # The sandbox has no network; keep syft from reaching out.
            "SYFT_CHECK_FOR_APP_UPDATE": "false",
            # Writable, hermetic cache/config for the sandbox.
            "HOME": home,
            "XDG_CACHE_HOME": home,
        }
        subprocess.run(
            [
                str(syft),
                "scan",
                f"dir:{tree}",
                "--source-name",
                source_name,
                "--source-version",
                source_version,
                "-o",
                f"spdx-json={spdx}",
                "-o",
                f"cyclonedx-json={cdx}",
            ],
            check=True,
            env=env,
        )

    _normalize(spdx, cdx, source_name, source_version, epoch)
    print(f"sbom: wrote SPDX -> {spdx} and CycloneDX -> {cdx}", file=sys.stderr)


if __name__ == "__main__":
    main()
