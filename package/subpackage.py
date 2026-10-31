# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Match the packages that a build produced to the binary subpackages that its target declares."""

import re
import sys
from pathlib import Path

import util


def emit(
    declared: dict[str, Path],
    produced: dict[str, Path],
    *,
    version: str,
    generated: str,
    strict: bool,
) -> None:
    """Copy the package of each subpackage name in `declared` to the path that `declared` maps it to.

    `produced` maps the file name of each package that the build wrote to its path. `version` is a regular
    expression for the part of a file name after `<name>`, including the separator after the name.
    `generated` matches the file names of packages that the build tool adds on its own, such as debug
    packages, which a target need not declare. If the declared and the produced packages differ in any
    other way, the function fails when `strict` is set and prints the difference otherwise.
    """
    names_by_len = sorted(declared, key=len, reverse=True)
    patterns = {name: re.compile(rf"^{re.escape(name)}{version}$") for name in declared}

    matched: dict[str, str] = {}  # subpackage name -> produced file name
    for fname in sorted(produced):
        for name in names_by_len:
            if patterns[name].match(fname):
                if name not in matched:
                    matched[name] = fname
                break

    missing = sorted(set(declared) - set(matched))
    unexpected = sorted(
        f for f in produced if f not in set(matched.values()) and not re.search(generated, f)
    )
    if missing or unexpected:
        report = (
            "subpackage fidelity gate failed:\n"
            f"  declared but not produced: {missing}\n"
            f"  produced but not declared: {unexpected}"
        )
        if strict:
            util.fail(report)
        # A build without `strict` takes its packaging from a pinned commit, and the declared subpackages
        # can come from elsewhere. Buck fails a build of a declared sub-target that this build did not
        # produce.
        print(f"{report}\nThe pinned packaging differs from the declared subpackages.", file=sys.stderr)

    for name, fname in matched.items():
        util.clone_file(produced[fname], declared[name], allow_link=True)
    print(f"emitted {len(matched)} subpackage sub-targets", file=sys.stderr)
