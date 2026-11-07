# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Store complete filesystem snapshots independently of artifact filesystem metadata."""

import base64
import os
import stat
import tempfile
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

from util import clone_file, fail

import uapi16
from isolation import Overlay


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
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            fail(f"storage: invalid SHA-256 digest {digest!r}")
        if digest not in self.blobs:
            fail(f"storage: missing blob {digest} for {obj['name']!r}")
        path = self.blobs[digest]
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != obj["size"]:
            fail(f"storage: invalid blob {digest} for {obj['name']!r}")
        return path


def capture(tree: Path, out: Path, parents: Sequence[str | Path] = (), *, epoch: int) -> None:
    """Write the snapshot of `tree` to `out`, with modification times clamped to `epoch`."""
    parent = Snapshot(parents)
    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    found = list(uapi16.entries(tree))
    tokens = uapi16.hardlink_tokens(found)
    root = uapi16.file_object(uapi16.Entry("", tree, tree.lstat()), epoch=epoch)
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
                destination = blobs / digest
                if digest not in parent.blobs and not destination.exists():
                    clone_file(entry.path, destination)
                    destination.chmod(0o444)
            inodes[entry.inode] = obj
        objects.append(obj)
    for obj in objects:
        # Snapshot ownership is root even when a package manager installs another owner.
        obj["uid"] = obj["gid"] = 0
    uapi16.write_objects(objects, out / uapi16.MANIFEST)


def metadata(path: Path, obj: uapi16.Object) -> None:
    if "uid" in obj or "gid" in obj:
        os.chown(path, obj.get("uid", 0), obj.get("gid", 0), follow_symlinks=False)
    if obj["type"] != "lnk":
        path.chmod(obj["mode"])
    for name, value in obj.get("extendedAttributes", {}).get("user", {}).items():
        data = value["text"].encode() if "text" in value else base64.b64decode(value["base64"])
        os.setxattr(path, "user." + name, data, follow_symlinks=False)
    if "mTimeNSec" in obj:
        os.utime(path, ns=(obj["mTimeNSec"], obj["mTimeNSec"]), follow_symlinks=False)


def restore(snapshot: Snapshot, tree: Path, *, hardlinks: bool | None = None) -> None:
    """Restore private inodes, optionally selecting linked or independent files."""
    tree.mkdir(parents=True, exist_ok=True)
    groups: dict[int, Path] = {}
    directories = [(tree, snapshot.objects[0])]
    for obj in snapshot.objects[1:]:
        path = tree / obj["name"]
        kind = obj["type"]
        token = obj.get("inodeToken")
        if kind == "dir":
            path.mkdir()
            directories.append((path, obj))
            continue
        if hardlinks is not None and bool(token) != hardlinks:
            continue
        if token and token in groups:
            os.link(groups[token], path, follow_symlinks=False)
            continue
        if kind == "reg":
            # Metadata restoration and writes must never change a cached blob's inode.
            clone_file(snapshot.content(obj), path)
        elif kind == "lnk":
            path.symlink_to(os.fsdecode(base64.b64decode(obj["contents"][0]["literal"])))
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


@contextmanager
def root(stores: Sequence[str | Path], *, base: int = 0) -> Generator[Path]:
    snapshot = Snapshot(stores)
    if base:
        inherited = _identities(Snapshot(stores[:base]).objects[1:])
        changed = {
            name for name, obj in _identities(snapshot.objects[1:]).items() if inherited.get(name) != obj
        }
        needed = {str(parent) for name in changed for parent in Path(name).parents}
        snapshot.objects = [snapshot.objects[0]] + [
            obj for obj in snapshot.objects[1:] if obj["name"] in changed or obj["name"] in needed
        ]
    with ExitStack() as stack:
        scratch = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="snapshot-root.", dir="/var/tmp"))
        )
        lower, upper, work, merged = (scratch / name for name in ("lower", "upper", "work", "root"))
        restore(snapshot, lower, hardlinks=False)
        # index=off splits lower hardlinks on copy-up. Upper hardlinks keep their inode on writes.
        restore(snapshot, upper, hardlinks=True)
        work.mkdir()
        stack.enter_context(Overlay((lower,), upper, work, merged)).mount()
        yield merged
