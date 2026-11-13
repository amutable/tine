# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Store complete filesystem snapshots independently of artifact filesystem metadata."""

import os
import re
import stat
import tempfile
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from util import ReflinkUnavailable, clone_file, fail, reflink

import rootfs
import uapi16
from isolation import Bind, Overlay

type Strategy = Literal["auto", "copy", "overlay"]


class Snapshot:
    def __init__(self, stores: Sequence[str | Path]) -> None:
        self.stores = [Path(store) for store in stores]
        self.objects: list[uapi16.Object] = (
            uapi16.read(self.stores[-1] / uapi16.MANIFEST)
            if self.stores
            else [{"mediaType": uapi16.MEDIA_TYPE, "type": "dir", "mode": 0o755}]
        )
        self.blobs: dict[str, Path] = {}
        for store in self.stores:
            for blob in (store / "blobs").iterdir():
                self.blobs.setdefault(blob.name, blob)
        self._validate()

    def _validate(self) -> None:
        directories = {""}
        groups: dict[int, uapi16.Object] = {}
        for obj in self.objects[1:]:
            name = obj["name"]
            if name.rpartition("/")[0] not in directories:
                fail(f"storage: {name!r} does not follow its parent directory")
            if obj["type"] == "dir":
                directories.add(name)
            if token := obj.get("inodeToken"):
                if obj["type"] == "dir":
                    fail(f"storage: directory {name!r} has an inodeToken")
                inode = obj.copy()
                del inode["name"]
                if groups.setdefault(token, inode) != inode:
                    fail(f"storage: hardlink group {token} has conflicting metadata")
            if obj["type"] == "reg":
                digest = obj["sha256"]
                if obj["contents"] != [{"file": f"blobs/{digest}"}]:
                    fail(f"storage: unsupported content reference for {name!r}")
                self.content(obj)

    def content(self, obj: uapi16.Object) -> Path:
        digest = obj["sha256"]
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            fail(f"storage: invalid SHA-256 digest {digest!r}")
        if digest not in self.blobs:
            fail(f"storage: missing blob {digest} for {obj['name']!r}")
        path = self.blobs[digest]
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size != obj["size"]:
            fail(f"storage: invalid blob {digest} for {obj['name']!r}")
        return path


@dataclass(frozen=True)
class Root:
    path: Path
    upper: Path | None = None

    def entries(self) -> Generator[uapi16.Entry]:
        for entry in uapi16.entries(self.path):
            if self.upper is not None and entry.st.st_nlink > 1 and not stat.S_ISDIR(entry.st.st_mode):
                # OverlayFS can report different origin inodes for paths linked after copy-up.
                # The upper inode identifies the hardlink group that commands actually modified.
                try:
                    st = (self.upper / entry.name).lstat()
                except FileNotFoundError:
                    pass
                else:
                    entry = replace(entry, st=st)
            yield entry


def add_blob(source: Path, destination: Path) -> None:
    if not destination.exists():
        clone_file(source, destination)
        destination.chmod(0o444)


def capture(tree: Root, out: Path, parents: Sequence[str | Path] = (), *, epoch: int) -> None:
    """Write the snapshot of `tree` to `out`, with modification times clamped to `epoch`."""
    parent = Snapshot(parents)
    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    found = list(tree.entries())
    tokens = uapi16.hardlink_tokens(found)
    root = uapi16.file_object(uapi16.Entry("", tree.path, tree.path.lstat()), epoch=epoch)
    root.pop("name")
    root["mediaType"] = uapi16.MEDIA_TYPE
    objects = [root]
    inodes: dict[tuple[int, int], uapi16.Object] = {}
    for entry in found:
        if entry.inode in inodes:
            obj = inodes[entry.inode].copy()
            obj["name"] = entry.name
        else:
            obj = uapi16.file_object(entry, epoch=epoch, token=tokens.get(entry.inode))
            if obj["type"] == "reg":
                digest = obj["sha256"]
                obj["contents"] = [{"file": f"blobs/{digest}"}]
                if digest not in parent.blobs:
                    add_blob(entry.path, blobs / digest)
            inodes[entry.inode] = obj
        objects.append(obj)
    for obj in objects:
        # Snapshot ownership is root even when a package manager installs another owner.
        obj["uid"] = obj["gid"] = 0
    uapi16.write_objects(objects, out / uapi16.MANIFEST)


def save(snapshot: Snapshot, out: Path) -> None:
    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for obj in snapshot.objects[1:]:
        if obj["type"] == "reg":
            add_blob(snapshot.content(obj), blobs / obj["sha256"])
    uapi16.write_objects(snapshot.objects, out / uapi16.MANIFEST)


def by_name(objects: Sequence[uapi16.Object]) -> dict[str, uapi16.Object]:
    # The root object has no name. resolve() returns "." for it.
    return {obj.get("name", "."): obj for obj in objects}


def resolve(objects: dict[str, uapi16.Object], name: str, *, follow: bool = True) -> str:
    resolved = PurePosixPath()
    parts = list(PurePosixPath(name).parts)
    hops = 0
    while parts:
        part = parts.pop(0)
        if part == "..":
            resolved = resolved.parent
            continue
        candidate = resolved / part
        obj = objects.get(str(candidate))
        if obj is None or obj["type"] != "lnk" or (not parts and not follow):
            resolved = candidate
            continue
        hops += 1
        if hops > 40:
            fail(f"storage: too many levels of symbolic links in {name!r}")
        target = PurePosixPath(uapi16.symlink_target(obj))
        if target.is_absolute():
            resolved = PurePosixPath()
            target = target.relative_to("/")
        parts = [*target.parts, *parts]
    return str(resolved)


def metadata(path: Path, obj: uapi16.Object) -> None:
    if "uid" in obj or "gid" in obj:
        os.chown(path, obj.get("uid", 0), obj.get("gid", 0), follow_symlinks=False)
    if obj["type"] != "lnk":
        path.chmod(obj["mode"])
    for name, value in obj.get("extendedAttributes", {}).get("user", {}).items():
        os.setxattr(path, "user." + name, uapi16.xattr_value(value), follow_symlinks=False)
    if "mTimeNSec" in obj:
        os.utime(path, ns=(obj["mTimeNSec"], obj["mTimeNSec"]), follow_symlinks=False)


def restore(
    snapshot: Snapshot,
    tree: Path,
    *,
    metacopy: bool = False,
    upper: Path | None = None,
) -> None:
    """Restore private files or prepare metadata and hardlinks for OverlayFS."""
    tree.mkdir(parents=True, exist_ok=True)
    groups: dict[int, Path] = {}
    directories = [(tree, snapshot.objects[0])]
    linked_parents = set()
    if upper is not None:
        upper.mkdir()
        directories.append((upper, snapshot.objects[0]))
        linked_parents = {
            str(parent)
            for obj in snapshot.objects[1:]
            if obj.get("inodeToken")
            for parent in Path(obj["name"]).parents
        }
    for obj in snapshot.objects[1:]:
        kind = obj["type"]
        token = obj.get("inodeToken")
        # index=off breaks lower hardlinks on copy-up, so writable groups start in the upper.
        private = upper is not None and token
        path = (upper if private else tree) / obj["name"]
        if kind == "dir":
            path.mkdir()
            directories.append((path, obj))
            if upper is not None and obj["name"] in linked_parents:
                (upper / obj["name"]).mkdir()
                directories.append((upper / obj["name"], obj))
            continue
        if token and token in groups:
            os.link(groups[token], path, follow_symlinks=False)
            continue
        if kind == "reg":
            if metacopy and not private:
                with path.open("wb") as stream:
                    stream.truncate(obj["size"])
                os.setxattr(path, "user.overlay.metacopy", b"")
                os.setxattr(path, "user.overlay.redirect", ("/" + obj["sha256"]).encode())
            else:
                # Metadata restoration and writes must never change a cached blob's inode.
                clone_file(snapshot.content(obj), path)
        elif kind == "lnk":
            path.symlink_to(uapi16.symlink_target(obj))
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind in ("chr", "blk"):
            mode = stat.S_IFCHR if kind == "chr" else stat.S_IFBLK
            os.mknod(path, mode | obj["mode"], os.makedev(obj["major"], obj["minor"]))
        elif kind == "sock":
            os.mknod(path, stat.S_IFSOCK | obj["mode"])
        else:
            fail(f"storage: unsupported inode type {kind!r}")
        metadata(path, obj)
        if token:
            groups[token] = path
    # Creating children changes directory mtimes, so restore directories last.
    for path, obj in reversed(directories):
        metadata(path, obj)


def _identities(objects: Sequence[uapi16.Object]) -> dict[str, tuple[uapi16.Object, frozenset[str]]]:
    """Map each name to its object without the inodeToken and to the names of its hardlink group."""
    groups: dict[int, set[str]] = {}
    for obj in objects:
        if token := obj.get("inodeToken"):
            groups.setdefault(token, set()).add(obj["name"])
    identities: dict[str, tuple[uapi16.Object, frozenset[str]]] = {}
    for obj in objects:
        rest = obj.copy()
        token = rest.pop("inodeToken", None)
        identities[obj["name"]] = (rest, frozenset(groups[token]) if token else frozenset())
    return identities


def _reflinks(snapshot: Snapshot, scratch: Path) -> bool:
    # clone_file() reflinks through copy_file_range() where the filesystem supports reflinks. On other
    # filesystems it copies every byte, and copying every blob is slower than an overlay mount.
    blob = next(iter(snapshot.blobs.values()), None)
    if blob is None:
        return True
    try:
        reflink(blob, scratch / "reflink")
    except ReflinkUnavailable:
        return False
    return True


def delta(snapshot: Snapshot, base: int) -> list[str]:
    """Keep the objects that differ from the first `base` stores, and their parent directories.

    Return the names that the first `base` stores contain and the snapshot does not.
    """
    inherited = _identities(Snapshot(snapshot.stores[:base]).objects[1:])
    current = _identities(snapshot.objects[1:])
    changed = {name for name, obj in current.items() if inherited.get(name) != obj}
    needed = {str(parent) for name in changed for parent in Path(name).parents}
    snapshot.objects = [snapshot.objects[0]] + [
        obj for obj in snapshot.objects[1:] if obj["name"] in changed or obj["name"] in needed
    ]
    return sorted(inherited.keys() - current.keys())


def write_buck_tree(snapshot: Snapshot, out: Path) -> None:
    """Write the snapshot as a directory that contains only what Buck stores."""
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o755)
    for obj in snapshot.objects[1:]:
        path = out.joinpath(*map(rootfs.escape, obj["name"].split("/")))
        kind = obj["type"]
        if kind == "dir":
            path.mkdir()
            path.chmod(0o755)
        elif kind == "reg":
            clone_file(snapshot.content(obj), path)
            path.chmod(0o755 if obj["mode"] & 0o111 else 0o644)
        elif kind == "lnk":
            path.symlink_to(uapi16.symlink_target(obj))
        else:
            fail(f"storage: Buck cannot store the {kind} inode {obj['name']!r}")


@contextmanager
def root(snapshot: Snapshot, *, readonly: bool = False, strategy: Strategy = "auto") -> Generator[Root]:
    # Overlay refuses a read-only mount with one lower layer and no data layer. An empty snapshot
    # has no blob directories to use as data layers.
    if not snapshot.stores:
        strategy = "copy"
    with tempfile.TemporaryDirectory(prefix="snapshot-root.", dir="/var/tmp") as temporary:
        scratch = Path(temporary)
        tree = scratch / "root"
        if strategy == "auto":
            strategy = "copy" if _reflinks(snapshot, scratch) else "overlay"
        if strategy == "copy":
            restore(snapshot, tree)
            with ExitStack() as stack:
                if readonly:
                    bound = stack.enter_context(Bind(tree, tree, readonly=True))
                    bound.mount()
                yield Root(tree)
            return
        lower = scratch / "lower"
        upper = None if readonly else scratch / "upper"
        work = None if readonly else scratch / "work"
        restore(snapshot, lower, metacopy=True, upper=upper)
        if work is not None:
            work.mkdir()
        datadirs = tuple(store / "blobs" for store in snapshot.stores)
        with Overlay(lower, upper, work, tree, datadirs=datadirs) as mounted:
            mounted.mount()
            yield Root(tree, upper)
