# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Mount target roots and translate stored OCI markers to native overlayfs state.

Buck cannot store device nodes, xattrs, or backslashes in paths, so deltas encode them as
regular marker files. Sidecar layers reconstruct markers on entry; persisted uppers are
captured back to the storage form after unmounting.

`readonly_project()` makes the project read-only while a build driver runs. `source_overlay()` gives a
tool that writes into its sources, such as rpmbuild, a view of them that discards the writes.
"""

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from enum import Enum, auto
from pathlib import Path

from isolation import (
    AT_FDCWD,
    Bind,
    Close,
    Devices,
    Overlay,
    Tmpfs,
    open_directory,
    submounts,
)
from isolation import (
    chroot as _chroot,
)

_WH = ".wh."  # `.wh.<name>`: a whiteout hiding <name> from lower layers
_OPAQUE = ".wh..wh..opq"  # marks its parent dir opaque (hide everything below it)
_ESC = ".esc."  # `.esc.<escaped>`: a name buck can't store, percent-escaped
_OPAQUE_XATTR = b"user.overlay.opaque"  # an opaque dir's native form (userxattr namespace)


# OCI delta to native overlay translation.


def _escape(name: str) -> str:
    """Escape a path component Buck cannot store, preserving injectivity."""
    if "\\" not in name and not name.startswith(_ESC):
        return name
    return _ESC + name.replace("%", "%25").replace("\\", "%5C")


def _unescape(name: str) -> str:
    return name.removeprefix(_ESC).replace("%5C", "\\").replace("%25", "%")


class _Kind(Enum):
    """What a stored delta entry means once decoded."""

    NORMAL = auto()  # a plain entry, carried through under its true name
    WHITEOUT = auto()  # deletes the payload name from lower layers
    OPAQUE = auto()  # marks its parent dir opaque


def _classify(name: str) -> tuple[_Kind, str]:
    """Decode a stored name into its marker kind and payload."""
    true = _unescape(name) if name.startswith(_ESC) else name
    if true == _OPAQUE:
        return _Kind.OPAQUE, ""
    if true.startswith(_WH):
        return _Kind.WHITEOUT, true[len(_WH) :]
    return _Kind.NORMAL, true


def deletions(delta: Path) -> list[Path]:
    """The marker files of a stored delta that delete from the layers below it."""
    markers = (path for path in delta.rglob("*") if _classify(path.name)[0] is not _Kind.NORMAL)
    return sorted(path.relative_to(delta) for path in markers)


def _whiteout_marker(name: str) -> str:
    """Encode a possibly unstorable whiteout target."""
    return _escape(_WH + name)


def _whiteout(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.mknod(str(directory / name), stat.S_IFCHR | 0o600, os.makedev(0, 0))


def _thaw(src: Path, dst: Path) -> None:
    """Copy an escaped entry under its true name, recursively decoding markers."""
    if src.is_symlink() or not src.is_dir():
        shutil.copy2(src, dst, follow_symlinks=False)
        return
    dst.mkdir(mode=stat.S_IMODE(src.lstat().st_mode))
    for child in src.iterdir():
        kind, payload = _classify(child.name)
        if kind is _Kind.OPAQUE:
            os.setxattr(str(dst), _OPAQUE_XATTR, b"y")
        elif kind is _Kind.WHITEOUT:
            _whiteout(dst, payload)
        else:
            _thaw(child, dst / payload)
    shutil.copystat(src, dst, follow_symlinks=False)


def _markers(delta: Path, dest: Path) -> tuple[Path | None, Path | None]:
    """Express stored markers as sparse sidecar layers above and below `delta`."""
    above, below = dest / "above", dest / "below"
    used_above = used_below = False
    for p in delta.rglob("*"):
        rel = p.parent.relative_to(delta)
        if any(part.startswith(_ESC) for part in rel.parts):
            continue  # inside an escaped dir: thawed wholesale when its ancestor was hit
        kind, payload = _classify(p.name)
        if kind is _Kind.OPAQUE:
            opaque = below / rel
            opaque.mkdir(parents=True, exist_ok=True)
            os.setxattr(str(opaque), _OPAQUE_XATTR, b"y")
            _whiteout(above / rel, p.name)  # hide the marker file itself
            used_above = used_below = True
        elif kind is _Kind.WHITEOUT:
            _whiteout(above / rel, payload)  # whiteout the deleted target
            _whiteout(above / rel, p.name)  # hide the literal marker file (`.wh.` or `.esc.`)
            used_above = True
        elif p.name.startswith(_ESC):  # an escaped normal entry
            (above / rel).mkdir(parents=True, exist_ok=True)
            _thaw(p, above / rel / payload)  # materialize it under its true name
            _whiteout(above / rel, p.name)  # hide the `.esc.` entry itself
            used_above = True
    return (above if used_above else None, below if used_below else None)


def _reconstruct(lowers: list[Path], scratch: Path) -> list[Path]:
    """Frame stored deltas as an overlayfs lowerdir list in top-to-bottom order."""
    components: list[Path] = []
    for i, lo in enumerate(reversed(lowers)):
        above, below = _markers(lo, scratch / f"m{i}")
        if above is not None:
            components.append(above)
        components.append(lo)
        if below is not None:
            components.append(below)
    return components


def _is_opaque(d: Path) -> bool:
    try:
        return os.getxattr(str(d), _OPAQUE_XATTR) == b"y"
    except OSError:
        return False


def _in_lower(lower: Path, rel: Path) -> bool:
    """Return whether the stored layer `lower` has an entry at `rel`, without following its symlinks."""
    path = lower
    for part in rel.parts:
        if path.is_symlink() or not path.is_dir():
            return False
        path = path / _escape(part)
        if not os.path.lexists(path):
            return False
    return True


def capture(tree: Path, lowers: Sequence[Path] | None = None) -> None:
    """Rewrite native overlay state and unstorable names into regular marker files.

    `lowers` are the stored layers below `tree`, if `tree` is an overlay upper directory.
    """
    # Buck must be able to delete and rename through package-supplied read-only directories.
    for p in tree.rglob("*"):
        st = p.lstat()
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
            p.chmod(stat.S_IMODE(st.st_mode) | 0o700)
    for p in sorted(tree.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        st = p.lstat()
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
            p.unlink()
            (p.parent / _whiteout_marker(p.name)).write_bytes(b"")
            continue
        if stat.S_ISDIR(st.st_mode) and _is_opaque(p):
            os.removexattr(str(p), _OPAQUE_XATTR)
            # overlayfs marks a directory of the upper layer opaque when the directory is renamed
            # into a directory that a lower layer also contains. dpkg unpacks every new directory
            # under a temporary name and renames it into place, so a Debian layer gets an opaque mark
            # on every new directory.
            # The mark hides nothing when no lower layer has an entry at the path, so capture() writes
            # no marker for it.
            rel = p.relative_to(tree)
            if lowers is None or any(_in_lower(lower, rel) for lower in lowers):
                (p / _OPAQUE).write_bytes(b"")
        escaped = _escape(p.name)
        if escaped != p.name:
            p.rename(p.parent / escaped)


@contextmanager
def capture_on_exit(tree: str | Path, lowers: Sequence[Path] | None = None) -> Generator[Path]:
    """Capture a tree after use without hiding an error from the body."""
    tree = Path(tree)
    try:
        yield tree
    except BaseException as error:
        try:
            capture(tree, lowers)
        except BaseException as capture_error:
            error.add_note(
                f"rootfs capture of {tree} also failed: {capture_error.__class__.__name__}: {capture_error}"
            )
        raise
    else:
        capture(tree, lowers)


# Root setup.


@contextmanager
def source_overlay(source: Path, target: Path) -> Generator[Path]:
    """Mount `source` at `target` as an overlay, and discard every write when the context exits.

    `rootfs()` decodes the `.wh.` and `.esc.` marker files of a stored image layer. This function
    does not, because a checkout is not an image layer and can contain such names.
    """
    # An overlay shows the directory that a mount in its lower directory covers, not the mounted tree.
    # A `tine mount` covers a directory of the project, so directory submounts need overlays of their own.
    # statmount() returns mount points with their symlinks resolved, so `source` needs them resolved too.
    resolved = source.resolve()
    layers = [(source, target)]
    # overlayfs only takes directories as layers, so `target` shows the file that a file mount covers.
    points = [Path(point) for point in submounts(str(resolved)) if Path(point).is_dir()]
    layers += [(point, target / point.relative_to(resolved)) for point in points]
    with ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="source.")))
        for index, (lower, merged) in enumerate(layers):
            upper, work = scratch / str(index) / "upper", scratch / str(index) / "work"
            upper.mkdir(parents=True)
            work.mkdir()
            # Make sure the root directory has the mode and times of the lower directory rather than
            # the tmpdir mode and times.
            attributes = lower.stat()
            upper.chmod(stat.S_IMODE(attributes.st_mode))
            os.utime(upper, ns=(attributes.st_atime_ns, attributes.st_mtime_ns))
            try:
                stack.enter_context(Overlay((lower,), upper, work, merged)).mount()
            except OSError as error:
                if error.errno == errno.EINVAL:
                    error.add_note(
                        "Locked mounts below the source can cause EINVAL. A caller must be allowed to clone "
                        "the project's mount tree before the sandbox creates its user namespace to avoid "
                        "locking inherited mounts. `tine buck` prepares that namespace when the project "
                        "has mounts declared with `tine mount`."
                    )
                raise
        yield target


@contextmanager
def readonly_project(project: Path, outputs: Mapping[Path, Path]) -> Generator[None]:
    """Make `project` read-only and change into it, but keep each output directory writable.

    `outputs` maps an output directory under `project` to a mount point outside `project`.
    """
    project = project.absolute()
    if len(set(outputs.values())) != len(outputs):
        raise ValueError(f"readonly_project: outputs share a mount point: {sorted(outputs)}")
    with ExitStack() as stack:
        # The file descriptor refers to the working directory on the writable mount, also while the
        # read-only bind of `project` covers it.
        cwd = stack.enter_context(Close(open_directory(AT_FDCWD, ".", create=False)))
        stack.callback(os.fchdir, cwd)
        root = stack.enter_context(Close(open_directory(AT_FDCWD, str(project), create=False)))
        # Mount the outputs before `project` becomes read-only. A bind of a directory on a read-only
        # mount is read-only too.
        for output, mounted in outputs.items():
            relative = output.relative_to(project) if output.is_absolute() else output
            # A symlink in any component of `output` can point out of `project`. Creating the output
            # through it would make a directory outside `project` writable for the build. open_directory()
            # fails with EXDEV on a path that leaves `project`.
            try:
                with Close(open_directory(root, str(relative.parent), beneath=True)) as parent:
                    # A stale symlink would bind an old content-based output instead of the current output.
                    # A kept action never removes the symlink; `kept_dir()` in project/defs.bzl explains why.
                    with suppress(FileNotFoundError):
                        if stat.S_ISLNK(os.lstat(relative.name, dir_fd=parent).st_mode):
                            os.unlink(relative.name, dir_fd=parent)
                    source = open_directory(parent, relative.name, beneath=True)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                raise ValueError(f"readonly_project: output leaves the project: {output}") from error
            with Close(source):
                stack.enter_context(Bind(".", mounted, root=source)).mount()
        # The read-only bind covers `project`, but the working directory of this process still
        # refers to the writable mount underneath. After fchdir() into the bind, relative paths of
        # this process and of its children resolve through the read-only bind.
        bind = stack.enter_context(Bind(project, project, readonly=True))
        bind.mount()
        os.fchdir(bind.fd)
        yield


def _apivfs(stack: ExitStack, root: int) -> None:
    """Mount the API and temporary filesystems expected by package scripts in the directory `root`."""
    tty = Path(os.ttyname(2)) if os.isatty(2) else None
    stack.enter_context(Devices("/dev", tty)).mount(root)
    stack.enter_context(Bind(Path("/proc"), "/proc")).mount(root)
    stack.enter_context(Tmpfs("/run")).mount(root)

    # The root splits temporary space as box/sandbox.py does. /tmp is a tmpfs for small files that
    # live briefly. /var/tmp is on disk, because package scripts stage gigabytes there. `buck run`
    # on a box and `buck test` have no scratch directory, so /var/tmp is a tmpfs there as well.
    stack.enter_context(Tmpfs("/tmp", mode="1777")).mount(root)

    if "BUCK_SCRATCH_PATH" not in os.environ:
        stack.enter_context(Tmpfs("/var/tmp", mode="1777")).mount(root)
    else:
        # In a run action, the sandbox mounts the action's scratch directory at /var/tmp, and TMPDIR
        # points there. Buck creates the scratch directory in buck-out, inside the project. /var/tmp
        # is a separate mount, so it stays writable when `readonly_project()` makes the project
        # read-only. The backing directory gets a unique name, because one action can mount several
        # roots.
        backing = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="var-tmp.", ignore_cleanup_errors=True))
        )
        backing.chmod(0o1777)  # what a tmpfs mounted on /var/tmp defaults to
        stack.enter_context(Bind(backing, "/var/tmp")).mount(root)


@contextmanager
def chroot(target: str | Path) -> Generator[Path]:
    """Temporarily enter an already-mounted target root."""
    with _chroot(Path(target)):
        yield Path("/")


@contextmanager
def rootfs(
    target: str | Path,
    *,
    bind: str | Path | None = None,
    capture_bind: bool = False,
    lowers: Sequence[str | Path] | None = None,
    upperdir: str | Path | None = None,
    workdir: str | Path | None = None,
    apivfs: bool = False,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
    chroot: bool = False,
) -> Generator[Path]:
    """Mount a bind or overlay root, optionally chrooting and persisting its output.

    Persistent overlay uppers and bind sources with `capture_bind=True` are captured after all
    mounts have been torn down.
    """
    if capture_bind and bind is None:
        raise ValueError("rootfs(capture_bind=True) needs bind=")
    target = Path(target)
    with ExitStack() as stack:
        if lowers is not None:
            # _in_lower() treats a symlink as a missing directory. Without resolve(), capture() would find
            # no entry in a layer that the caller passes as a symlink.
            resolved = [Path(lo).resolve() for lo in lowers]
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rootfs.")))
            components = _reconstruct(resolved, scratch)
            if not components:
                empty = scratch / "empty"
                empty.mkdir()
                components.append(empty)
            if upperdir is not None:
                if workdir is None:
                    raise ValueError("rootfs(upperdir=...) needs a matching workdir=")
                upper, work = Path(upperdir), Path(workdir)
            else:
                # A writable ephemeral upper also permits a single-component stack.
                upper, work = scratch / "upper", scratch / "work"
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            if upperdir is not None:
                # Enter before registering the unmount so capture runs after it.
                stack.enter_context(capture_on_exit(upper, resolved))
            # capture() reads and rewrites the upperdir after the unmount, so a captured upper needs a
            # strict unmount. Nobody reads an ephemeral upper, so it is unmounted lazily like every
            # other mount.
            mounted: Overlay | Bind = stack.enter_context(
                Overlay(tuple(components), upper, work, target, strict_unmount=upperdir is not None)
            )
        elif bind is not None:
            if capture_bind:
                # Enter before registering the unmount so capture runs after it.
                stack.enter_context(capture_on_exit(bind))
            # capture() reads the bind source after the unmount, like the upper of an overlay.
            mounted = stack.enter_context(Bind(Path(bind), target, strict_unmount=capture_bind))
        else:
            raise ValueError("rootfs needs bind= or lowers=")
        mounted.mount()
        # An image layer can contain an absolute symlink at a mount point. The mount points resolve inside
        # the root, so that the symlink doesn't place a mount outside of the root.
        if apivfs:
            _apivfs(stack, mounted.fd)
        for src, dst in binds or []:
            stack.enter_context(Bind(Path(src), dst)).mount(mounted.fd)
        if chroot:
            # Enter last so callbacks resolve paths after the chroot unwinds.
            stack.enter_context(_chroot(target))
        yield Path("/") if chroot else target
