# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read, write and combine UAPI.16 listings, and read and write the snapshot stores built on them."""

import base64
import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from util import clone_file, fail

MEDIA_TYPE = "application/vnd.uapi.16.manifest"
TRAILER_TYPE = "application/vnd.uapi.16.trailer"

# A manifest without `features` records only the default features of the format, which leave out
# mTimeNSec and extended attributes. file_object() writes the fields of every feature listed here.
FEATURES = ["device", "gid", "hardlink", "mode", "mtime", "sha256", "size", "symlink", "uid", "user-xattr"]

# RFC7464 frames every JSON text with a leading record separator and a trailing line feed.
_RS = b"\x1e"
_LF = b"\n"

# A directory holding a manifest cannot also list it, so the format reserves the name at the top
# level; deeper in the tree it is an ordinary file name.
MANIFEST = "Uapi16Manifest"

_INODE_TYPES = (
    (stat.S_ISREG, "reg"),
    (stat.S_ISDIR, "dir"),
    (stat.S_ISLNK, "lnk"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISCHR, "chr"),
    (stat.S_ISBLK, "blk"),
    (stat.S_ISSOCK, "sock"),
)

_NANOSECONDS = 10**9


class Content(TypedDict, total=False):
    file: str
    literal: str


class Xattr(TypedDict, total=False):
    text: str
    base64: str


class Object(TypedDict, total=False):
    """One record of a UAPI.16 manifest."""

    mediaType: str
    features: list[str]
    name: str
    type: str
    mode: int
    uid: int
    gid: int
    mTimeNSec: int
    size: int
    major: int
    minor: int
    sha256: str
    contents: list[Content]
    inodeToken: int
    extendedAttributes: dict[str, dict[str, Xattr]]


@dataclass(frozen=True)
class Entry:
    """One inode below the image root, under the name the manifest gives it."""

    name: str
    path: Path
    st: os.stat_result

    @property
    def inode(self) -> tuple[int, int]:
        return (self.st.st_dev, self.st.st_ino)


def inode_type(mode: int) -> str:
    for predicate, name in _INODE_TYPES:
        if predicate(mode):
            return name
    fail(f"manifest: inode type {stat.S_IFMT(mode):#o} has no manifest representation")


def check_name(name: str) -> str:
    """Reject a path the format cannot encode, rather than listing the image incompletely."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        fail(f"manifest: {name!r} is not valid UTF-8, which a manifest name must be")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        fail(f"manifest: {name!r} contains a control character")
    if any(part in ("", ".", "..") for part in name.split("/")):
        fail(f"manifest: {name!r} must be a normalized relative path")
    top = name.split("/", 1)[0]
    if top == MANIFEST or top.startswith(MANIFEST + "."):
        fail(f"manifest: {name!r} is the reserved manifest file name")
    return name


def scope(roots: Sequence[str]) -> list[str]:
    """The image paths to list, as manifest names: deduplicated, ordered, and non-overlapping.

    A path already covered by another is dropped rather than listed twice, which is what a partition
    copying both a directory and something below it would otherwise produce.
    """
    names = sorted({root.strip("/") for root in roots}, key=os.fsencode)
    if "" in names:  # the whole tree, which covers every other root by definition
        return [""]
    kept: list[str] = []
    for name in names:
        if not any(name == covered or name.startswith(covered + "/") for covered in kept):
            kept.append(name)
    return kept


def entries(tree: Path) -> Iterator[Entry]:
    """Yield every inode below `tree` in manifest order.

    Pre-order, because the format wants a directory's contents to follow it immediately, and sorted
    on the raw name bytes, which is an order neither the locale nor the filesystem can shift.
    """
    yield from _below(tree, "")


def _below(directory: Path, prefix: str) -> Iterator[Entry]:
    with os.scandir(directory) as scan:
        children = sorted(scan, key=lambda child: os.fsencode(child.name))
    for child in children:
        name = check_name(prefix + child.name)
        st = child.stat(follow_symlinks=False)
        yield Entry(name=name, path=Path(child.path), st=st)
        if stat.S_ISDIR(st.st_mode):
            yield from _below(Path(child.path), name + "/")


def hardlink_tokens(found: Sequence[Entry]) -> dict[tuple[int, int], int]:
    """Number the inodes the manifest reaches by more than one name, in the order it reaches them.

    A link count above one does not make a file hardlinked *here*: the other names may sit outside
    the image, and a directory's count is its subdirectories. Only a second name in the manifest
    does, so the tokens count reachable names rather than trusting st_nlink.
    """
    reachable = Counter(
        entry.inode for entry in found if entry.st.st_nlink > 1 and not stat.S_ISDIR(entry.st.st_mode)
    )
    tokens: dict[tuple[int, int], int] = {}
    for entry in found:
        if reachable[entry.inode] > 1 and entry.inode not in tokens:
            tokens[entry.inode] = len(tokens) + 1
    return tokens


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_object(entry: Entry, *, epoch: int, token: int | None = None, digest: str | None = None) -> Object:
    """Describe one inode using UAPI.16 metadata and content fields.

    `digest` is the known SHA-256 of a regular file's contents. Without it, the file is read.
    """
    kind = inode_type(entry.st.st_mode)
    obj: Object = {"name": entry.name, "type": kind}
    target = os.fsencode(os.readlink(entry.path)) if kind == "lnk" else b""

    if kind == "reg":
        obj["size"] = entry.st.st_size
    elif kind == "lnk":
        obj["size"] = len(target)
    elif kind in ("chr", "blk"):
        obj["major"] = os.major(entry.st.st_rdev)
        obj["minor"] = os.minor(entry.st.st_rdev)
    if kind != "lnk":  # the format gives a symlink no mode
        obj["mode"] = stat.S_IMODE(entry.st.st_mode)
    obj["uid"] = entry.st.st_uid
    obj["gid"] = entry.st.st_gid
    obj["mTimeNSec"] = min(entry.st.st_mtime_ns, epoch * _NANOSECONDS)
    if entry.st.st_mtime_ns < 0:
        fail(f"manifest: {entry.name!r} has a timestamp before the UNIX epoch")
    attributes: dict[str, Xattr] = {}
    for name in sorted(os.listxattr(entry.path, follow_symlinks=False)):
        if not name.startswith("user.") or name.startswith("user.overlay."):
            continue
        short = name.removeprefix("user.")
        if not short or any(ord(c) < 32 or ord(c) == 127 for c in short):
            fail(f"manifest: unsupported extended attribute name {name!r}")
        try:
            short.encode("utf-8")
        except UnicodeEncodeError:
            fail(f"manifest: unsupported extended attribute name {name!r}")
        value = os.getxattr(entry.path, name, follow_symlinks=False)
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or "\0" in text:
            attributes[short] = {"base64": base64.b64encode(value).decode("ascii")}
        else:
            attributes[short] = {"text": text}
    if attributes:
        obj["extendedAttributes"] = {"user": attributes}
    if token is not None:
        obj["inodeToken"] = token
    if kind == "lnk":
        # A symlink target has no file of its own to point at, and the format recommends carrying
        # it inline; base64 also spares it the UTF-8 rule a name has to satisfy.
        obj["contents"] = [{"literal": base64.b64encode(target).decode("ascii")}]
    if kind == "reg":
        obj["sha256"] = digest or _digest(entry.path)
    return obj


def _b64decode(value: str) -> bytes:
    # UAPI.16 lets a producer use the URL-safe alphabet. b64decode() drops `-` and `_` without an
    # error and returns the wrong bytes for such a value.
    return base64.b64decode(value.replace("-", "+").replace("_", "/"), validate=True)


def tree_objects(
    tree: Path, found: Sequence[Entry], *, epoch: int, digest: Callable[[Entry], str | None] | None = None
) -> list[Object]:
    """List `tree` and the entries `found` below it, with modification times clamped to `epoch`.

    `digest` returns the known SHA-256 of a regular file. A file without one is read.
    """
    tokens = hardlink_tokens(found)
    root = file_object(Entry("", tree, tree.lstat()), epoch=epoch)
    root.pop("name")
    root["mediaType"] = MEDIA_TYPE
    objects = [root]
    inodes: dict[tuple[int, int], Object] = {}
    for entry in found:
        if entry.inode in inodes:
            obj = inodes[entry.inode].copy()
            obj["name"] = entry.name
        else:
            known = digest(entry) if digest is not None else None
            obj = inodes[entry.inode] = file_object(
                entry, epoch=epoch, token=tokens.get(entry.inode), digest=known
            )
        objects.append(obj)
    return objects


def hardlink_groups(objects: Sequence[Object]) -> dict[int, list[str]]:
    """Map each inodeToken of `objects` to the names that share it, in manifest order."""
    groups: dict[int, list[str]] = {}
    for obj in objects:
        if token := obj.get("inodeToken"):
            groups.setdefault(token, []).append(obj["name"])
    return groups


def symlink_target(obj: Object) -> str:
    return os.fsdecode(_b64decode(obj["contents"][0]["literal"]))


def xattr_value(value: Xattr) -> bytes:
    return value["text"].encode() if "text" in value else _b64decode(value["base64"])


def write_listing(objects: Sequence[Object], out: Path) -> int:
    # The `contents` of a regular file names a blob, and the blob only exists inside a store.
    listed: list[Object] = []
    for obj in objects:
        copy = obj.copy()
        if copy.get("type") != "lnk":
            copy.pop("contents", None)
        listed.append(copy)
    return write_objects(listed, out)


def _record(obj: Object) -> bytes:
    return (
        _RS
        + json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + _LF
    )


_FIELDS = {
    "reg": ("mode", "sha256", "size"),
    "dir": ("mode",),
    "lnk": ("contents",),
    "fifo": ("mode",),
    "sock": ("mode",),
    "chr": ("mode", "major", "minor"),
    "blk": ("mode", "major", "minor"),
}


def _check_fields(source: Path, obj: object) -> None:
    # A store that Buck did not write, such as one that `export --store` reads, can lack any field.
    # storage.restore() reads every field below and fails with a KeyError on a missing one.
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str) or obj.get("type") not in _FIELDS:
        fail(f"manifest: {source} contains an object without a name or a known type: {obj!r}")
    for field in ("mTimeNSec", *_FIELDS[obj["type"]]):
        if field not in obj:
            fail(f"manifest: {source} lists {obj['name']!r} without {field}")


def read(source: Path) -> list[Object]:
    """Read a manifest that `write_objects` produced.

    Only a complete manifest ends with the trailer, so a manifest that a failed write cut short fails
    here instead of becoming a snapshot.
    """
    records = source.read_bytes().split(_RS)[1:]
    if not records or records[-1] != _record({"mediaType": TRAILER_TYPE})[1:]:
        fail(f"manifest: {source} is incomplete: missing trailer")
    objects: list[Object] = [json.loads(record) for record in records[:-1]]
    for obj in objects[1:]:
        _check_fields(source, obj)
    # storage.restore() gives a file no user xattrs when its object has no extendedAttributes. UAPI.16
    # only defines that meaning when the manifest lists `user-xattr`. A reader must ignore the order of
    # the features and every feature it does not know.
    if not objects or not set(FEATURES) <= set(objects[0].get("features", [])):
        fail(f"manifest: {source} does not record the features {', '.join(FEATURES)}")
    return objects


def write_objects(objects: Sequence[Object], out: Path) -> int:
    root = objects[0].copy()
    root["features"] = FEATURES
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as stream:
        for obj in [root, *objects[1:]]:
            stream.write(_record(obj))
        stream.write(_record({"mediaType": TRAILER_TYPE}))
    return len(objects) + 1


def order(name: str) -> list[bytes]:
    """Sort a name before the contents it holds, which sorting the names themselves does not.

    A manifest is in pre-order, and "dir/x" belongs directly after "dir" even though "dir.txt"
    sorts between the two: the components decide, not the bytes of the whole name.
    """
    return [os.fsencode(component) for component in name.split("/")]


def merge(sources: Sequence[Path], out: Path) -> int:
    """Write the listing of a whole from the listings of the parts it is assembled from.

    A disk is filled one partition at a time, so what it carries is what its partitions carry
    between them. A name more than one of them lists is taken from the last, which is the partition
    mounted over a directory the others merely hold.
    """
    objects: dict[str, Object] = {}
    offset = 0
    for source in sources:
        highest = 0
        for obj in read(source)[1:]:
            token = obj.get("inodeToken")
            if isinstance(token, int):
                # A token numbers an inode within one manifest, so each part keeps its own grouping
                # by carrying on where the part before it left off.
                highest = max(highest, token)
                obj["inodeToken"] = token + offset
            objects[str(obj["name"])] = obj
        offset += highest

    ordered = [objects[name] for name in sorted(objects, key=order)]
    renumber(ordered)
    return write_objects([{"mediaType": MEDIA_TYPE}, *ordered], out)


def renumber(objects: Sequence[Object]) -> None:
    tokens: dict[int, int] = {}
    groups = hardlink_groups(objects)
    for obj in objects:
        token = obj.pop("inodeToken", None)
        if token and len(groups[token]) > 1:
            obj["inodeToken"] = tokens.setdefault(token, len(tokens) + 1)


class Snapshot:
    def __init__(self, stores: Sequence[str | Path]) -> None:
        self.stores = [Path(store) for store in stores]
        self.objects: list[Object] = (
            read(self.stores[-1] / MANIFEST)
            if self.stores
            else [
                {
                    "mediaType": MEDIA_TYPE,
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

    def content(self, obj: Object) -> Path:
        return self.blobs[obj["sha256"]]


def add_blob(source: Path, destination: Path) -> None:
    if not destination.exists():
        clone_file(source, destination)
        destination.chmod(0o444)


def save(snapshot: Snapshot, out: Path) -> None:
    blobs = out / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for obj in snapshot.objects[1:]:
        if obj["type"] == "reg":
            add_blob(snapshot.content(obj), blobs / obj["sha256"])
    write_objects(snapshot.objects, out / MANIFEST)


def check_blobs(snapshot: Snapshot) -> None:
    """Fail when the blob of a file is missing or its contents do not match the file's digest."""
    for obj in snapshot.objects[1:]:
        if obj["type"] != "reg":
            continue
        blob = snapshot.blobs.get(obj["sha256"])
        if blob is None:
            fail(f"storage: the blob of {obj['name']!r} is missing")
        if _digest(blob) != obj["sha256"]:
            fail(f"storage: the blob of {obj['name']!r} does not match its digest")


def by_name(objects: Sequence[Object]) -> dict[str, Object]:
    return {obj["name"]: obj for obj in objects if "name" in obj}
