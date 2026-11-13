# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Finalizing an image: the generators a layer persists, and the preparation a terminal applies.

A generator turns installed package content into the derived state a system boots from, such as
the module and hardware databases or the locale archive. Each is one image operation of its own,
and what it writes is an ordinary file, so a layer captures it. Terminal preparation is the other
half: the authored tmpfiles snippets a driver applies to the stack it just mounted, which no
layer can hold.
"""

import bz2
import contextlib
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TypedDict

from util import fail

import rootfs
import storage
import uapi16


class ImageSpec(TypedDict):
    """The half of a terminal driver's spec that names the image itself."""

    lower: list[str]
    tmpfiles: list[str]


_WORLD_WRITABLE = ("tmp", "var/tmp")


@contextlib.contextmanager
def image(
    spec: ImageSpec,
    *,
    program: str,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
    base: int = 0,
    readonly: bool = True,
    strategy: storage.Strategy = "auto",
) -> Generator[Path]:
    """Mount the image stack the spec names and yield the finalized tree."""
    with _root(
        spec, program=program, binds=binds, base=base, readonly=readonly, strategy=strategy
    ) as restored:
        yield restored.path


@contextlib.contextmanager
def _root(
    spec: ImageSpec,
    *,
    program: str,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
    base: int = 0,
    readonly: bool = True,
    strategy: storage.Strategy = "auto",
) -> Generator[storage.Root]:
    snapshot = storage.Snapshot(spec["lower"])
    if base:
        storage.delta(snapshot, base)
    normalize_snapshot(snapshot)
    with (
        storage.root(
            snapshot,
            # Authored tmpfiles rules write into the root. They fail on a read-only root.
            readonly=readonly and not spec["tmpfiles"],
            strategy=strategy,
        ) as restored,
        rootfs.rootfs("/buildroot", bind=restored.path, binds=binds) as tree,
    ):
        apply_tmpfiles(tree, spec["tmpfiles"], program=program)
        yield storage.Root(tree, restored.upper)


def apply_tmpfiles(tree: Path, snippets: list[str], *, program: str) -> None:
    """Apply authored tmpfiles configuration beneath `tree`."""
    if not snippets:
        return
    tmpfiles = shutil.which("systemd-tmpfiles")
    if tmpfiles is None:
        fail(f"{program}: systemd-tmpfiles is required to finalize this image")

    config = "".join(snippet if snippet.endswith("\n") else snippet + "\n" for snippet in snippets)
    subprocess.run(
        [tmpfiles, "--create", f"--root={tree}", "-"],
        input=config,
        text=True,
        check=True,
    )


def _box_tool(name: str, what: str) -> str:
    path = shutil.which(name)
    if path is None:
        fail(f"{what}: this box carries no {name}")
    return path


def _run(what: str, cmd: list[str]) -> None:
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        fail(f"{what}: `{' '.join(cmd)}` failed (rc={rc})")


def kernel_versions(tree: Path) -> list[str]:
    """The module directories holding a kernel, which are the ones depmod has work to do in."""
    modules = tree / "usr/lib/modules"
    if not modules.is_dir():
        return []
    # A directory holding nothing but `updates` belongs to a kernel this image does not install.
    return sorted(
        entry.name
        for entry in modules.iterdir()
        if entry.is_dir() and {child.name for child in entry.iterdir()} - {"updates"}
    )


# The directories depmod reads its configuration from by default, in its order of precedence.
_DEPMOD_CONFIG = (
    "etc/depmod.d",
    "run/depmod.d",
    "usr/local/lib/depmod.d",
    "usr/lib/depmod.d",
    "lib/depmod.d",
)


# The suffixes of the module files that depmod indexes.
_MODULE_SUFFIXES = (".ko", ".ko.gz", ".ko.xz", ".ko.zst")


def depmod_config(root: Path) -> dict[str, str]:
    """The contents of the depmod.d files below `root` that depmod reads, by file name."""
    files: dict[str, str] = {}
    for directory in _DEPMOD_CONFIG:
        path = root / directory
        if not path.is_dir():
            continue
        for entry in sorted(path.iterdir()):
            # depmod reads only files named `*.conf`. A file in an earlier directory hides a file
            # with the same name in a later directory.
            if entry.name.startswith(".") or not entry.name.endswith(".conf") or entry.name in files:
                continue
            if entry.is_file():
                files[entry.name] = entry.read_text(encoding="utf-8")
    for name, text in files.items():
        # depmod searches an `external` directory without the `--basedir` prefix, so it would
        # index the box's directory instead of the image's.
        if any(line.split()[:1] == ["external"] for line in text.splitlines()):
            fail(f"depmod: {name} lists an external module directory, which depmod cannot index in an image")
    return files


def check_module_symlinks(tree: Path, version: str) -> None:
    """Fail on a module that is an absolute symlink, which depmod would resolve in the box."""
    for directory, dirnames, filenames in os.walk(tree / "usr/lib/modules" / version):
        # depmod does not index the `build` and `source` directories of a kernel.
        dirnames[:] = [name for name in dirnames if name not in ("build", "source")]
        for name in filenames:
            path = Path(directory, name)
            if name.endswith(_MODULE_SUFFIXES) and path.is_symlink() and os.readlink(path).startswith("/"):
                fail(f"depmod: the module {path.relative_to(tree)} is an absolute symlink")


def depmod_command(tool: str, tree: Path, version: str, *, config: Path, moduledir: bool) -> list[str]:
    """The depmod invocation indexing one kernel's modules with the configuration in `config`."""
    return [
        tool,
        "--all",
        f"--basedir={tree}",
        # kmod 34 added `--moduledir`. Earlier versions always index /lib/modules, which a merged
        # /usr links to /usr/lib/modules.
        *(["--moduledir=/usr/lib/modules"] if moduledir else []),
        # depmod reads no default configuration directory once `--config` is given.
        f"--config={config}",
        version,
    ]


def depmod(tree: Path) -> None:
    """Rebuild the module indexes modprobe and udev read, for every kernel the image installs."""
    versions = kernel_versions(tree)
    if not versions:
        return
    tool = _box_tool("depmod", "depmod")
    usage = subprocess.run([tool, "--help"], capture_output=True, text=True, check=True).stdout
    # `--basedir` does not move the directories depmod reads its configuration from. The driver
    # reads the image's configuration itself, inside the image's root, so that an absolute symlink
    # in the image resolves to a file of the image.
    with rootfs.chroot(tree) as root:
        files = depmod_config(root)
    for version in versions:
        check_module_symlinks(tree, version)
    with tempfile.TemporaryDirectory(prefix="depmod.") as scratch:
        config = Path(scratch)
        for name, text in files.items():
            (config / name).write_text(text, encoding="utf-8")
        for version in versions:
            print(f"depmod: {version}", file=sys.stderr)
            cmd = depmod_command(tool, tree, version, config=config, moduledir="--moduledir" in usage)
            _run("depmod", cmd)


def hwdb_command(tool: str, tree: Path, *, usr: bool, strict: bool) -> list[str]:
    """The systemd-hwdb invocation building the binary database from the image's hwdb.d."""
    return [
        tool,
        f"--root={tree}",
        *(["--usr"] if usr else []),
        *(["--strict"] if strict else []),
        "update",
    ]


def hwdb(tree: Path, *, usr: bool, strict: bool) -> None:
    """Compile the image's hardware database, which udev reads in binary form and never sources."""
    if not any((tree / source).is_dir() for source in ("usr/lib/udev/hwdb.d", "etc/udev/hwdb.d")):
        return
    _run("hwdb", hwdb_command(_box_tool("systemd-hwdb", "hwdb"), tree, usr=usr, strict=strict))
    # A database in /etc would shadow the one in /usr the image now ships forever.
    if usr:
        (tree / "etc/udev/hwdb.bin").unlink(missing_ok=True)


# The directories locale-gen searches for locale sources, in its order.
_I18N = ("usr/local/share/i18n", "usr/share/i18n")


def locale_entries(root: Path) -> list[tuple[str, str]] | None:
    """The locale and charset of every entry in /etc/locale.gen below `root`.

    The result is None when the file is missing or empty, which is when locale-gen leaves the archive
    alone.
    """
    config = root / "etc/locale.gen"
    if not config.is_file() or config.stat().st_size == 0:
        return None
    entries = []
    for line in config.read_text(encoding="utf-8").splitlines():
        fields = line.split(maxsplit=1)
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) < 2:
            fail(f"locale_gen: the entry {line.strip()!r} in /etc/locale.gen has no charset")
        entries.append((fields[0], fields[1].strip()))
    return entries


def locale_source(root: Path, locale: str) -> Path:
    """The source definition for `locale` below `root`, searched the way locale-gen searches it."""
    base = locale.split(".", 1)[0].split("@", 1)[0]
    modifier = "@" + locale.split("@", 1)[1] if "@" in locale else ""
    for name in (locale, base + modifier):
        for i18n in _I18N:
            source = root / i18n / "locales" / name
            if source.is_file():
                return source.resolve()
    fail(f"locale_gen: the image has no source definition for {locale}")


def _charmap_names(path: Path) -> list[str]:
    """The names that the header of the charmap `path` declares, which localedef matches aliases against."""
    names = []
    with (
        gzip.open(path, "rt", encoding="utf-8", errors="replace")
        if path.name.endswith(".gz")
        else bz2.open(path, "rt", encoding="utf-8", errors="replace")
        if path.name.endswith(".bz2")
        else path.open(encoding="utf-8", errors="replace")
    ) as charmap:
        for line in charmap:
            # The header ends where the character definitions start.
            if "CHARMAP" in line:
                break
            fields = line.split()
            if fields[:1] == ["<code_set_name>"] and len(fields) > 1:
                names.append(fields[1])
            elif fields[:2] == ["%", "alias"] and len(fields) > 2:
                names.append(fields[2])
    return names


def charmap_name(root: Path, charset: str) -> str:
    """The name of the charmap below `root` for `charset`, which can be an alias of the charmap."""
    for i18n in _I18N:
        for suffix in ("", ".gz", ".bz2"):
            if (root / i18n / "charmaps" / (charset + suffix)).is_file():
                return charset
    # Without --force, localedef fails on an alias. With --force, it looks for the alias among the
    # box's charmaps only. The driver looks for the alias among the image's charmaps instead.
    charmaps = root / "usr/share/i18n/charmaps"
    if charmaps.is_dir():
        for path in sorted(charmaps.iterdir()):
            if path.is_file() and charset.lower() in (name.lower() for name in _charmap_names(path)):
                return path.name.removesuffix(".gz").removesuffix(".bz2")
    fail(f"locale_gen: the image has no charmap for {charset}")


def localedef_command(
    tool: str, tree: Path, source: Path, charmap: str, locale: str, alias: Path | None
) -> list[str]:
    """The localedef invocation adding one locale to the image's locale archive."""
    # locale-gen also passes --force. With --force, localedef writes a locale whose definition has
    # errors. It also exits with 0 when the charmap is missing.
    return [
        tool,
        f"--prefix={tree}",
        f"--inputfile={source}",
        f"--charmap={charmap}",
        *([f"--alias-file={alias}"] if alias is not None else []),
        locale,
    ]


def _in_tree(tree: Path, path: Path) -> Path:
    return tree / str(path).lstrip("/")


def locale_gen(tree: Path) -> None:
    """Generate the locales /etc/locale.gen asks for, for the distributions that work that way."""
    # The driver reads the image's files inside the image's root, so that an absolute symlink in the
    # image resolves to a file of the image.
    with rootfs.chroot(tree) as root:
        entries = locale_entries(root)
        if entries is None:
            return
        # locale-gen deletes the archive before it generates the locales that /etc/locale.gen lists.
        (root / "usr/lib/locale/locale-archive").unlink(missing_ok=True)
        locales = [
            (locale, locale_source(root, locale), charmap_name(root, charset)) for locale, charset in entries
        ]
        # Arch's glibc contains locale.alias and its locale-gen passes it. Debian contains no
        # locale.alias, and its locale-gen passes none.
        alias = root / "usr/share/locale/locale.alias"
        alias_file = alias.resolve() if alias.is_file() else None
    if not locales:
        return
    tool = _box_tool("localedef", "locale_gen")
    # localedef searches I18NPATH for the files that a locale source includes with `copy` and for
    # the charmap. Without I18NPATH, it reads the box's copies.
    env = os.environ | {"I18NPATH": ":".join(str(tree / i18n) for i18n in _I18N)}
    for locale, source, charmap in locales:
        print(f"locale_gen: {locale} {charmap}", file=sys.stderr)
        cmd = localedef_command(
            tool,
            tree,
            _in_tree(tree, source),
            charmap,
            locale,
            _in_tree(tree, alias_file) if alias_file is not None else None,
        )
        # localedef opens the charmap and the files that `copy` includes relative to its working
        # directory before it searches I18NPATH. The root of the image contains no file with such a
        # name.
        rc = subprocess.run(cmd, env=env, cwd=tree).returncode
        # localedef exits with 1 for a warning and for a failure to write the archive, and with 4 when
        # it wrote nothing. Only 0 means that the archive contains the locale.
        if rc != 0:
            fail(f"locale_gen: `{' '.join(cmd)}` failed (rc={rc})")


def normalize_snapshot(snapshot: storage.Snapshot) -> None:
    for obj in snapshot.objects:
        if obj["type"] == "dir":
            obj["mode"] = 0o1777 if obj.get("name") in _WORLD_WRITABLE else 0o755
        elif obj["type"] == "reg":
            obj["mode"] = 0o755 if obj["mode"] & 0o111 else 0o644


def _remove(objects: dict[str, uapi16.Object], relative: str, epoch: int) -> None:
    path = Path(relative)
    name = storage.resolve(objects, relative, follow=False)
    removed = False
    for child in list(objects):
        if child == name or child.startswith(name + "/"):
            del objects[child]
            removed = True
    while True:
        path = path.parent
        name = storage.resolve(objects, str(path))
        directory = objects.get(name)
        if directory is None or directory["type"] != "dir":
            return
        if removed:
            # Removing a file from a real tree sets the mtime of its directory to the current time.
            # snapshot() clamps that time to the epoch.
            directory["mTimeNSec"] = epoch * 10**9
        # remove_path() stops at a parent that is a symlink.
        if name == "." or objects[storage.resolve(objects, str(path), follow=False)]["type"] == "lnk":
            return
        if any(child.startswith(name + "/") for child in objects):
            return
        del objects[name]
        removed = True


@contextlib.contextmanager
def snapshot(
    spec: ImageSpec,
    *,
    program: str,
    epoch: int,
    pkgdb_paths: Sequence[str] = (),
) -> Generator[storage.Snapshot]:
    with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
        if spec["tmpfiles"]:
            out = Path(scratch) / "snapshot"
            with _root(spec, program=program, readonly=False) as restored:
                storage.capture(restored, out, spec["lower"], epoch=epoch)
            result = storage.Snapshot([*spec["lower"], out])
        else:
            result = storage.Snapshot(spec["lower"])
            normalize_snapshot(result)
        objects = storage.by_name(result.objects)
        for relative in pkgdb_paths:
            _remove(objects, relative, epoch)
        result.objects = list(objects.values())
        uapi16.renumber(result.objects)
        for obj in result.objects:
            obj["mTimeNSec"] = min(obj.get("mTimeNSec", epoch * 10**9), epoch * 10**9)
        yield result
