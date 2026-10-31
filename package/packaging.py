#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Check out the packaging commit that a source checkout pins.

The pin is a JSON file in the checkout that maps each distro to a repository `url`, a `branch` and a
`commit`. Some forges refuse to serve a commit by its hash, so the driver fetches the branch and requires
the commit to be on it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import specs
from util import fail

PIN_KEYS = ("branch", "commit", "url")


class Spec(TypedDict):
    pin: str
    distro: str
    out: str


def read_pin(path: Path, distro: str) -> tuple[str, str, str]:
    """Return the url, branch and commit that a pin file names for `distro`."""
    try:
        pins = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        fail(f"{path}: cannot read the packaging pin: {error}")
    if not isinstance(pins, dict):
        fail(f"{path}: the packaging pin must be a JSON object that maps each distro to its pin")
    if distro not in pins:
        fail(f"{path}: the packaging pin has no entry for {distro}")
    # Only the entry of `distro` is checked. The entries of other distros can be read by other tools,
    # and a mistake in one of them doesn't break this build.
    pin = pins[distro]
    values = [pin.get(key) for key in PIN_KEYS] if isinstance(pin, dict) else []
    strings = [value for value in values if isinstance(value, str) and value]
    if not isinstance(pin, dict) or sorted(pin) != list(PIN_KEYS) or len(strings) != len(PIN_KEYS):
        fail(f"{path}: the packaging pin of {distro} must be an object of the strings {', '.join(PIN_KEYS)}")
    branch, commit, url = strings
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail(f"{path}: the packaging pin's commit {commit!r} for {distro} is not 40 lowercase hex digits")
    return url, branch, commit


def check_out(pin: Path, distro: str, out: Path) -> None:
    """Fill `out` with the tree at the commit that `pin` names for `distro`."""
    url, branch, commit = read_pin(pin, distro)
    subprocess.run(["git", "init", "--quiet", str(out)], check=True)
    # git resolves a relative url against its working directory, and the driver runs in the project root.
    # A pin can therefore name a repository in the project, such as the bundle in examples/package/rpm.
    # `git -C` would resolve the url against `out` instead.
    git = ["git", f"--git-dir={out / '.git'}", f"--work-tree={out}"]
    fetched = subprocess.run([*git, "fetch", "--quiet", "--", url, f"refs/heads/{branch}"])
    if fetched.returncode:
        fail(f"{pin}: cannot fetch branch {branch} from {url}")
    ancestor = subprocess.run(
        [*git, "merge-base", "--is-ancestor", commit, "FETCH_HEAD"], stderr=subprocess.DEVNULL
    )
    if ancestor.returncode:
        fail(f"{pin}: commit {commit} is not on branch {branch} of {url}")
    subprocess.run([*git, "checkout", "--quiet", "--detach", commit], check=True)
    shutil.rmtree(out / ".git")
    # The build drivers copy the files at the top of the tree into the build outside the chroot, and the
    # whole project is readable there. The copy follows symlinks, so a symlink to a file of the project
    # would put that file into the package.
    if links := sorted(path.name for path in out.iterdir() if path.is_symlink()):
        fail(f"{pin}: commit {commit} contains symlinks at the top of its tree: {', '.join(links)}")


def main(argv: list[str] | None = None) -> None:
    """Parse an invocation and check out the pinned packaging."""
    spec = specs.parse(Spec, "packaging", argv)
    check_out(Path(spec["pin"]), spec["distro"], Path(spec["out"]))


if __name__ == "__main__":
    main()
