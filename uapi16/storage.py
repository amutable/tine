# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Store complete filesystem snapshots independently of artifact filesystem metadata."""

import hashlib
import os
import stat
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from util import clone_file, fail, reflink

import uapi16
from isolation import Bind, Overlay


class Snapshot:
    def __init__(self, stores: Sequence[str | Path]) -> None:
        self.stores = [Path(store) for store in stores]
        self.objects: list[uapi16.Object] = (
            uapi16.read(self.stores[-1] / uapi16.MANIFEST)
            if self.stores
            else [
                {
                    "mediaType": uapi16.MEDIA_TYPE,
                    "type": "dir",
                    "mode": 0o755,
                    "uid": 0,
                    "gid": 0,
                    "mTimeNSec": 0,
                }
            ]
        )
        self.blobs: dict[str, Path] = {}
        for store in self.stores:
            for blob in (store / "blobs").iterdir():
                self.blobs.setdefault(blob.name, blob)

    def content(self, obj: uapi16.Object) -> Path:
        return self.blobs[obj["sha256"]]


type Identity = tuple[int, int, int, int, int]


def identity(st: os.stat_result) -> Identity:
    # Every change to the contents of a file sets its ctime to the current time, and nothing can set the
    # ctime back. Since Linux 6.13, ext4, XFS, btrfs and tmpfs give the first change after a stat() a ctime
    # that differs from the one stat() returned, even within one timer tick. Other filesystems can keep
    # the same ctime for a change within one tick. A file that is deleted and created again can also get
    # the same inode number. restore() sets the mtime of every file to its mtime in the snapshot, which is
    # in the past. A write sets the mtime to the current time, so the mtime and the size catch a write
    # that the ctime misses.
    return st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size


@dataclass(frozen=True)
class Root:
    path: Path
    upper: Path | None = None
    # capture() takes the digest of a restored file from here when the file has the same identity, so it
    # only reads the files that commands changed.
    digests: Mapping[Identity, str] = field(default_factory=dict)

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
            obj = uapi16.file_object(
                entry,
                epoch=epoch,
                token=tokens.get(entry.inode),
                digest=tree.digests.get(identity(entry.st)),
            )
            if obj["type"] == "chr" and obj["major"] == obj["minor"] == 0:
                # Overlayfs reads a 0/0 character device in a lower layer as a whiteout, so a root
                # restored on a host without reflinks would not contain the device.
                fail(f"storage: {entry.name} is a 0/0 character device, which a snapshot cannot store")
            if obj["type"] == "reg":
                digest = obj["sha256"]
                obj["contents"] = [{"file": f"blobs/{digest}"}]
                if digest not in parent.blobs:
                    add_blob(entry.path, blobs / digest)
            inodes[entry.inode] = obj
        objects.append(obj)
    for obj in objects:
        # "Filesystem layer representation" in docs/design/architecture.md says why capture clears the
        # set-id bits.
        obj["uid"] = obj["gid"] = 0
        if "mode" in obj:
            obj["mode"] &= ~(stat.S_ISUID | stat.S_ISGID)
    uapi16.write_objects(objects, out / uapi16.MANIFEST)


def save(snapshot: Snapshot, out: Path) -> None:
    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for obj in snapshot.objects[1:]:
        if obj["type"] == "reg":
            add_blob(snapshot.content(obj), blobs / obj["sha256"])
    uapi16.write_objects(snapshot.objects, out / uapi16.MANIFEST)


def check(snapshot: Snapshot) -> None:
    """Fail when the blob of a file is missing or its contents do not match the file's digest."""
    for obj in snapshot.objects[1:]:
        if obj["type"] != "reg":
            continue
        blob = snapshot.blobs.get(obj["sha256"])
        if blob is None:
            fail(f"storage: the blob of {obj['name']!r} is missing")
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != obj["sha256"]:
                fail(f"storage: the blob of {obj['name']!r} does not match its digest")


def by_name(objects: Sequence[uapi16.Object]) -> dict[str, uapi16.Object]:
    return {obj["name"]: obj for obj in objects if "name" in obj}


def metadata(path: Path, obj: uapi16.Object) -> None:
    if "uid" in obj or "gid" in obj:
        os.chown(path, obj.get("uid", 0), obj.get("gid", 0), follow_symlinks=False)
    if obj["type"] != "lnk":
        path.chmod(obj["mode"])
    for name, value in obj.get("extendedAttributes", {}).get("user", {}).items():
        os.setxattr(path, "user." + name, uapi16.xattr_value(value), follow_symlinks=False)
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
    # A name below a symlink or outside of the tree would make restore() write outside of the tree.
    created = {"."}
    # A store can list a name twice, first as a symlink to a path outside of the tree and then as a
    # regular file. Restoring the regular file would write through the symlink.
    restored: set[str] = set()
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
        name = uapi16.check_name(obj["name"])
        if str(Path(name).parent) not in created:
            fail(f"storage: the parent of {name!r} is not a directory listed before it")
        if name in restored:
            fail(f"storage: {name!r} is listed more than once")
        restored.add(name)
        # index=off breaks lower hardlinks on copy-up, so writable groups start in the upper.
        private = upper is not None and token
        path = (upper if private else tree) / obj["name"]
        if kind == "dir":
            created.add(name)
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


def _with_digests(root: Root, snapshot: Snapshot) -> Root:
    names = {obj["name"]: obj["sha256"] for obj in snapshot.objects[1:] if obj["type"] == "reg"}
    digests = {identity(entry.st): names[entry.name] for entry in root.entries() if entry.name in names}
    return replace(root, digests=digests)


def _reflinks(snapshot: Snapshot, scratch: Path) -> bool:
    # clone_file() reflinks through copy_file_range() where the filesystem supports reflinks. On other
    # filesystems it copies every byte, and copying every blob is slower than an overlay mount.
    blob = next(iter(snapshot.blobs.values()), None)
    # Without blobs, restore() copies no file data, so a private root costs no more than an overlay. The
    # empty snapshot has no store and therefore no data layer. Overlay refuses a read-only mount with one
    # lower layer and no data layer.
    if blob is None:
        return True
    return reflink(blob, scratch / "reflink")


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


@contextmanager
def root(snapshot: Snapshot, *, readonly: bool = False) -> Generator[Root]:
    with tempfile.TemporaryDirectory(prefix="snapshot-root.", dir="/var/tmp") as temporary:
        scratch = Path(temporary)
        tree = scratch / "root"
        if _reflinks(snapshot, scratch):
            restore(snapshot, tree)
            with ExitStack() as stack:
                if readonly:
                    bound = stack.enter_context(Bind(tree, tree, readonly=True))
                    bound.mount()
                yield Root(tree) if readonly else _with_digests(Root(tree), snapshot)
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
            yield Root(tree) if readonly else _with_digests(Root(tree, upper), snapshot)
