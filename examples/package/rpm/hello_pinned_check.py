#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check the build of the hello fixture with the packaging of its pinned dist-git commit."""

import subprocess
import sys
from pathlib import Path

rpms = Path(sys.argv[1])
subpackage = Path(sys.argv[2])


def query(package: Path, *args: str) -> str:
    return subprocess.run(
        ["rpm", "-qp", *args, package], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


packages = list(rpms.glob("hello-[0-9]*.rpm"))
assert len(packages) == 1, packages
[package] = packages
files = set(query(package, "-l").splitlines())
assert "/usr/share/tine-package/spec-from-pin" in files, files
assert "/usr/share/tine-package/spec-from-checkout" not in files, files
# The build installs jq only because the pinned spec lists it in BuildRequires, and %build runs jq.
assert "/usr/share/tine-package/built-with-jq" in files, files
assert query(subpackage, "--queryformat", "%{NAME}") == "hello"
print(f"hello: pinned packaging, {len(files)} files")
