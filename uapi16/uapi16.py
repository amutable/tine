# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read, write and combine UAPI.16 filesystem listings."""

import base64
import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from util import fail

MEDIA_TYPE = "application/vnd.uapi.16.manifest"
TRAILER_TYPE = "application/vnd.uapi.16.trailer"

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


def entries(tree: Path, roots: Sequence[str] = ()) -> Iterator[Entry]:
    """Yield every inode below `tree` in manifest order, or only those `roots` reach.

    Pre-order, because the format wants a directory's contents to follow it immediately, and sorted
    on the raw name bytes, which is an order neither the locale nor the filesystem can shift.

    A root the image does not have is skipped rather than refused: repart copies what a definition
    names if it is there, so the ESP definition can name both /boot and /efi for images that have
    one or the other.
    """
    for name in scope(roots) if roots else [""]:
        if name == "":
            yield from _below(tree, "")
            continue
        path = tree / name
        if not path.is_symlink() and not path.exists():
            continue
        st = path.lstat()
        yield Entry(name=check_name(name), path=path, st=st)
        if stat.S_ISDIR(st.st_mode):
            yield from _below(path, name + "/")


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


def file_object(entry: Entry, *, epoch: int, token: int | None = None) -> Object:
    """Describe one inode using UAPI.16 metadata and content fields."""
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
        # No `contents`: for a regular file the format then implies the file of that name beside
        # the manifest, which is what this manifest describes.
        obj["sha256"] = _digest(entry.path)
    return obj


def symlink_target(obj: Object) -> str:
    return os.fsdecode(base64.b64decode(obj["contents"][0]["literal"]))


def xattr_value(value: Xattr) -> bytes:
    return value["text"].encode() if "text" in value else base64.b64decode(value["base64"])


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


def records(tree: Path, epoch: int, roots: Sequence[str] = ()) -> Iterator[bytes]:
    """The whole sequence for `tree`: the root object, then one object per inode below it."""
    # The root object carries the media type alone, which is what marks the sequence a manifest.
    # A partition manifest lists a part of the image, and the image root is not part of it. The format
    # defaults a nameless first object to a directory.
    yield _record({"mediaType": MEDIA_TYPE})
    found = list(entries(tree, roots))
    tokens = hardlink_tokens(found)
    for entry in found:
        yield _record(file_object(entry, epoch=epoch, token=tokens.get(entry.inode)))
    yield _record({"mediaType": TRAILER_TYPE})


def write(tree: Path, out: Path, epoch: int, roots: Sequence[str] = ()) -> int:
    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as stream:
        for record in records(tree, epoch, roots):
            stream.write(record)
            written += 1
    return written


def read(source: Path) -> list[Object]:
    """Read a complete manifest, rejecting truncated sequences and trailing data."""
    data = source.read_bytes()
    if not data.startswith(_RS):
        fail(f"manifest: {source} does not open with a record")
    objects: list[Object] = []
    names: set[str] = set()
    trailer = False
    for record in data[1:].split(_RS):
        if trailer:
            fail(f"manifest: {source} has data after its trailer")
        if not record.endswith(_LF):
            fail(f"manifest: {source} holds a record that no line feed ends")
        try:
            obj = cast(Object, json.loads(record))
        except (ValueError, UnicodeDecodeError) as error:
            fail(f"manifest: {source} has invalid JSON: {error}")
        if not isinstance(obj, dict):
            fail(f"manifest: {source} holds a non-object record")
        if not objects:
            if obj.get("mediaType") != MEDIA_TYPE or obj.get("name") is not None:
                fail(f"manifest: {source} does not open with a {MEDIA_TYPE} root object")
        elif obj.get("mediaType") == TRAILER_TYPE:
            text = record.decode("utf-8")
            _, end = json.JSONDecoder().raw_decode(text, len(text) - len(text.lstrip()))
            if obj != {"mediaType": TRAILER_TYPE} or text[end:].lstrip(" \t\r") != "\n":
                fail(f"manifest: {source} has an invalid trailer or data after its trailer")
            trailer = True
            continue
        elif obj.get("mediaType") not in (None, MEDIA_TYPE):
            continue
        else:
            name = obj.get("name")
            if not isinstance(name, str) or name in names:
                fail(f"manifest: {source} has a missing or duplicate name: {name!r}")
            names.add(check_name(name))
        objects.append(obj)
    if not trailer:
        fail(f"manifest: {source} is incomplete: missing trailer")
    return objects


def write_objects(objects: Sequence[Object], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as stream:
        for obj in objects:
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
    counts = Counter(obj.get("inodeToken") for obj in objects if obj.get("inodeToken"))
    for obj in objects:
        token = obj.pop("inodeToken", None)
        if isinstance(token, int) and counts[token] > 1:
            obj["inodeToken"] = tokens.setdefault(token, len(tokens) + 1)
