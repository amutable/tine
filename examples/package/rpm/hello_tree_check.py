#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check one release build of the hello fixture, the mirror of hello_dev_check.py."""

import subprocess
import sys
from pathlib import Path

rpms = Path(sys.argv[1])


def query(package: Path, *args: str) -> str:
    return subprocess.run(
        ["rpm", "-qp", *args, package], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


packages = list(rpms.glob("hello-[0-9]*.rpm"))
assert len(packages) == 1, packages
[package] = packages
assert query(package, "--queryformat", "%{PAYLOADCOMPRESSOR}") != "(none)"
files = set(query(package, "-l").splitlines())
markers = "/usr/share/tine-package"
# The spec comes out of the checkout as in a dev build, but none of the dev-only savings apply.
assert f"{markers}/spec-from-checkout" in files, files
# The build installs jq only because the spec lists it in BuildRequires, and %build runs jq.
assert f"{markers}/built-with-jq" in files, files
assert f"{markers}/profile-lto-enabled" in files, files
assert f"{markers}/profile-annobin-enabled" in files, files
assert list(rpms.glob("hello-debuginfo-*.rpm")), sorted(rpms.iterdir())
print(f"hello: release build, {len(files)} files")
