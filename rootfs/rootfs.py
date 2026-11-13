# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Mount target roots and capture output trees in a form Buck can store.

Buck cannot store backslashes in paths, so `capture()` escapes names that contain them.

`readonly_project()` makes the project read-only while a build driver runs. `source_overlay()` gives a
tool that writes into its sources, such as rpmbuild, a view of them that discards the writes.
"""

import errno
import os
import stat
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
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

_ESC = ".esc."  # `.esc.<escaped>`: a name buck can't store, percent-escaped


def escape(name: str) -> str:
    """Escape a path component Buck cannot store, preserving injectivity."""
    if "\\" not in name and not name.startswith(_ESC):
        return name
    return _ESC + name.replace("%", "%25").replace("\\", "%5C")


def capture(tree: Path) -> None:
    """Make every directory of `tree` writable for its owner and escape the names Buck cannot store."""
    # Buck must be able to delete and rename through package-supplied read-only directories.
    for p in tree.rglob("*"):
        st = p.lstat()
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
            p.chmod(stat.S_IMODE(st.st_mode) | 0o700)
    for p in sorted(tree.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        escaped = escape(p.name)
        if escaped != p.name:
            p.rename(p.parent / escaped)


@contextmanager
def capture_on_exit(tree: str | Path) -> Generator[Path]:
    """Capture a tree after use without hiding an error from the body."""
    tree = Path(tree)
    try:
        yield tree
    except BaseException as error:
        try:
            capture(tree)
        except BaseException as capture_error:
            error.add_note(
                f"rootfs capture of {tree} also failed: {capture_error.__class__.__name__}: {capture_error}"
            )
        raise
    else:
        capture(tree)


# Root setup.


@contextmanager
def source_overlay(source: Path, target: Path) -> Generator[Path]:
    """Mount `source` at `target` as an overlay, and discard every write when the context exits."""
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
                stack.enter_context(Overlay(lower, upper, work, merged)).mount()
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
    bind: str | Path,
    apivfs: bool = False,
    binds: Sequence[tuple[str | Path, str | Path]] | None = None,
    chroot: bool = False,
    strict_unmount: bool = False,
) -> Generator[Path]:
    """Bind `bind` at `target` with optional API filesystems, extra binds and a chroot."""
    target = Path(target)
    with ExitStack() as stack:
        mounted = stack.enter_context(Bind(Path(bind), target, strict_unmount=strict_unmount))
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
