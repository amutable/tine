#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build RPMs inside an assembled, pinned buildroot.

Sources and the RPM spec are staged in action scratch space. Buck keeps the
build directory between runs when the project's dev configuration requests it;
otherwise only the produced RPMs persist. The box sandbox already supplies
isolation around the chroot.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import specs
import util

import builder
import rpmbuild
import subpackage

# The file in the persistent build directory that records which rpm macros its build was configured with.
MACROS_STAMP = ".tine-rpm-macros"


class Spec(builder.Spec, rpmbuild.Parse):
    """Buck's generated build invocation, distinct from the package's RPM spec."""

    # `strict_subpackages` fails the build when the produced subpackages differ from the declared ones. The
    # spec of pinned packaging can declare other subpackages than the imported metadata.
    strict_subpackages: bool


def build_rpm(spec: Spec) -> int:
    """Build the RPMs described by `spec` in action scratch space."""
    return builder.run(spec, lambda topdir, out, subpackages: _build_rpm(spec, topdir, out, subpackages))


def _build_rpm(spec: Spec, topdir: Path, out: Path, subpackages: Path) -> int:
    source_tree = spec["source_tree"]
    staged = rpmbuild.stage(topdir, spec, source_tree)
    env = os.environ | rpmbuild.environment(spec["source_date_epoch"])
    arguments = rpmbuild.arguments(staged, spec)
    with builder.buildroot(spec["lower"], topdir) as root:
        if spec["build_dir"] is not None:
            _reset_stale_build_dir(
                root / builder.CHROOT_TOPDIR.relative_to("/") / builder.BUILD, arguments, env
            )
        mode = ["-ba"]
        cwd = staged.cwd
        if source_tree is not None:
            mode = ["-bb", "--noprep", "--build-in-place"]
        rc = subprocess.run(
            [
                "/usr/bin/rpmbuild",
                *arguments,
                *mode,
                "--nocheck",
                "--noclean",
                str(staged.spec),
            ],
            cwd=cwd,
            env=env,
        ).returncode
    if rc != 0:
        return rc

    # Collect binary packages and, for a regular archive build, the source package.
    produced: dict[str, Path] = {}  # basename -> path of each binary rpm
    for sub in ("RPMS", "SRPMS"):
        for f in sorted((topdir / sub).rglob("*.rpm")):
            util.clone_file(f, out / f.name, allow_link=True)
            if not f.name.endswith(".src.rpm"):
                produced[f.name] = f
    source_output = "" if source_tree is not None else " + srpm"
    print(f"collected {len(produced)} binary rpms{source_output} into {spec['out']}", file=sys.stderr)

    if spec["subpackages"]:
        subpackage.emit(
            {name: subpackages / f"{name}.rpm" for name in spec["subpackages"]},
            produced,
            version=r"-[^-]+-[^-]+\.[^.]+\.rpm",
            # rpmbuild adds debuginfo and debugsource packages on its own.
            generated=r"-debug(info|source)-",
            strict=spec["strict_subpackages"],
        )

    return 0


def _reset_stale_build_dir(build_dir: Path, arguments: list[str], env: dict[str, str]) -> None:
    """Empty `build_dir` unless its last build used the same rpm macros as `arguments`.

    It runs in the chroot of the build, so that it reads the macros of the root that the build runs in.
    """
    # meson reads CFLAGS and LDFLAGS from the environment on the first configure only. It ignores changed
    # values even with --reconfigure. %meson runs again in every build, but a changed flag never reaches
    # the compiler. The flags expand from rpm macros. A change to a macro of the buildroot or to the
    # rpmbuild options therefore empties the build directory. A spec that redefines a flag macro itself
    # does not change the digest.
    showrc = subprocess.run(
        ["/usr/bin/rpmbuild", *arguments, "--showrc"], env=env, stdout=subprocess.PIPE, check=True
    ).stdout
    digest = hashlib.sha256(showrc).hexdigest()
    stamp = build_dir / MACROS_STAMP
    if stamp.exists() and stamp.read_text() == digest:
        return
    for path in build_dir.iterdir():
        util.remove_path(path)
    stamp.write_text(digest)


def main(argv: list[str] | None = None) -> int:
    """Parse an invocation and build its RPMs."""
    return build_rpm(specs.parse(Spec, "build_rpm", argv))


if __name__ == "__main__":
    raise SystemExit(main())
