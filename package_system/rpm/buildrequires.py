#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Write the BuildRequires of an RPM spec that the buildroot lacks as a JSON list.

The driver stages the spec as build.py does and runs `rpmbuild -bd` in the buildroot with the arguments of
the build. The distribution's macros and the spec's conditionals then select the same BuildRequires as
the build does.
"""

import itertools
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import specs
import util

import builder
import rootfs
import rpmbuild


class Spec(rpmbuild.Parse):
    # `lower` lists the layers of the buildroot with declared BuildRequires installed, bottom first.
    lower: list[str]
    # `source_tree` is the source tree of an in-place build, or None.
    source_tree: str | None
    out: str


_HEADER = "error: Failed build dependencies:"


def query(spec: Spec) -> list[str]:
    """Return the BuildRequires of the spec of `spec` that the buildroot lacks."""
    # The driver parses the messages of rpmbuild, which rpm translates otherwise.
    env = os.environ | rpmbuild.environment(spec["source_date_epoch"]) | {"LC_ALL": "C.UTF-8"}
    source_tree = spec["source_tree"]
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space.
    topdir = Path(tempfile.gettempdir()) / "topdir"
    topdir.mkdir()
    with ExitStack() as stack:
        # %prep can leave read-only directories in topdir, and Buck must be able to remove them. The stack
        # exits in reverse order. capture_on_exit() then runs after source_overlay() unmounts the checkout.
        stack.enter_context(rootfs.capture_on_exit(topdir))
        if source_tree is not None:
            stack.enter_context(rootfs.source_overlay(Path(source_tree), topdir / builder.CHECKOUT))
        staged = rpmbuild.stage(topdir, spec, source_tree)
        arguments = rpmbuild.arguments(staged, spec)
        # rpmbuild -bd checks the static BuildRequires against the rpm database of the buildroot first. It
        # runs %prep and %generate_buildrequires only when the buildroot has all of them, and then checks
        # what the script reports. -bd doesn't package the sources, so it doesn't check that the Source and
        # Patch files exist. An in-place spec can then name a Source archive that the checkout replaces.
        mode = ["-bd"]
        # rpmbuild skips %prep for an in-place build. --noprep would skip it as well, but rpmbuild then
        # doesn't parse the spec again for its BuildArch, which build.py's rpmbuild -bb does.
        if source_tree is not None:
            mode.append("--build-in-place")
        stack.enter_context(builder.buildroot(spec["lower"], topdir))
        result = subprocess.run(
            ["/usr/bin/rpmbuild", *arguments, *mode, str(staged.spec)],
            cwd=staged.cwd,
            env=env,
            stdout=sys.stderr,
            stderr=subprocess.PIPE,
            text=True,
            # %prep and %generate_buildrequires write to the same stderr as rpmbuild, in any encoding.
            errors="replace",
        )
    sys.stderr.write(result.stderr)
    if result.returncode == 0:
        return []
    # rpmbuild exits with 11 when BuildRequires are missing or BuildConflicts are installed. It lists each
    # problem on a tab-indented line after a header:
    #
    #     error: Failed build dependencies:
    #             meson >= 0.60 is needed by example-1-1.x86_64
    #
    # rpmbuild checks the dependencies after the scripts run, so only the lines after the last header are
    # its own.
    lines = result.stderr.splitlines()
    problems: list[str] = []
    if result.returncode == 11 and _HEADER in lines:
        start = len(lines) - lines[::-1].index(_HEADER)
        problems = [
            line.strip() for line in itertools.takewhile(lambda line: line.startswith("\t"), lines[start:])
        ]
    if conflicts := [problem for problem in problems if " conflicts with " in problem]:
        util.fail(
            f"buildrequires: the buildroot contains BuildConflicts of {spec['spec_file']}: {conflicts}"
        )
    missing = {problem.rsplit(" is needed by ", 1)[0] for problem in problems if " is needed by " in problem}
    if not missing:
        util.fail(
            f"buildrequires: rpmbuild -bd failed with exit status {result.returncode}: {spec['spec_file']}"
        )
    return sorted(missing)


def main(argv: list[str] | None = None) -> None:
    """Parse an invocation and write the BuildRequires of its spec."""
    spec = specs.parse(Spec, "buildrequires", argv)
    # The query runs every %(...) of the spec. For a spec with %generate_buildrequires, it also runs the
    # script, and %prep in an archive build. The project is read-only meanwhile, as during the build.
    with rootfs.readonly_project(Path.cwd(), {}):
        missing = query(spec)
    Path(spec["out"]).write_text(json.dumps(missing, indent=2) + "\n")
    print(f"{spec['spec_file']}: {len(missing)} missing BuildRequires", file=sys.stderr)


if __name__ == "__main__":
    main()
