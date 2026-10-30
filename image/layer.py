#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Apply ordered operations against one mounted root and capture its overlay delta."""

import glob  # noqa: F401  # Preload for Path.glob before entering the image chroot.
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

import specs
import util

import finalize
import installer
import rootfs


class LayerInstall(TypedDict):
    """What a layer adds to the install request it writes: which installer, and what to hide."""

    arch: str
    installer: str
    packages_dir: str
    langs: list[str]
    docs: bool


class Spec(TypedDict):
    lower: list[str]
    out: str
    work: str | None
    install: LayerInstall | None
    operations: list[object]


# Where a chrooted command sees the project, and so every declared input. /run is one of the
# tmpfs mounts apivfs puts over the mounted root, so the mount point never reaches the overlay
# upper the layer captures, and the path does not depend on where the project is checked out.
PROJECT = "/run/tine/project"


def _chroots(operation: list[object]) -> bool:
    return operation[0] == "run" and len(operation) == 4 and operation[3] is True


def _operation(value: object) -> list[object]:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        util.fail(f"image op is not a tagged array: {value!r}")
    return cast(list[object], value)


def _mapping(tag: str, field: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        util.fail(f"image op {tag!r} has invalid {field}: {raw!r}")
    return cast(dict[str, str], raw)


def _run(tag: str, raw_cmd: object, raw_env: object, cwd: str | None = None) -> None:
    if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(arg, str) for arg in raw_cmd):
        util.fail(f"image op {tag!r} has invalid cmd: {raw_cmd!r}")
    cmd = cast(list[str], raw_cmd)
    env = _mapping(tag, "env", raw_env)
    rc = subprocess.run(cmd, env=os.environ | env, cwd=cwd).returncode
    if rc != 0:
        util.fail(f"image op `{tag} {cmd}` failed (rc={rc})")


def _destination(tree: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        util.fail(f"image copy destination must be an absolute image path: {value!r}")
    return tree.joinpath(*path.parts[1:])


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            util.fail(f"cannot replace directory {destination} with symlink {source}")
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        shutil.copy2(source, destination, follow_symlinks=False)
    elif source.is_dir():
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            util.fail(f"cannot merge directory {source} into non-directory {destination}")
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            # Reflink each regular file; copytree recreates symlinks itself
            copy_function=lambda s, d: util.clone_file(Path(s), Path(d)),
            symlinks=True,
        )
    else:
        if destination.is_dir() and not destination.is_symlink():
            util.fail(f"cannot replace directory {destination} with file {source}")
        if destination.is_symlink():
            destination.unlink()
        util.clone_file(source, destination)


def _merge_os_release(tree: Path, raw_fields: object) -> None:
    """Merge quoted KEY="value" assignments into /usr/lib/os-release, replacing existing keys."""
    fields = dict(_mapping("os_release", "fields", raw_fields))
    for key, value in fields.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            util.fail(f"image op 'os_release' has invalid key: {key!r}")
        # Values are emitted double-quoted verbatim, so refuse anything needing escapes.
        if '"' in value or "\\" in value or "\n" in value:
            util.fail(f"image op 'os_release' value needs escaping: {value!r}")
    path = tree / "usr/lib/os-release"
    lines = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, _ = line.partition("=")
            if sep and key in fields:
                line = f'{key}="{fields.pop(key)}"'
            lines.append(line)
    lines += [f'{key}="{value}"' for key, value in fields.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _remove_glob(tree: Path, value: str) -> None:
    pattern = PurePosixPath(value)
    if not pattern.is_absolute() or ".." in pattern.parts:
        util.fail(f"image remove pattern must be an absolute image path: {value!r}")
    relative = str(PurePosixPath(*pattern.parts[1:]))
    matches = sorted(tree.glob(relative), key=lambda path: (len(path.parts), str(path)), reverse=True)
    for path in matches:
        util.remove_path(path)


def _normalize_mode(path: Path) -> None:
    """Normalize file permissions for path.

    See docs/user/images.md.
    """
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        path.chmod(0o755)
    elif stat.S_ISREG(mode):
        path.chmod(0o755 if mode & 0o111 else 0o644)
    # ignore symlinks (meaningless mode), whiteout nodes, and non-files


def _normalize_modes(tree: Path) -> None:
    _normalize_mode(tree)
    for path in tree.rglob("*"):
        _normalize_mode(path)


def _apply_filesystem(operation: list[object]) -> None:
    match operation:
        case ["mkdir", str(path)]:
            Path(path).mkdir(parents=True, exist_ok=True)
        case ["symlink", str(target), str(path)]:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(target)
        case ["write_file", str(path), str(content)]:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        case ["remove", str(path)]:
            _remove_glob(Path("/"), path)
        case _:
            util.fail(f"invalid image filesystem op: {operation!r}")


def _install(install: LayerInstall, target: Path, scratch: Path) -> None:
    """Install the layer's package closure into the mounted root."""
    request = installer.InstallSpec(
        arch=install["arch"],
        packages_dir=str(Path(install["packages_dir"]).absolute()),
        target=None,
        installroot=str(target),
        lower=[],
        work=None,
        box_config=False,
        langs=install["langs"],
        docs=install["docs"],
    )
    spec = specs.write(scratch / "install.spec.json", dict(request))
    rc = subprocess.run([install["installer"], "--spec", str(spec)]).returncode
    if rc != 0:
        util.fail(f"image package installation failed (rc={rc})")


def _apply(value: object, target: Path) -> None:
    """Apply one operation with its requested view of the mounted root."""
    operation = _operation(value)
    match operation:
        case ["run", raw_cmd, raw_env, bool(chroot)]:
            if not chroot:
                _run("run", raw_cmd, raw_env)
            else:
                with rootfs.chroot(target):
                    _run("run", raw_cmd, raw_env, cwd=PROJECT)
        case ["copy", str(source), str(destination)]:
            _copy(Path(source), _destination(target, destination))
        case ["os_release", raw_fields]:
            _merge_os_release(target, raw_fields)
        case ["depmod"]:
            finalize.depmod(target)
        case ["hwdb", bool(usr), bool(strict)]:
            finalize.hwdb(target, usr=usr, strict=strict)
        case ["locale_gen"]:
            finalize.locale_gen(target)
        case ["mkdir", _] | ["symlink", _, _] | ["write_file", _, _] | ["remove", _]:
            with rootfs.chroot(target):
                _apply_filesystem(operation)
        case _:
            util.fail(f"invalid image op: {operation!r}")


def main(argv: list[str] | None = None) -> None:
    spec = specs.parse(Spec, "image", argv)

    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    operations = [_operation(operation) for operation in spec["operations"]]
    for operation in operations:
        if operation[0] == "copy" and len(operation) == 3 and isinstance(operation[1], str):
            operation[1] = str(Path(operation[1]).absolute())
    install = spec["install"]

    # A chrooted command names an artifact exactly as a box command does, so the project is
    # mounted for the whole layer whenever one asks for it.
    binds = [(os.getcwd(), PROJECT)] if any(_chroots(op) for op in operations) else []

    lower = spec["lower"]
    if lower:
        if spec["work"] is None:
            util.fail("image lower stack needs a work overlay directory")
        mounted = rootfs.rootfs(
            "/buildroot",
            lowers=lower,
            upperdir=out,
            workdir=spec["work"],
            apivfs=True,
            binds=binds,
        )
    else:
        if spec["work"] is not None:
            util.fail("image work overlay directory requires a lower stack")
        mounted = rootfs.rootfs(
            "/buildroot",
            bind=out,
            capture_bind=True,
            apivfs=True,
            binds=binds,
        )

    with mounted as target, tempfile.TemporaryDirectory(prefix="layer.") as scratch:
        # Packages land before the operations so every one of them sees what this layer installs.
        if install is not None:
            _install(install, target, Path(scratch))
        for operation in operations:
            _apply(operation, target)
    _normalize_modes(out)
    print(f"image: applied {len(operations)} ops over {len(lower)} lower(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
