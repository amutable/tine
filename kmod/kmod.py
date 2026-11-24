# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Pick the kernel modules a UKI's per-kernel initrd carries and close over what they need.

A pattern matches a trailing run of a module's path components; a leading slash anchors it at the
modules root instead, and a trailing slash takes everything below a directory. The dependency
and firmware closure comes from libkmod, so the initrd resolves modules through the very index modprobe
reads at boot, and it is bound with an empty configuration vector so no modprobe.d from the box can
reach the result.
"""

import ctypes
import fnmatch
import json
import os
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Self, cast

from util import fail

MODULES = PurePosixPath("usr/lib/modules")
FIRMWARE = PurePosixPath("usr/lib/firmware")
BOOT = PurePosixPath("boot")
# Every module spelling depmod indexes. The suffix is stripped before a pattern sees a path.
SUFFIXES = (".ko", ".ko.gz", ".ko.xz", ".ko.zst")
_COMPRESSION = ("", ".gz", ".xz", ".zst")
_LINK_DEPTH = 40

# An opaque libkmod pointer, as ctypes hands one back.
type Handle = int


class Kernel(NamedTuple):
    """One installed kernel: the release its modules are indexed under, and the image itself."""

    release: str
    path: Path


def kernels(tree: Path) -> list[Kernel]:
    """Every kernel an image installs, wherever its distribution puts the image itself.

    Distributions following kernel-install put the kernel beside its modules; Debian keeps it in
    `/boot` under a name carrying the release. A tree can hold either, so both are looked for and
    the release is what ties an image back to the modules that go with it.
    """
    found = []
    modules = tree / MODULES
    if modules.is_dir():
        for directory in sorted(modules.iterdir()):
            kernel = directory / "vmlinuz"
            if directory.is_dir() and kernel.is_file():
                found.append(Kernel(directory.name, kernel))
    boot = tree / BOOT
    if boot.is_dir():
        indexed = {kernel.release for kernel in found}
        for kernel in sorted(boot.glob("vmlinuz-*")):
            release = kernel.name.removeprefix("vmlinuz-")
            if kernel.is_file() and release not in indexed:
                found.append(Kernel(release, kernel))
    return found


class Entry(NamedTuple):
    """One path the modules cpio carries, and why it is in there."""

    path: PurePosixPath
    kind: str
    size: int | None = None
    # A module a pattern named itself, rather than one the closure pulled in.
    selected: bool = False
    needed_by: tuple[str, ...] = ()
    declared_by: tuple[str, ...] = ()


class Closure(NamedTuple):
    """What libkmod answered for one set of module names."""

    # The module file each name resolves to, None where the kernel has it built in.
    modules: dict[str, Path | None]
    needed_by: dict[str, tuple[str, ...]]
    firmware: dict[str, tuple[str, ...]]
    missing: list[str]


class Selection(NamedTuple):
    """What one pattern list resolved to, and what did not resolve."""

    kernel: str
    patterns: list[str]
    entries: list[Entry]
    modules: list[PurePosixPath]
    firmware: list[PurePosixPath]
    unmatched: list[str]
    missing: list[str]
    missing_firmware: list[str]


# Patterns.


class _Pattern(NamedTuple):
    glob: str
    exclude: bool


def _normalize(name: str) -> str:
    """kmod reads '_' and '-' in a module name as the same character."""
    return name.replace("_", "-")


def _normalize_glob(glob: str) -> str:
    normalized = ""
    while glob:
        head, bracket, glob = glob.partition("[")
        normalized += _normalize(head) + bracket
        if not bracket:
            break
        # A character class spells both characters out, so leave it alone.
        members, close, glob = glob.partition("]")
        normalized += members + close
    return normalized


def _compile(pattern: str) -> _Pattern:
    exclude = pattern.startswith("-")
    glob = _normalize_glob(pattern[1:] if exclude else pattern)
    return _Pattern(glob + "*" if glob.endswith("/") else glob, exclude)


def _matches(name: str, pattern: _Pattern) -> bool:
    glob = pattern.glob
    return (
        # A leading slash anchors the pattern at the root of the module directory.
        (glob.startswith("/") and fnmatch.fnmatchcase(f"/{name}", glob))
        # Any other pattern holding a slash matches a trailing run of path components.
        or ("/" in glob and fnmatch.fnmatchcase(f"/{name}", f"*/{glob}"))
        or fnmatch.fnmatchcase(name.rpartition("/")[2], glob)
    )


def module_name(rel: PurePosixPath) -> str:
    """The name kmod knows a module file by."""
    return _normalize(rel.name.partition(".")[0])


def matchable(rel: PurePosixPath) -> str:
    """The form a pattern sees: no module suffix, dashes for underscores."""
    name = rel.name
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _normalize(str(rel.with_name(name)))


def module_files(modulesd: Path) -> list[PurePosixPath]:
    """Every module file under `modulesd`, relative to it, in a stable order."""
    return sorted(
        PurePosixPath(path.relative_to(modulesd))
        for path in modulesd.rglob("*.ko*")
        if path.name.endswith(SUFFIXES) and path.is_file()
    )


def select(modulesd: Path, patterns: Sequence[str]) -> tuple[list[PurePosixPath], list[str]]:
    """The modules the patterns select, and the patterns that selected nothing."""
    compiled = [_compile(pattern) for pattern in patterns]
    picked = []
    matched: set[int] = set()
    for rel in module_files(modulesd):
        name = matchable(rel)
        keep = False
        # Evaluated in order, so the last pattern to match decides.
        for index, pattern in enumerate(compiled):
            if _matches(name, pattern):
                matched.add(index)
                keep = not pattern.exclude
        if keep:
            picked.append(rel)
    # A pattern naming something this kernel builds in selects no file, which is ordinary across
    # kernel configurations and worth no warning.
    for rel in builtin_paths(modulesd):
        name = matchable(rel)
        matched |= {index for index, pattern in enumerate(compiled) if _matches(name, pattern)}
    return picked, [pattern for index, pattern in enumerate(patterns) if index not in matched]


def builtin_paths(modulesd: Path) -> list[PurePosixPath]:
    """The modules linked into the kernel, as depmod records them."""
    index = modulesd / "modules.builtin"
    if not index.exists():
        return []
    return [PurePosixPath(line) for line in index.read_text().splitlines() if line]


# libkmod.


def _load() -> ctypes.CDLL:
    try:
        lib = ctypes.CDLL("libkmod.so.2", use_errno=True)
    except OSError as error:
        fail(f"kmod: selecting kernel modules needs libkmod: {error}")
    ptr = ctypes.c_void_p
    out = ctypes.POINTER(ctypes.c_void_p)
    text = ctypes.c_char_p
    for name, restype, argtypes in (
        ("kmod_new", ptr, (text, ctypes.POINTER(text))),
        ("kmod_unref", ptr, (ptr,)),
        ("kmod_load_resources", ctypes.c_int, (ptr,)),
        ("kmod_list_next", ptr, (ptr, ptr)),
        ("kmod_module_new_from_name", ctypes.c_int, (ptr, text, out)),
        ("kmod_module_unref", ptr, (ptr,)),
        ("kmod_module_unref_list", ctypes.c_int, (ptr,)),
        ("kmod_module_get_module", ptr, (ptr,)),
        ("kmod_module_get_name", text, (ptr,)),
        ("kmod_module_get_path", text, (ptr,)),
        ("kmod_module_get_dependencies", ptr, (ptr,)),
        ("kmod_module_get_softdeps", ctypes.c_int, (ptr, out, out)),
        ("kmod_module_get_info", ctypes.c_int, (ptr, out)),
        ("kmod_module_info_get_key", text, (ptr,)),
        ("kmod_module_info_get_value", text, (ptr,)),
        ("kmod_module_info_free_list", None, (ptr,)),
    ):
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


class Kmod:
    """libkmod scoped to one image's module directory."""

    def __init__(self, modulesd: Path) -> None:
        # Every lookup is answered out of depmod's binary index. Without it there are no dependencies
        # to find and the initrd would come out quietly incomplete.
        if not (modulesd / "modules.dep.bin").exists():
            fail(f"kmod: {modulesd} has no modules.dep.bin, so depmod never ran for it")
        self._lib = _load()
        # An empty configuration vector keeps the build hermetic: libkmod reads no modprobe.d.
        ctx = self._lib.kmod_new(os.fsencode(modulesd), (ctypes.c_char_p * 1)(None))
        if not ctx:
            fail(f"kmod: cannot read the module directory {modulesd}")
        self._ctx: Handle = ctx
        self._lib.kmod_load_resources(self._ctx)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._lib.kmod_unref(self._ctx)

    def _each(self, head: Handle | None) -> Iterator[Handle]:
        entry = head
        while entry:
            yield entry
            entry = cast(Handle | None, self._lib.kmod_list_next(head, entry))

    @contextmanager
    def _lookup(self, name: str) -> Generator[Handle | None]:
        mod = ctypes.c_void_p()
        if self._lib.kmod_module_new_from_name(self._ctx, name.encode(), ctypes.byref(mod)) < 0:
            yield None
            return
        try:
            yield mod.value
        finally:
            self._lib.kmod_module_unref(mod)

    def _named(self, entry: Handle) -> str:
        mod = self._lib.kmod_module_get_module(entry)
        try:
            return _normalize(self._lib.kmod_module_get_name(mod).decode())
        finally:
            self._lib.kmod_module_unref(mod)

    def info(self, mod: Handle) -> list[tuple[str, str]]:
        """The module's modinfo fields, from its ELF or, for a builtin, from depmod's index."""
        head = ctypes.c_void_p()
        if self._lib.kmod_module_get_info(mod, ctypes.byref(head)) < 0:
            return []
        try:
            fields: list[tuple[str, str]] = []
            for entry in self._each(head.value):
                key = self._lib.kmod_module_info_get_key(entry)
                value = self._lib.kmod_module_info_get_value(entry)
                if key is not None and value is not None:
                    fields.append((key.decode(), value.decode()))
            return fields
        finally:
            self._lib.kmod_module_info_free_list(head)

    def dependencies(self, mod: Handle) -> list[str]:
        """Everything the module needs loaded first; depmod already flattened this transitively."""
        head = self._lib.kmod_module_get_dependencies(mod)
        if not head:
            return []
        try:
            return [self._named(entry) for entry in self._each(head)]
        finally:
            self._lib.kmod_module_unref_list(head)

    def softdeps(self, mod: Handle) -> list[str]:
        pre, post = ctypes.c_void_p(), ctypes.c_void_p()
        if self._lib.kmod_module_get_softdeps(mod, ctypes.byref(pre), ctypes.byref(post)) < 0:
            return []
        names: list[str] = []
        for head in (pre, post):
            if not head.value:
                continue
            try:
                names += [self._named(entry) for entry in self._each(head.value)]
            finally:
                self._lib.kmod_module_unref_list(head)
        return names

    def closure(self, names: Iterable[str]) -> Closure:
        """Resolve names to their modules and firmware, recording what pulled each one in."""
        modules: dict[str, Path | None] = {}
        needed_by: dict[str, set[str]] = {}
        firmware: dict[str, set[str]] = {}
        missing: list[str] = []
        seen: set[str] = set()
        todo = sorted(set(names))
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            with self._lookup(name) as mod:
                if mod is None:
                    missing.append(name)
                    continue
                path = self._lib.kmod_module_get_path(mod)
                fields = self.info(mod)
                if path is None and not fields:
                    # Neither a file nor a builtin: something depends on a module nobody installed.
                    missing.append(name)
                    continue
                modules[name] = Path(os.fsdecode(path)) if path is not None else None
                for key, value in fields:
                    if key == "firmware":
                        firmware.setdefault(value, set()).add(name)
                for dep in self.dependencies(mod) + self.softdeps(mod):
                    # Every requester is recorded, not just the one that got here first, so the
                    # answer to "why is this module here" does not depend on traversal order.
                    needed_by.setdefault(dep, set()).add(name)
                    if dep not in seen:
                        todo.append(dep)
        return Closure(
            modules,
            {name: tuple(sorted(requesters)) for name, requesters in needed_by.items()},
            {name: tuple(sorted(requesters)) for name, requesters in firmware.items()},
            sorted(missing),
        )


# Firmware.


def _chase(tree: Path, rel: PurePosixPath) -> Iterator[PurePosixPath]:
    """Yield every symlink on the way to `rel`'s target, and the target itself.

    Firmware trees link whole directories as well as files, so any component of the path may be a
    link and the initrd needs each one to resolve the way the booted system resolves it.
    """
    todo = list(rel.parts)
    current = PurePosixPath()
    for _ in range(_LINK_DEPTH):
        while todo:
            part = todo.pop(0)
            if part == "..":
                current = current.parent
            elif part not in (".", "/"):
                current = current / part
                if (tree / current).is_symlink():
                    break
        else:
            yield current
            return
        yield current
        target = PurePosixPath(os.readlink(tree / current))
        current = PurePosixPath() if target.is_absolute() else current.parent
        todo = [*target.parts, *todo]
    fail(f"kmod: {rel} does not resolve within {_LINK_DEPTH} symlinks")


def carry(tree: Path, rel: PurePosixPath) -> set[PurePosixPath]:
    """`rel` and the symlinks leading to it, minus whatever the image turns out not to have.

    Both trees link at files no installed package ships, and packing a path that is not there would
    fail the build over something the image never needed.
    """
    return {path for path in _chase(tree, rel) if (tree / path).is_symlink() or (tree / path).exists()}


def firmware_files(tree: Path, names: Iterable[str]) -> tuple[dict[str, list[PurePosixPath]], list[str]]:
    """The files each named modinfo entry refers to, and the entries nothing satisfies."""
    found: dict[str, list[PurePosixPath]] = {}
    missing: list[str] = []
    for name in sorted(set(names)):
        # An initrd holds what modules need, and nothing a module claims can reach out of the tree.
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            fail(f"kmod: a module declares {name!r}, which is not firmware below /{FIRMWARE}")
        if any(character in name for character in "*?["):
            candidates = sorted(
                PurePosixPath(path.relative_to(tree)) for path in (tree / FIRMWARE).glob(name)
            )
        else:
            candidates = [FIRMWARE / f"{name}{suffix}" for suffix in _COMPRESSION]
        matches = [rel for rel in candidates if (tree / rel).exists() or (tree / rel).is_symlink()]
        if not matches:
            missing.append(name)
            continue
        files: set[PurePosixPath] = set()
        for rel in matches:
            files |= carry(tree, rel)
        found[name] = sorted(files)
    return found, missing


# Assembly.


def entries(
    tree: Path,
    kver: str,
    modules: Mapping[PurePosixPath, Entry],
    firmware: Mapping[PurePosixPath, Entry],
) -> list[Entry]:
    """Everything the modules cpio carries, parents included, in extraction order."""
    modulesd = MODULES / kver
    found = {**modules, **firmware}
    # udev and modprobe read depmod's indexes at boot. A reference to a module the cpio leaves out
    # only fails that one modprobe; a missing index breaks autoloading outright.
    for path in (tree / modulesd).glob("modules*"):
        found.setdefault(modulesd / path.name, Entry(modulesd / path.name, "index"))
    vdso = tree / modulesd / "vdso"
    if vdso.is_dir():
        for path in vdso.iterdir():
            found.setdefault(modulesd / "vdso" / path.name, Entry(modulesd / "vdso" / path.name, "vdso"))
    for path in list(found):
        for parent in path.parents:
            if str(parent) != ".":
                found.setdefault(parent, Entry(parent, "directory"))
    return sorted(
        entry if entry.kind == "directory" else entry._replace(size=(tree / entry.path).lstat().st_size)
        for entry in found.values()
    )


def manifest(selection: Selection, *, archive_bytes: int) -> str:
    """Render the selection as the report that ships beside the UKI.

    Everything the driver decided lands here, including what it left out, so that a UKI that boots to
    no root can be answered from the build's own artifacts rather than by rerunning it.
    """
    report: dict[str, object] = {
        "kernel": selection.kernel,
        "patterns": selection.patterns,
        "unmatched_patterns": selection.unmatched,
        "missing_modules": selection.missing,
        "missing_firmware": selection.missing_firmware,
        "totals": {
            "modules": len(selection.modules),
            "firmware": len(selection.firmware),
            "entries": len(selection.entries),
            "content_bytes": sum(entry.size or 0 for entry in selection.entries),
            "archive_bytes": archive_bytes,
        },
        "entries": [
            {
                "path": str(entry.path),
                "kind": entry.kind,
                **({} if entry.size is None else {"size": entry.size}),
                **({"selected": True} if entry.selected else {}),
                **({"needed_by": list(entry.needed_by)} if entry.needed_by else {}),
                **({"declared_by": list(entry.declared_by)} if entry.declared_by else {}),
            }
            for entry in selection.entries
        ],
    }
    return json.dumps(report, indent=2) + "\n"


def initrd_modules(tree: Path, kver: str, patterns: Sequence[str]) -> Selection:
    """Select modules for the per-kernel initrd, close over them, and list what to pack."""
    modulesd = tree / MODULES / kver
    picked, unmatched = select(modulesd, patterns)
    builtin = {module_name(rel) for rel in builtin_paths(modulesd)}
    # Seeded by name, so a selected file that kmod resolves elsewhere packs the path modprobe would
    # load rather than the one the pattern happened to walk into.
    selected = {module_name(rel) for rel in picked}
    seeds = sorted(selected)
    firmwared = (tree / FIRMWARE).is_dir()
    if firmwared:
        # A builtin driver loads its firmware from the initrd too, and no pattern can select one.
        seeds += sorted(builtin)

    closure = Closure({}, {}, {}, [])
    # Asking for no module at all is a valid answer, and one this image need not own an index for.
    if seeds:
        with Kmod(modulesd) as kmod:
            closure = kmod.closure(seeds)
        # A kernel that ships no modules.builtin.modinfo answers nothing about what it holds, which
        # is not the image failing to install anything.
        closure = closure._replace(missing=[name for name in closure.missing if name not in builtin])

    module_entries: dict[PurePosixPath, Entry] = {}
    for name, path in closure.modules.items():
        if path is None:  # built into the kernel, so there is no file to carry
            continue
        rel = PurePosixPath(path.relative_to(tree))
        # A module directory can link one module at another, and half a link boots nothing.
        for carried in sorted(carry(tree, rel)):
            module_entries[carried] = Entry(
                carried,
                "module",
                selected=name in selected,
                needed_by=closure.needed_by.get(name, ()),
            )

    firmware_entries: dict[PurePosixPath, Entry] = {}
    absent: list[str] = []
    if firmwared:
        files, absent = firmware_files(tree, closure.firmware)
        for name, carried in files.items():
            for path in carried:
                firmware_entries[path] = Entry(path, "firmware", declared_by=closure.firmware[name])

    return Selection(
        kver,
        list(patterns),
        entries(tree, kver, module_entries, firmware_entries),
        sorted(module_entries),
        sorted(firmware_entries),
        unmatched,
        closure.missing,
        absent,
    )
