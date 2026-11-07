# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Generic helpers shared by tine's Python entry points."""

import bz2
import errno
import fcntl
import gzip
import http.client
import io
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, NoReturn, TextIO, cast

# Fall back only when the filesystem does not support the range copy or linking.
_COPY_FALLBACK_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV})
# EPERM covers a source another uid owns under fs.protected_hardlinks, or an immutable one.
_LINK_FALLBACK_ERRNOS = frozenset({errno.EMLINK, errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV})

_TRANSIENT_HTTP_STATUS = frozenset((408, 429, 500, 502, 503, 504))
_FETCH_ATTEMPTS = 4


class ReflinkUnavailable(OSError):
    pass


def fail(message: str) -> NoReturn:
    sys.exit(f"tine: {message}")


def object_table(value: object, description: str) -> dict[str, object]:
    """Require a TOML table with string keys."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        fail(f"{description} must be a table")
    return cast(dict[str, object], value)


def cache_home() -> Path:
    """The user's cache root, wherever XDG puts it."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def resolved(argument: Path) -> Path:
    """Resolve a path from configuration to an absolute one, following symlinks.

    A bind mount operates on the resolved path, and so does the lock on a cache directory, so
    whatever records one has to record the same path.
    """
    try:
        return argument.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        fail(f"{argument} names no path: {error}")


def _zstd(stream: IO[bytes]) -> io.BufferedIOBase:
    """Open a zstd stream, saying plainly when this interpreter is too old to.

    A driver that runs inside a box runs under that box's own python, which is whatever its
    distribution ships: `compression.zstd` arrived in 3.14, and Debian trixie is on 3.13. Reaching
    for it lazily keeps a box usable for the formats it does read, and a box asked for one it
    cannot read gets told which of the two is missing rather than an import error at startup.
    """
    try:
        import compression.zstd
    except ImportError:
        fail(
            f"this stream is zstd-compressed and {sys.executable} has no compression.zstd, which "
            "arrived in python 3.14"
        )

    return compression.zstd.ZstdFile(stream, mode="rb")


# What a compressed stream starts with, and what opens it. A file's name is not authoritative
# about how it was compressed, and a repository is free to change compressor between releases,
# so every reader here selects one from the bytes instead.
_COMPRESSORS: tuple[tuple[bytes, Callable[[IO[bytes]], io.BufferedIOBase]], ...] = (
    (b"\x28\xb5\x2f\xfd", _zstd),
    (b"\x1f\x8b", lambda stream: gzip.GzipFile(fileobj=stream, mode="rb")),
    (b"\xfd7zXZ\x00", lambda stream: lzma.LZMAFile(stream, mode="rb")),
    (b"BZh", lambda stream: bz2.BZ2File(stream, mode="rb")),
)

# Enough leading bytes to tell every compressor above apart.
MAGIC = 6


def decompressor(magic: bytes) -> Callable[[IO[bytes]], io.BufferedIOBase] | None:
    """What opens a stream beginning with `magic`, or None where it names no compression.

    What an uncompressed stream is then taken to be is the caller's: each reader here expects a
    different thing underneath, and treating the wrong one as valid is how a corrupt download
    becomes a confusing parse error instead of an honest one.
    """
    for prefix, opener in _COMPRESSORS:
        if magic.startswith(prefix):
            return opener
    return None


# Level 9 is the sweet spot: it beats the default 3 by 10% in a fraction of a second, where 19 buys
# another 10% but takes 28 times as long.
_ZSTD_LEVEL = 9


def compress_zstd(src: Path, out: Path) -> None:
    """Compress `src` into `out`, with the settings a build artifact wants.

    Needs the zstd binary for python < 3.14.
    """
    try:
        import compression.zstd
    except ImportError:
        # fall back to the zstd binary for older Pythons
        # Multi-threaded output is byte-identical to single-threaded output, so every core stays
        # reproducible; --threads=0 is every core to the binary, where libzstd reads 0 as no worker
        # at all. --adapt is not reproducible, so it stays out.
        subprocess.run(
            ["zstd", "-q", "-f", "--threads=0", f"-{_ZSTD_LEVEL}", "-o", str(out), str(src)],
            check=True,
        )
        return

    # IntEnum → plain int mapping for ZstdFile
    options: dict[int, int] = {
        compression.zstd.CompressionParameter.compression_level: _ZSTD_LEVEL,
        compression.zstd.CompressionParameter.nb_workers: os.process_cpu_count() or 1,
    }
    with (
        src.open("rb") as source,
        compression.zstd.ZstdFile(out, mode="wb", options=options) as sink,
    ):
        shutil.copyfileobj(source, sink)


def urlopen(url: str, *, agent: str, timeout: float | None = None) -> http.client.HTTPResponse:
    """Open one URL, naming the tool that asks.

    CDN bot filters (e.g. Cloudflare's) reject Python's default Python-urllib agent. `timeout` bounds
    each blocking socket operation, not the whole transfer.
    """
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    if timeout is None:
        return cast(http.client.HTTPResponse, urllib.request.urlopen(request))
    return cast(http.client.HTTPResponse, urllib.request.urlopen(request, timeout=timeout))


def with_retries[T](what: str, operation: Callable[[], T]) -> T:
    """Run one network operation, retrying transient connection failures and HTTP errors."""
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            return operation()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError) and error.code not in _TRANSIENT_HTTP_STATUS
            )
            if permanent or attempt == _FETCH_ATTEMPTS:
                raise
            print(f"{what}: {error}; retrying…", file=sys.stderr)
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def copy_range(dst_fd: int, dst_off: int, src_fd: int, src_off: int, size: int) -> None:
    """Copy a byte range between two files at explicit offsets.

    copy_file_range shares extents outright where the filesystem can clone them, so a whole-file
    copy on btrfs or reflinked XFS costs nothing; the pread/pwrite step is for the filesystems and
    kernels that refuse the call.
    """
    while size:
        try:
            n = os.copy_file_range(src_fd, dst_fd, size, offset_src=src_off, offset_dst=dst_off)
        except OSError as error:
            if error.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            n = os.pwrite(dst_fd, os.pread(src_fd, min(size, 1 << 20), src_off), dst_off)
        if not n:
            raise OSError(f"short copy: {size} bytes remain")
        src_off += n
        dst_off += n
        size -= n


def clone_file(src: Path, dst: Path, allow_link: bool = False) -> None:
    """Hardlink or copy src to dst while preserving its mode.

    Hardlinking requires an explicit opt-in because later writes affect both paths.
    """
    if allow_link:
        dst.unlink(missing_ok=True)
        try:
            os.link(src, dst)
            return
        except OSError as error:
            if error.errno not in _LINK_FALLBACK_ERRNOS:
                raise
    with open(src, "rb") as source, open(dst, "wb") as destination:
        copy_range(destination.fileno(), 0, source.fileno(), 0, os.fstat(source.fileno()).st_size)
    shutil.copymode(src, dst)


def reflink(src: Path, dst: Path) -> None:
    """Raise ReflinkUnavailable instead of copying bytes when the filesystem cannot clone src."""
    with open(src, "rb") as source, open(dst, "wb") as destination:
        try:
            fcntl.ioctl(destination.fileno(), fcntl.FICLONE, source.fileno())
        except OSError as error:
            if error.errno in _COPY_FALLBACK_ERRNOS | {errno.EACCES, errno.EBADF, errno.ENOTTY, errno.EPERM}:
                raise ReflinkUnavailable(error.errno, error.strerror) from error
            raise
    shutil.copymode(src, dst)


def named_files(source: Path, name: str) -> list[Path]:
    """Find files called `name` under `source`, and return their paths relative to `source`."""
    found: list[Path] = []
    for directory, _, files in source.walk():
        if name in files:
            found.append((directory / name).relative_to(source))
    return sorted(found)


def remove_previous_binaries(out: Path) -> None:
    """Remove the binaries that an earlier run left in `out`."""
    # Buck keeps the output directory of an incremental build between runs. A binary that the build no
    # longer declares would otherwise stay in `out`. A consumer can also hold a hard link to a binary of
    # the last run, and a copy to the same path would write into the consumer's file.
    for previous in out.iterdir():
        previous.unlink()


def remove_path(path: Path, with_parents: bool = False) -> None:
    """Remove a file, symlink, or whole directory tree, if it is there at all.

    shutil.rmtree only handles real directories: it raises on files, on symlinks even when
    they point at a directory, and on a missing path. With `with_parents`, also take the
    directories the removal leaves empty, up to the first one still in use.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()
    if not with_parents:
        return
    for parent in path.parents:
        # `parent.parent` stops the walk at the filesystem root.
        if parent == parent.parent or parent.is_symlink() or not parent.is_dir() or any(parent.iterdir()):
            break
        parent.rmdir()


@contextmanager
def atomic_text_writer(path: Path) -> Generator[TextIO]:
    """Yield a UTF-8 stream and atomically replace its destination on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as stream:
            yield stream
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        # A successful replace already moved it away.
        temporary.unlink(missing_ok=True)


@contextmanager
def text_destination(path: Path) -> Generator[TextIO]:
    """Yield a UTF-8 stream for a path, or for stdout when the caller named `-`.

    A driver published as a run target is asked for its output by a caller that has nowhere to put
    a file: the hermetic sandbox makes only the project writable, and buck2 execs the target, so
    stdout reaches the caller unmediated.
    """
    if str(path) == "-":
        yield cast(TextIO, sys.stdout)
        sys.stdout.flush()
        return
    with atomic_text_writer(path) as stream:
        yield stream


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a path with UTF-8 text."""
    with atomic_text_writer(path) as stream:
        stream.write(content)


def write_if_changed(path: Path, text: str) -> None:
    """Replace a path with UTF-8 text, leaving it alone when that is already what it holds.

    Not `atomic_write_text`: leaving an unchanged file untouched is what keeps Buck's file watcher
    quiet, and a missing parent is an error here rather than something to create, because what this
    writes sits beside a project that already exists.
    """
    # Renaming over a symlink would leave the file it shares stale and turn the link into a copy.
    if path.is_symlink():
        path = path.resolve()
    # `newline=""`, or a file carrying a carriage return never compares equal and is rewritten by
    # every command.
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as handle:
            if handle.read() == text:
                return
    # Rename into place so a concurrent Buck never parses a half-written config.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        fail(f"cannot write {path}: {error}")


def buck2_argv(*args: str) -> list[str]:
    """Build an argv for invoking our pinned Buck2.

    `tine` exports the Buck2 it resolved as `BUCK2_BINARY`. Running a driver outside tine is
    unsupported, so the variable is hard requirement.

    Every invocation is daemon-less. Buck ignores the flag for a command that never starts a daemon,
    so we can add it unconditionally.
    """
    return [os.environ["BUCK2_BINARY"], "--no-buckd", *args]


def buck_output(*args: str) -> str:
    """One nested Buck command, its stdout stripped."""

    # `-v 0` mutes Buck's own chatter on success
    return subprocess.run(
        buck2_argv("-v", "0", *args), check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def package_directory(package: str) -> Path:
    """Where a `cell//path` package label lives on disk, asked of Buck rather than assumed."""
    cell, separator, path = package.partition("//")
    if not separator or not cell or ":" in package or "..." in package:
        fail(f"expected a cell-relative package label, got {package!r}")
    return Path(buck_output("audit", "cell", cell, "--paths-only")) / path


def _record_paths(directory: Path, pathspec: str, how: list[str], message: str | None) -> bool:
    git = ["git", "-C", str(directory)]
    status = subprocess.run(
        [*git, "status", "--porcelain", "--", pathspec], check=True, capture_output=True, encoding="utf-8"
    )
    if not status.stdout:
        return False
    subprocess.run([*git, "add", "--", pathspec], check=True)
    subprocess.run([*git, "commit", *how, "--", pathspec], input=message, encoding="utf-8", check=True)
    return True


def commit_paths(directory: Path, pathspec: str, subject: str) -> bool:
    """Commit `pathspec` under `directory` if it changed, returning whether it did.

    If there are no changes, no commits are made. These are mechanical, so they are not signed off.
    """
    return _record_paths(directory, pathspec, ["--file=-"], f"{subject}\n")


def amend_paths(directory: Path, pathspec: str) -> bool:
    """Fold `pathspec` under `directory` into HEAD if it changed, returning whether it did.

    For a change that is only meaningful as part of the commit that caused it. That commit keeps
    its message and everything else it already holds.
    """
    return _record_paths(directory, pathspec, ["--amend", "--no-edit"], None)
