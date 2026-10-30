#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a Rust project from source inside a box, against its vendored crate tree.

The build runs with no network: every registry crate is already unpacked, and pointing cargo's
crates-io source at that directory is what keeps it from consulting the registry index. Each git
source is replaced by the fetched repository itself, a file:// clone cargo takes its own checkout
from, insisting on the locked commit, so nothing has to be rewritten or re-verified here.
cargo-auditable wraps the build to record the crate graph in each binary, which is how the image's
SBOM learns what went into it.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

import specs
import util

import rootfs
from isolation import Bind


class GitSource(TypedDict):
    # The fields cargo identifies the source by (`git` plus the reference the project asked for).
    fields: dict[str, str]
    # The fetched repository providing the locked commit.
    repo: str


class Spec(TypedDict):
    # The cargo-auditable wrapper cargo builds through.
    auditable: str
    # The output directory that contains the binaries.
    bin: str
    # The binaries to take out of the build.
    binaries: list[str]
    # Locked commit -> the git source to replace with its fetched repository.
    git: dict[str, GitSource]
    # Where the workspace sits inside `src`, empty when the project is its own root.
    root: str
    # The project's source tree.
    src: str
    # Cargo's persistent incremental build directory for a project in dev mode
    target: str | None
    # cargo builds the binaries for this Rust target triple.
    triple: str
    # The unpacked crates the build resolves against.
    vendor: str


def _reject_local_config(source: Path, workspace: Path) -> None:
    """Refuse a checkout's own cargo configuration, which would outrank the one this driver writes.

    Cargo merges configuration from the working directory upward and reads `$CARGO_HOME` last, so a
    file in the checkout wins: it can redirect the crates-io source away from the vendored tree, or
    move the directory the declared binaries are taken from.
    """
    directory = workspace
    while True:
        for name in ("config.toml", "config"):
            found = directory / ".cargo" / name
            if found.exists():
                util.fail(
                    f"cargo-build: {found.relative_to(source)} would override the vendored source "
                    "configuration; keep it out of src"
                )
        if directory == source:
            return
        directory = directory.parent


def _cargo_config(vendor: Path, git: dict[str, GitSource]) -> str:
    """Point the registry at the vendored directory and every git source at its fetched repository.

    Cargo replaces a source as a whole, so each git dependency needs a stanza of its own: one
    section carrying the fields cargo identifies the source by (the section names are arbitrary),
    replaced with one naming the local repository that stands in for the remote.
    """
    sections = ['[source.crates-io]\nreplace-with = "vendored-sources"\n']
    for commit, source in sorted(git.items()):
        rendered = "".join(f'{name} = "{value}"\n' for name, value in source["fields"].items())
        local = f"git-{commit[:12]}"
        sections.append(f'[source."{local}-upstream"]\n{rendered}replace-with = "{local}"\n')
        repo = Path(source["repo"]).absolute()
        sections.append(f'[source.{local}]\ngit = "file://{repo}"\nrev = "{commit}"\n')
    sections.append(f'[source.vendored-sources]\ndirectory = "{vendor}"\n')
    return "\n".join(sections)


def _take_binaries(target: Path, triple: str, names: list[str], into: Path) -> None:
    """Copy each declared binary out of cargo's `target/<triple>/release` into the output directory."""
    built = target / triple / "release"
    missing = [name for name in names if not (built / name).is_file()]
    if missing:
        # Everything executable in there, which after an earlier build of the same project may name
        # more than this one produced.
        entries = built.iterdir() if built.is_dir() else []
        found = sorted(entry.name for entry in entries if entry.is_file() and os.access(entry, os.X_OK))
        holds = f"which holds: {', '.join(found)}" if found else "which holds no executable"
        util.fail(f"cargo-build: no {', '.join(missing)} in target/{triple}/release, {holds}")
    for name in names:
        util.clone_file(built / name, into / name)


def build_cargo(spec: Spec) -> None:
    """Build a Cargo workspace on a read-only bind mount of its sources, in a read-only project."""
    # The sandbox points TMPDIR at /var/tmp, which it backs with the action's scratch space. Buck
    # clears the scratch space before each run, so none of these paths exists yet.
    scratch = Path(tempfile.gettempdir())
    build = scratch / "build"
    cargo_home = scratch / "cargo"
    target = scratch / "target"
    out = scratch / "bin"

    # Cargo resolves a relative path in a config.toml against the directory that contains the file.
    # Cargo also runs in the workspace, not in the project. Every project path that this driver passes
    # to cargo is therefore absolute.
    cargo_home.mkdir()
    (cargo_home / "config.toml").write_text(
        _cargo_config(Path(spec["vendor"]).absolute(), spec["git"]),
        encoding="utf-8",
    )

    outputs = {Path(spec["bin"]): out}
    if spec["target"] is not None:
        # For an incremental build buck keeps cargo's previous build directory, so a rebuild redoes
        # only what changed. Cargo decides that from the modification times of the sources.
        outputs[Path(spec["target"])] = target
    with rootfs.readonly_project(Path.cwd(), outputs):
        util.remove_previous_binaries(out)

        # Cargo reads `.cargo/config.toml` and a `Cargo.toml` with a `[workspace]` table in every parent
        # directory of the workspace. Inside the project, those parents belong to the consuming
        # repository, and their files would change the build. The parents of the bind mount are
        # directories of the sandbox. The bind is read-only, because it is a bind of a directory in the
        # read-only project.
        with Bind(Path(spec["src"]), build) as bind:
            bind.mount()
            workspace = build / spec["root"]
            _reject_local_config(build, workspace)

            # Cargo refuses every git transfer under --offline, the file:// repositories included. That
            # flag is just belt-and-suspenders though: the action always runs in an unshared network
            # namespace, so cargo can never reach out to the actual internet.
            offline = [] if spec["git"] else ["--offline"]
            subprocess.run(
                [
                    Path(spec["auditable"]).absolute(),
                    "auditable",
                    "build",
                    "--release",
                    "--locked",
                    # Without --target, cargo builds for the box's architecture. The image that
                    # contains the binaries can be for another architecture. A cross build needs the
                    # target's standard library and a linker for it in the box. Without them, cargo
                    # fails.
                    "--target",
                    spec["triple"],
                    *offline,
                ],
                check=True,
                cwd=workspace,
                env=os.environ
                | {
                    "CARGO_HOME": str(cargo_home),
                    "CARGO_TARGET_DIR": str(target),
                    # Fedora's pkg-config is a wrapper script that runs `rpm --eval`, and its package
                    # doesn't depend on rpm. In a box without rpm, every pkg-config lookup fails. The
                    # pkg-config crate runs PKG_CONFIG in place of pkg-config.
                    "PKG_CONFIG": "pkgconf",
                },
            )

        _take_binaries(target, spec["triple"], spec["binaries"], out)


def main(argv: list[str] | None = None) -> None:
    build_cargo(specs.parse(Spec, "cargo-build", argv))


if __name__ == "__main__":
    main()
