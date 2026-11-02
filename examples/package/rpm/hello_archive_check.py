#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check one regular archive build: a binary and a source package, both with compressed payloads."""

import subprocess
import sys
from pathlib import Path

rpms = sorted(Path(sys.argv[1]).glob("hello-*.rpm"))
sources = [rpm for rpm in rpms if rpm.name.endswith(".src.rpm")]
assert len(rpms) == 2 and len(sources) == 1, rpms
for rpm in rpms:
    query = ["rpm", "-qp", "--queryformat", "%{PAYLOADCOMPRESSOR}", rpm]
    compressor = subprocess.run(query, check=True, stdout=subprocess.PIPE, text=True).stdout
    assert compressor in {"gzip", "bzip2", "xz", "lzma", "zstd"}, (rpm, compressor)
[binary] = [rpm for rpm in rpms if rpm not in sources]
files = subprocess.run(
    ["rpm", "-qpl", binary], check=True, stdout=subprocess.PIPE, text=True
).stdout.splitlines()
assert "/usr/share/tine-package/built-with-jq" in files, files
print(f"archive: {len(rpms)} packages with compressed payloads")
