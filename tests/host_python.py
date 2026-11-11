# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Fail when `tine.use_host_python` does not put the bootstrap toolchain on the host's python3.

    buck run tine//tests:host-python

The test enables the setting for the project while it runs, and builds in its own isolation dir to
leave the developer's build state alone. A host whose python3 is older than the build needs, such as
a CI runner on Ubuntu 24.04, stands in the pinned interpreter for it.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from util import buck_output, fail

ISOLATION = "host-python"
SETTING = Path(".buckconfig.d/tine-host-python")
PROBE = "tine//tests:host-python-probe"
SUITE = "tine//python:test"


def realpath(python: str) -> str | None:
    """Return the interpreter's resolved path, or None if it cannot run the build."""
    proc = subprocess.run(
        [python, "-c", "import os, sys, compression.zstd; print(os.path.realpath(sys.executable))"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


class Nested:
    """Run nested `tine buck` commands in the test's isolation dir."""

    def __init__(self, root: Path, path: str) -> None:
        self.root = root
        # The outer `tine buck run` exports BUCK2_BINARY, BUCK2_ARG0 and TINE_MOUNTS. Without them, the
        # nested bin/tine resolves Buck and sets up the mounts itself, as it does for a developer.
        self.environment = {
            name: value
            for name, value in os.environ.items()
            if name not in ("BUCK2_BINARY", "BUCK2_ARG0", "TINE_MOUNTS")
        } | {"PATH": path}

    def tine(self, *arguments: str) -> str:
        """Run `tine buck` in the isolation dir and return its stdout. Buck's progress goes to stderr."""
        command = [str(self.root / "bin" / "tine"), "buck", "--isolation-dir", ISOLATION, *arguments]
        print("+", " ".join(command[1:]), file=sys.stderr, flush=True)
        proc = subprocess.run(
            command, cwd=self.root, env=self.environment, stdout=subprocess.PIPE, text=True
        )
        if proc.returncode != 0:
            print(proc.stdout, end="", file=sys.stderr, flush=True)
            fail(f"tine buck {' '.join(arguments)} exited with {proc.returncode}")
        return proc.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fallback", required=True, help="the pinned interpreter")
    args = parser.parse_args()

    root = Path(buck_output("root", "--kind", "project"))
    path = os.environ.get("PATH", "")
    host = shutil.which("python3", path=path)
    expected = realpath(host) if host else None
    if expected is None:
        fallback = Path(args.fallback).absolute()
        print(f"=== no python3 on PATH runs the build; using {fallback}", file=sys.stderr)
        path = os.pathsep.join([str(fallback.parent), path])
        expected = realpath(str(fallback))
        if expected is None:
            fail(f"{fallback} cannot run the build either")

    setting = root / SETTING
    if setting.exists():
        fail(f"{setting} exists already; this test writes and removes it")
    with ExitStack() as stack:
        run = Nested(root, path)
        # `clean` deletes the isolation dir's buck-out and stops its daemon.
        stack.callback(run.tine, "clean")
        stack.callback(setting.unlink)
        setting.parent.mkdir(exist_ok=True)
        setting.write_text("[tine]\nuse_host_python = true\n")

        print("=== the toolchain starts the host's python3, isolated from the host", file=sys.stderr)
        # bin/tine prints its own notes between the probe's output, which is one JSON line.
        lines = [line for line in run.tine("run", PROBE).splitlines() if line.startswith("{")]
        if not lines:
            fail(f"{PROBE} printed nothing")
        probe = json.loads(lines[-1])
        if probe["executable"] != expected:
            fail(f"{PROBE} ran on {probe['executable']}, not on {expected}")
        flags = ("ignore_environment", "no_site", "no_user_site")
        if unset := [flag for flag in flags if not probe[flag]]:
            fail(f"{PROBE} ran without {', '.join(unset)}")
        if "tine-host-python" not in probe["xoptions"]:
            fail(f"{PROBE} ran without the interpreter's identity on its command line")

        print("=== a suite runs on the interpreter it is told it runs on", file=sys.stderr)
        run.tine("test", SUITE)
        print(f"=== the bootstrap toolchain ran on {expected}", file=sys.stderr)


if __name__ == "__main__":
    main()
