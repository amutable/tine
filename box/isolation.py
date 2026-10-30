# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Enter Linux namespaces and assemble filesystems for tine sandboxes.

Every sandbox imports this module. Keep imports, including transitive imports, to the absolute
minimum to minimize startup time.
"""

import ctypes
import errno
import os
import stat
import sys

TYPE_CHECKING = False

# The host-side launcher also imports this module on Python 3.12/3.13, which evaluate annotations eagerly.
if TYPE_CHECKING or sys.version_info < (3, 14):
    from typing import Self, override
else:

    def override[T](method: T) -> T:
        return method


type StrPath = str | os.PathLike[str]

AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100
AT_NO_AUTOMOUNT = 0x800
AT_RECURSIVE = 0x8000
AT_SYMLINK_NOFOLLOW = 0x100
CAP_CHOWN = 0
CAP_DAC_OVERRIDE = 1
CAP_DAC_READ_SEARCH = 2
CAP_FOWNER = 3
CAP_FSETID = 4
CAP_SETGID = 6
CAP_SETUID = 7
CAP_SETPCAP = 8
CAP_NET_BIND_SERVICE = 10
CAP_NET_ADMIN = 12
CAP_SYS_CHROOT = 18
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21
CAP_SYS_RESOURCE = 24
CAP_SETFCAP = 31
CLONE_NEWNET = 0x40000000
CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
IFF_UP = 0x1
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
LINUX_CAPABILITY_U32S_3 = 2
LINUX_CAPABILITY_VERSION_3 = 0x20080522
MNT_DETACH = 2
UMOUNT_NOFOLLOW = 8
MOUNT_ATTR_RDONLY = 0x00000001
MOUNT_ATTR_NOSUID = 0x00000002
MOUNT_ATTR_NODEV = 0x00000004
MOUNT_ATTR_SIZE_VER0 = 32
MNT_ID_REQ_SIZE_VER0 = 24
FSCONFIG_SET_FLAG = 0
FSCONFIG_SET_STRING = 1
FSCONFIG_SET_FD = 5
FSCONFIG_CMD_CREATE = 6
FSMOUNT_CLOEXEC = 1
FSOPEN_CLOEXEC = 1
MOVE_MOUNT_F_EMPTY_PATH = 0x00000004
MOVE_MOUNT_T_EMPTY_PATH = 0x00000040
MS_SLAVE = 1 << 19
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
OPEN_TREE_CLONE = 1
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_IS_SET = 1
PR_CAP_AMBIENT_RAISE = 2
PR_CAP_AMBIENT_LOWER = 3
PR_CAPBSET_DROP = 24
PR_SET_DUMPABLE = 4
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000
SCMP_ARCH_X86 = 0x40000003
SCMP_ARCH_X86_64 = 0xC000003E
SCMP_ARCH_X32 = 0x4000003E
SCMP_ARCH_ARM = 0x40000028
SCMP_ARCH_AARCH64 = 0xC00000B7
SCMP_ARCH_PPC = 0x14
SCMP_ARCH_PPC64 = 0x80000015
SCMP_ARCH_PPC64LE = 0xC0000015
SCMP_ARCH_S390 = 0x16
SCMP_ARCH_S390X = 0x80000016
SCMP_FLTATR_ACT_BADARCH = 2
SCMP_FLTATR_API_SYSRAWRC = 9

RESOLVE_BENEATH = 0x08
RESOLVE_IN_ROOT = 0x10
STATMOUNT_MNT_POINT = 0x00000010
STATX_MNT_ID_UNIQUE = 0x00004000

# glibc 2.17 has no statx() wrapper. aarch64 and riscv64 use the generic syscall table.
_NR_STATX = 332 if os.uname().machine == "x86_64" else 291

# Linux uses the same numbers for the system calls below on every architecture that tine supports.
_NR_MOVE_MOUNT = 429
_NR_FSOPEN = 430
_NR_FSCONFIG = 431
_NR_FSMOUNT = 432
_NR_OPENAT2 = 437
_NR_MOUNT_SETATTR = 442
_NR_STATMOUNT = 457
_NR_LISTMOUNT = 458
_NR_OPEN_TREE_ATTR = 467

# _STATX_MNT_ID is the offset of stx_mnt_id in struct statx. _STATMOUNT_MNT_POINT and _STATMOUNT_STRINGS are
# the offsets of mnt_point and of the strings in struct statmount.
_STATX_MNT_ID = 144
_STATMOUNT_MNT_POINT = 108
_STATMOUNT_STRINGS = 512
_ROOT = "/"
_TMPFS_DEFAULT_MODE = "0755"
_COMPAT_ARCHITECTURES = {
    SCMP_ARCH_X86_64: (SCMP_ARCH_X86, SCMP_ARCH_X32),
    SCMP_ARCH_AARCH64: (SCMP_ARCH_ARM,),
    SCMP_ARCH_PPC64: (SCMP_ARCH_PPC,),
    SCMP_ARCH_PPC64LE: (SCMP_ARCH_PPC,),
    SCMP_ARCH_S390X: (SCMP_ARCH_S390,),
}
_KEPT_CAPABILITIES = (
    CAP_CHOWN,
    CAP_DAC_OVERRIDE,
    CAP_DAC_READ_SEARCH,
    CAP_FOWNER,
    CAP_FSETID,
    CAP_SETGID,
    CAP_SETUID,
    CAP_SETPCAP,
    CAP_SYS_CHROOT,
    CAP_SYS_PTRACE,
    CAP_SYS_ADMIN,
    CAP_SYS_RESOURCE,
    CAP_SETFCAP,
)
_CHOWN_SYSCALLS = (
    b"chown",
    b"chown32",
    b"fchown",
    b"fchown32",
    b"fchownat",
    b"lchown",
    b"lchown32",
)
_SYNC_SYSCALLS = (
    b"fdatasync",
    b"fsync",
    b"msync",
    b"sync",
    b"sync_file_range",
    b"sync_file_range2",
    b"syncfs",
)


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class _MountIdRequest(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("spare", ctypes.c_uint32),
        ("mnt_id", ctypes.c_uint64),
        ("param", ctypes.c_uint64),
    ]


class _MountAttributes(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.capget.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_LIBC.capset.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_LIBC.pivot_root.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
_LIBC.prctl.argtypes = (
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
)
_LIBC.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)
_LIBC.unshare.argtypes = (ctypes.c_int,)
_LIBC.syscall.restype = ctypes.c_long


class SandboxOSError(OSError):
    def __init__(self, number: int, message: str) -> None:
        super().__init__(number, os.strerror(number))
        self.message = message


def _error(call: str, path: StrPath | None = None, number: int = 0) -> None:
    number = number or ctypes.get_errno()

    raise OSError(number, f"{call}: {os.strerror(number)}", path)


def umount2(path: StrPath, flags: int = 0) -> None:
    if _LIBC.umount2(os.fsencode(path), flags) < 0:
        _error("umount2", path)


def unshare(flags: int) -> None:
    if _LIBC.unshare(flags) < 0:
        _error("unshare")


def _prctl(option: int, argument: int) -> int:
    result = int(_LIBC.prctl(option, argument, 0, 0, 0))

    if result < 0:
        _error("prctl")

    return result


def _has_capability(capability: int) -> bool:
    header = _CapabilityHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * LINUX_CAPABILITY_U32S_3)()

    if _LIBC.capget(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capget")

    effective = (data[1].effective << 32) | data[0].effective

    return bool(effective & (1 << capability))


def _capability_mask(capabilities: tuple[int, ...]) -> int:
    return sum(1 << capability for capability in capabilities)


def fix_user_namespace_capabilities(*, network: bool) -> None:
    """Keep the sandbox's mount capabilities across exec."""

    capabilities = _KEPT_CAPABILITIES
    if network:
        capabilities += (CAP_NET_BIND_SERVICE, CAP_NET_ADMIN)

    header = _CapabilityHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * LINUX_CAPABILITY_U32S_3)()
    if _LIBC.capget(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capget")

    permitted = ((data[1].permitted << 32) | data[0].permitted) & _capability_mask(capabilities)
    with open("/proc/sys/kernel/cap_last_cap", encoding="ascii") as handle:
        last = int(handle.read())

    for capability in range(last + 1):
        if not permitted & (1 << capability):
            _prctl(PR_CAPBSET_DROP, capability)

    for index in range(LINUX_CAPABILITY_U32S_3):
        word = (permitted >> (index * 32)) & 0xFFFFFFFF
        data[index].permitted = word
        data[index].effective = word
        data[index].inheritable = word

    if _LIBC.capset(ctypes.addressof(header), ctypes.addressof(data)) < 0:
        _error("capset")

    for capability in range(last + 1):
        if permitted & (1 << capability):
            _ambient_capability_raise(capability)
        elif _ambient_capability_is_set(capability):
            _ambient_capability_lower(capability)


def _ambient_capability_is_set(capability: int) -> bool:
    result = _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_IS_SET, capability, 0, 0)

    if result < 0:
        _error("prctl")

    return bool(result)


def _ambient_capability_raise(capability: int) -> None:
    if _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, capability, 0, 0) < 0:
        _error("prctl")


def _ambient_capability_lower(capability: int) -> None:
    if _LIBC.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_LOWER, capability, 0, 0) < 0:
        _error("prctl")


def unprivileged_user_namespace(*, become_root: bool) -> None:
    """Map only the caller into a fresh user namespace."""

    _prctl(PR_SET_DUMPABLE, 1)

    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    parent = os.getpid()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)

        try:
            os.read(read_fd, 1)
            with open(f"/proc/{parent}/setgroups", "w", encoding="ascii") as handle:
                handle.write("deny\n")

            gid = os.getgid()
            uid = os.getuid()
            mapped_gid = 0 if become_root else gid
            mapped_uid = 0 if become_root else uid

            with open(f"/proc/{parent}/gid_map", "w", encoding="ascii") as handle:
                handle.write(f"{mapped_gid} {gid} 1\n")
            with open(f"/proc/{parent}/uid_map", "w", encoding="ascii") as handle:
                handle.write(f"{mapped_uid} {uid} 1\n")
        except OSError as error:
            os._exit(error.errno or 1)
        except BaseException:
            os._exit(1)

        os._exit(0)

    os.close(read_fd)

    namespace_error: OSError | None = None
    try:
        unshare(CLONE_NEWUSER)
    except OSError as error:
        namespace_error = error

    try:
        os.write(write_fd, b"1")
    finally:
        os.close(write_fd)

    result = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    if namespace_error is not None:
        if namespace_error.errno == errno.EPERM:
            raise SandboxOSError(
                errno.EPERM,
                "tine cannot create an unprivileged user namespace; check the host's user-namespace policy",
            ) from namespace_error
        raise namespace_error

    if result:
        raise OSError(result, os.strerror(result))


def _single_user_namespace() -> bool:
    try:
        with open("/proc/self/uid_map", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return False

    return len(lines) == 1 and int(lines[0].split()[-1]) == 1


def _acquire_privileges(*, become_root: bool, network: bool) -> bool:
    if _has_capability(CAP_SYS_ADMIN) and (not become_root or (os.getuid() == 0 and os.getgid() == 0)):
        return False

    unprivileged_user_namespace(become_root=become_root)
    fix_user_namespace_capabilities(network=network)

    return True


def _libseccomp(root: str = "/") -> str:
    """Return the path of libseccomp.so.2 in the tree at `root`."""
    # The stage1 tree that bootstraps a root box contains no /etc/ld.so.cache. A lookup by name then
    # searches only the default directories of the host's dynamic loader, and an Arch host searches only
    # /usr/lib. Debian installs libraries into a multiarch directory, such as usr/lib/x86_64-linux-gnu.
    for directory in ("usr/lib64", f"usr/lib/{os.uname().machine}-linux-gnu", "usr/lib"):
        path = os.path.join(root, directory, "libseccomp.so.2")
        if os.path.exists(path):
            return path
    raise SandboxOSError(errno.ENOENT, "the box contains no libseccomp.so.2")


def _suppress_syscalls(*, chown: bool, sync: bool) -> None:
    if not chown and not sync:
        return

    library = ctypes.CDLL(_libseccomp())

    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    library.seccomp_attr_set.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32)
    library.seccomp_arch_native.argtypes = ()
    library.seccomp_arch_native.restype = ctypes.c_uint32
    library.seccomp_arch_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    library.seccomp_load.argtypes = (ctypes.c_void_p,)

    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise OSError(errno.ENOMEM, os.strerror(errno.ENOMEM))

    try:
        result = library.seccomp_attr_set(context, SCMP_FLTATR_API_SYSRAWRC, 1)
        if result < 0:
            _error("seccomp_attr_set", number=-result)

        for architecture in _COMPAT_ARCHITECTURES.get(library.seccomp_arch_native(), ()):
            result = library.seccomp_arch_add(context, architecture)
            if result < 0 and result not in (-errno.EEXIST, -errno.EDOM):
                _error("seccomp_arch_add", number=-result)

        result = library.seccomp_attr_set(context, SCMP_FLTATR_ACT_BADARCH, SCMP_ACT_ALLOW)
        if result < 0:
            _error("seccomp_attr_set", number=-result)

        syscalls = (_CHOWN_SYSCALLS if chown else ()) + (_SYNC_SYSCALLS if sync else ())
        for syscall in syscalls:
            number = library.seccomp_syscall_resolve_name(syscall)
            if number != -1:
                result = library.seccomp_rule_add(context, SCMP_ACT_ERRNO, number, 0)
                if result < 0:
                    _error("seccomp_rule_add", number=-result)

        result = library.seccomp_load(context)
        if result < 0:
            _error("seccomp_load", number=-result)
    finally:
        library.seccomp_release(context)


def _syscall(call: str, number: int, *arguments: object, path: StrPath | None = None) -> int:
    result = int(_LIBC.syscall(ctypes.c_long(number), *arguments))
    if result < 0:
        _error(call, path)
    return result


def _clone(dirfd: int, path: StrPath, flags: int = 0, attributes: int = 0) -> int:
    """Return a detached copy of the mount at `path`, relative to `dirfd`, with MOUNT_ATTR_* `attributes`."""
    # A clone of a shared mount is a peer of the mount. A mount below the clone would then show up below
    # the source mount, outside of the sandbox. A slave only receives mounts from its source.
    mount_attributes = _MountAttributes(attr_set=attributes, propagation=MS_SLAVE)
    return _syscall(
        "open_tree_attr",
        _NR_OPEN_TREE_ATTR,
        ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(flags | OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC),
        ctypes.c_void_p(ctypes.addressof(mount_attributes)),
        ctypes.c_size_t(MOUNT_ATTR_SIZE_VER0),
        path=path,
    )


def _move_mount(
    fd: int, dirfd: int, path: StrPath = "", flags: int = 0, target: StrPath | None = None
) -> None:
    """Attach the detached mount `fd` at `path`, relative to the directory `dirfd`.

    An error names `target` instead of `path`, for a caller that resolved `target` to `dirfd` itself.
    """
    _syscall(
        "move_mount",
        _NR_MOVE_MOUNT,
        ctypes.c_int(fd),
        ctypes.c_char_p(b""),
        ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH | flags),
        path=path if target is None else target,
    )


def _set_propagation(path: StrPath, propagation: int) -> None:
    """Give the mount at `path` and every mount below it the MS_* `propagation` type."""
    attributes = _MountAttributes(propagation=propagation)
    _syscall(
        "mount_setattr",
        _NR_MOUNT_SETATTR,
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(AT_RECURSIVE),
        ctypes.c_void_p(ctypes.addressof(attributes)),
        ctypes.c_size_t(MOUNT_ATTR_SIZE_VER0),
        path=path,
    )


def _fsopen(filesystem: str) -> int:
    return _syscall(
        "fsopen", _NR_FSOPEN, ctypes.c_char_p(filesystem.encode()), ctypes.c_uint(FSOPEN_CLOEXEC)
    )


def _fsconfig(fd: int, command: int, key: str | None = None, value: str | None = None, aux: int = 0) -> None:
    result = _LIBC.syscall(
        ctypes.c_long(_NR_FSCONFIG),
        ctypes.c_int(fd),
        ctypes.c_uint(command),
        ctypes.c_char_p(None if key is None else key.encode()),
        ctypes.c_char_p(None if value is None else value.encode()),
        ctypes.c_int(aux),
    )
    if result >= 0:
        return
    number = ctypes.get_errno()
    # errno only says that the filesystem rejected the configuration. The filesystem writes the reason
    # into the log of the filesystem context, which a read() of `fd` returns one message at a time.
    messages = []
    while True:
        try:
            messages.append(os.read(fd, 4096).decode(errors="replace").strip())
        except OSError:
            break
    reason = "; ".join(messages) or os.strerror(number)
    # FSCONFIG_CMD_CREATE is the only command without a key.
    raise OSError(number, f"fsconfig {key or 'create'}: {reason}")


def _fsmount(fd: int, attributes: int = 0) -> int:
    return _syscall(
        "fsmount", _NR_FSMOUNT, ctypes.c_int(fd), ctypes.c_uint(FSMOUNT_CLOEXEC), ctypes.c_uint(attributes)
    )


def _tmpfs(mode: str = _TMPFS_DEFAULT_MODE) -> int:
    with Close(_fsopen("tmpfs")) as fs:
        _fsconfig(fs, FSCONFIG_SET_STRING, "source", "tmpfs")
        _fsconfig(fs, FSCONFIG_SET_STRING, "mode", mode)
        _fsconfig(fs, FSCONFIG_CMD_CREATE)
        return _fsmount(fs)


def _overlay(lowers: tuple[str, ...], upper: str | None, work: str | None, attributes: int = 0) -> int:
    """Return a detached overlay. Without `upper` and `work`, the kernel makes it read-only."""
    layers = [("lowerdir+", lower) for lower in lowers]
    layers += [(key, path) for key, path in (("upperdir", upper), ("workdir", work)) if path is not None]
    with Close(_fsopen("overlay")) as fs:
        _fsconfig(fs, FSCONFIG_SET_STRING, "source", "overlayfs")
        for key, path in layers:
            with Close(_lookup(AT_FDCWD, path, os.O_DIRECTORY)) as layer:
                _fsconfig(fs, FSCONFIG_SET_FD, key, aux=layer)
        _fsconfig(fs, FSCONFIG_SET_FLAG, "userxattr")
        _fsconfig(fs, FSCONFIG_SET_STRING, "index", "off")
        _fsconfig(fs, FSCONFIG_SET_STRING, "metacopy", "off")
        _fsconfig(fs, FSCONFIG_CMD_CREATE)
        return _fsmount(fs, attributes)


def _lookup(root: int, path: StrPath, flags: int = 0, *, beneath: bool = False) -> int:
    """Open `path` with O_PATH.

    If `root` is a directory, `path` resolves inside it, and neither `..` nor a symlink leaves it. With
    `beneath`, a path that leaves `root` fails with EXDEV instead.
    """
    if beneath:
        resolve = RESOLVE_BENEATH
    else:
        resolve = 0 if root == AT_FDCWD else RESOLVE_IN_ROOT
    how = _OpenHow(flags=flags | os.O_PATH | os.O_CLOEXEC, resolve=resolve)
    # A lookup inside `root` fails with EAGAIN when a mount or a rename anywhere on the host happens while
    # it resolves a `..` component. openat2(2) asks the caller to retry.
    attempts = 100
    while True:
        try:
            return _syscall(
                "openat2",
                _NR_OPENAT2,
                ctypes.c_int(root),
                ctypes.c_char_p(os.fsencode(path)),
                ctypes.c_void_p(ctypes.addressof(how)),
                ctypes.c_size_t(ctypes.sizeof(how)),
                path=path,
            )
        except BlockingIOError:
            attempts -= 1
            if attempts == 0:
                raise


def _split(path: str) -> tuple[str, str]:
    parent, name = os.path.split(path.rstrip("/") or "/")
    return parent or ".", name


def open_directory(root: int, path: str, *, beneath: bool = False, create: bool = True) -> int:
    """Open the directory `path` in `root` with O_PATH, creating missing parents when `create` is true.

    `path` resolves as in `_lookup()`.
    """
    try:
        return _lookup(root, path, os.O_DIRECTORY, beneath=beneath)
    except FileNotFoundError:
        if not create:
            raise
    parent, name = _split(path)
    with Close(open_directory(root, parent, beneath=beneath)) as fd:
        try:
            os.mkdir(name, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
    return _lookup(root, path, os.O_DIRECTORY, beneath=beneath)


def _touch(dirfd: int, name: str, mode: int = 0o666) -> None:
    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode, dir_fd=dirfd))


def _mount_point(root: int, path: str, *, directory: bool) -> int:
    """Open the mount point for a mount at `path` in `root` with O_PATH, and create it if it's missing.

    A mount of a file or a symlink covers a symlink at `path` itself rather than its target.
    """
    if directory:
        return open_directory(root, path)
    try:
        return _lookup(root, path, os.O_NOFOLLOW)
    except FileNotFoundError:
        pass
    parent, name = _split(path)
    with Close(open_directory(root, parent)) as fd:
        _touch(fd, name, 0o644)
    return _lookup(root, path, os.O_NOFOLLOW)


def _mount_id(dirfd: int, path: str = "") -> int:
    """Return the unique ID of the mount that a lookup of `path` in `dirfd` ends on."""
    buffer = (ctypes.c_char * 256)()
    _syscall(
        "statx",
        _NR_STATX,
        ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_int(AT_SYMLINK_NOFOLLOW | AT_EMPTY_PATH),
        ctypes.c_uint(STATX_MNT_ID_UNIQUE),
        buffer,
        path=path or None,
    )
    return ctypes.c_uint64.from_buffer(buffer, _STATX_MNT_ID).value


def _listmount(mount_id: int) -> list[int]:
    """Return the IDs of the mounts below the mount `mount_id`, including covered ones, oldest first."""
    request = _MountIdRequest(size=MNT_ID_REQ_SIZE_VER0, mnt_id=mount_id)
    batch = (ctypes.c_uint64 * 256)()
    ids: list[int] = []
    while True:
        count = _syscall(
            "listmount",
            _NR_LISTMOUNT,
            ctypes.c_void_p(ctypes.addressof(request)),
            ctypes.c_void_p(ctypes.addressof(batch)),
            ctypes.c_size_t(len(batch)),
            ctypes.c_uint(0),
        )
        ids += batch[:count]
        if count < len(batch):
            return ids
        # The next call continues after the ID in `param`.
        request.param = ids[-1]


def _statmount_point(mount_id: int) -> str:
    """Return the mount point of the mount `mount_id`, relative to the process root."""
    request = _MountIdRequest(size=MNT_ID_REQ_SIZE_VER0, mnt_id=mount_id, param=STATMOUNT_MNT_POINT)
    size = 4096
    while True:
        buffer = (ctypes.c_char * size)()
        result = _LIBC.syscall(
            ctypes.c_long(_NR_STATMOUNT),
            ctypes.c_void_p(ctypes.addressof(request)),
            ctypes.c_void_p(ctypes.addressof(buffer)),
            ctypes.c_size_t(size),
            ctypes.c_uint(0),
        )
        if result >= 0:
            break
        # statmount() fails with EOVERFLOW when the strings don't fit into the buffer.
        if ctypes.get_errno() != errno.EOVERFLOW:
            _error("statmount")
        size *= 2
    offset = ctypes.c_uint32.from_buffer(buffer, _STATMOUNT_MNT_POINT).value
    return os.fsdecode(ctypes.string_at(ctypes.addressof(buffer) + _STATMOUNT_STRINGS + offset))


def submounts(directory: str) -> list[str]:
    """Return the mount points below the absolute path `directory` that a lookup reaches, parents first."""
    prefix = directory if directory.endswith("/") else directory + "/"
    points = []
    for mount_id in _listmount(_mount_id(AT_FDCWD, directory)):
        point = _statmount_point(mount_id)
        if not point.startswith(prefix):
            continue
        # listmount() also returns the mounts that a later mount covers. A lookup of the mount point of a
        # covered mount ends on the mount on top, or fails when the mount on top has no such path, a
        # directory the caller can't search or a symlink loop.
        try:
            visible = _mount_id(AT_FDCWD, point) == mount_id
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.ELOOP):
                raise
            visible = False
        if visible:
            points.append(point)
    return sorted(points, key=lambda point: point.count("/"))


class Close:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self) -> int:
        return self.fd

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


class chroot:
    """Temporarily change the process root and restore it afterward."""

    def __init__(self, root: StrPath) -> None:
        self.root = os.fspath(root)
        self.root_fd = -1

    def __enter__(self) -> None:
        if self.root_fd >= 0:
            raise RuntimeError("chroot context is already entered")
        self.previous = os.getcwd()
        self.root_fd = os.open(_ROOT, os.O_CLOEXEC | os.O_PATH | os.O_DIRECTORY)
        self.changed = False
        try:
            os.chroot(self.root)
            self.changed = True
            os.chdir(_ROOT)
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            if self.changed:
                os.fchdir(self.root_fd)
                os.chroot(".")
                os.chdir(self.previous)
        finally:
            os.close(self.root_fd)
            self.root_fd = -1


class _Mount:
    """A filesystem that the context builds as a detached mount, for `attach()` or `mount()` at `target`.

    The context exit closes the file descriptor of the mount. It also unmounts the mount if `mount()`
    attached it.
    """

    # A strict unmount fails with EBUSY while a mount has submounts, for example the device nodes of
    # `Devices` or the submounts that a recursive `Bind` copies. Unmounting the submounts first doesn't work
    # either. A submount inherited across a user namespace is locked, and unmounting it on its own fails
    # with EINVAL.
    _unmount_flags = MNT_DETACH

    def __init__(self, target: StrPath, *, strict_unmount: bool = False) -> None:
        self.target = os.fspath(target)
        # A lazy unmount also succeeds while a process still uses the mount, and that process can keep
        # writing into it. A caller that reads what the mount wrote after the unmount passes
        # `strict_unmount`. A strict unmount fails with EBUSY while a process still uses the mount.
        if strict_unmount:
            self._unmount_flags = 0
        self.fd = -1
        self._mounted: str | None = None

    def _open(self) -> int:
        """Build the detached mount and return its file descriptor."""
        raise NotImplementedError

    def __enter__(self) -> Self:
        self.fd = self._open()
        return self

    def __exit__(self, *exc: object) -> None:
        # A strict unmount fails with EBUSY while a file descriptor refers to the mount.
        os.close(self.fd)
        self.fd = -1
        if self._mounted is not None:
            mounted, self._mounted = self._mounted, None
            umount2(mounted, self._unmount_flags | UMOUNT_NOFOLLOW)

    def attach(self, root: int = AT_FDCWD) -> None:
        """Attach the mount at `target`, resolved inside the directory `root`, and keep it attached."""
        directory = stat.S_ISDIR(os.fstat(self.fd).st_mode)
        with Close(_mount_point(root, self.target, directory=directory)) as point:
            _move_mount(self.fd, point, flags=MOVE_MOUNT_T_EMPTY_PATH, target=self.target)

    def mount(self) -> None:
        """Attach the mount at `target`, and unmount it when the context exits."""
        self.attach()
        # statmount() returns the mount point with every symlink resolved. The path stays correct when the
        # working directory or a symlink in `target` changes before the context exits.
        try:
            self._mounted = _statmount_point(_mount_id(self.fd))
        except BaseException:
            umount2(f"/proc/self/fd/{self.fd}", MNT_DETACH)
            raise


def open_layers(layers: tuple[str, ...]) -> int:
    """Return a detached read-only mount of the directory in `layers`, or an overlay of several.

    `layers` lists the directories top first, the order of overlayfs lower directories.
    """
    # A copy keeps the flags of the mount it comes from, for example nosuid and nodev. An overlay is a new
    # mount and has neither flag. The tools tree gets MOUNT_ATTR_NOSUID and MOUNT_ATTR_NODEV either way, so
    # that its flags don't depend on the number of layers.
    attributes = MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV
    if len(layers) == 1:
        return _clone(AT_FDCWD, layers[0], AT_RECURSIVE, attributes)
    return _overlay(layers, None, None, attributes)


class ExitStack:
    """Exit the contexts that `enter_context()` entered, in reverse order."""

    def __init__(self) -> None:
        self._contexts: list[_Mount | Close] = []

    def enter_context(self, context: _Mount | Close) -> None:
        context.__enter__()
        self._contexts.append(context)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._contexts:
            return
        try:
            self._contexts.pop().__exit__(None, None, None)
        finally:
            # The remaining contexts exit even when `__exit__()` of this context raises.
            self.__exit__()


class Bind(_Mount):
    def __init__(
        self,
        source: StrPath,
        target: StrPath,
        readonly: bool = False,
        nofollow: bool = False,
        root: int = AT_FDCWD,
        *,
        strict_unmount: bool = False,
    ) -> None:
        """Bind `source`, a path in the directory `root`, at `target`.

        If `root` is a directory, `source` resolves inside it, and neither `..` nor a symlink leaves it.
        With `nofollow`, a symlink at `source` is bound itself instead of its target.
        """
        super().__init__(target, strict_unmount=strict_unmount)
        self.source = os.fspath(source)
        self.readonly = readonly
        self.nofollow = nofollow
        self.root = root

    @override
    def _open(self) -> int:
        flags = AT_RECURSIVE | AT_NO_AUTOMOUNT
        if self.nofollow:
            flags |= AT_SYMLINK_NOFOLLOW
        attributes = MOUNT_ATTR_RDONLY if self.readonly else 0
        if self.root == AT_FDCWD:
            return _clone(AT_FDCWD, self.source, flags, attributes)
        with Close(_lookup(self.root, self.source, os.O_NOFOLLOW if self.nofollow else 0)) as source:
            return _clone(source, "", flags | AT_EMPTY_PATH, attributes)


class Devices(_Mount):
    def __init__(self, target: StrPath, tty: StrPath | None = None) -> None:
        super().__init__(target)
        self.tty = None if tty is None else os.fspath(tty)

    @override
    def _open(self) -> int:
        with Close(_tmpfs()) as fd:
            nodes = [("/dev/" + name, name) for name in ("null", "zero", "full", "random", "urandom", "tty")]
            if os.path.exists("/dev/fuse"):
                nodes.append(("/dev/fuse", "fuse"))
            if self.tty is not None:
                nodes.append((self.tty, "console"))
            for source, name in nodes:
                _touch(fd, name)
                with Close(_clone(AT_FDCWD, source)) as node:
                    _move_mount(node, fd, name)

            for descriptor, name in enumerate(("stdin", "stdout", "stderr")):
                os.symlink(f"/proc/self/fd/{descriptor}", name, dir_fd=fd)
            os.symlink("/proc/self/fd", "fd", dir_fd=fd)
            os.symlink("/proc/kcore", "core", dir_fd=fd)
            os.mkdir("shm", mode=0o1777, dir_fd=fd)
            os.mkdir("pts", mode=0o755, dir_fd=fd)
            with Close(_fsopen("devpts")) as fs:
                _fsconfig(fs, FSCONFIG_SET_STRING, "source", "devpts")
                _fsconfig(fs, FSCONFIG_SET_FLAG, "newinstance")
                _fsconfig(fs, FSCONFIG_SET_STRING, "ptmxmode", "0666")
                _fsconfig(fs, FSCONFIG_SET_STRING, "mode", "620")
                _fsconfig(fs, FSCONFIG_CMD_CREATE)
                with Close(_fsmount(fs)) as pts:
                    _move_mount(pts, fd, "pts")
            os.symlink("pts/ptmx", "ptmx", dir_fd=fd)
            return os.dup(fd)


class File(_Mount):
    """A read-only file with `content` that only this mount shows."""

    def __init__(self, content: str, target: StrPath) -> None:
        super().__init__(target)
        self.content = content

    @override
    def _open(self) -> int:
        # The file is the only file of a detached tmpfs, which no other process can reach.
        with Close(_tmpfs()) as files:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            with open(os.open("file", flags, 0o644, dir_fd=files), "w", encoding="utf-8") as handle:
                handle.write(self.content)
            return _clone(files, "file", attributes=MOUNT_ATTR_RDONLY)


class Tmpfs(_Mount):
    def __init__(self, target: StrPath, mode: str = _TMPFS_DEFAULT_MODE) -> None:
        super().__init__(target)
        self.mode = mode

    @override
    def _open(self) -> int:
        return _tmpfs(self.mode)


class Symlink:
    def __init__(self, source: StrPath, target: StrPath) -> None:
        self.source = os.fspath(source)
        self.target = os.fspath(target)

    def attach(self, root: int = AT_FDCWD) -> None:
        """Create the symlink at `target`, resolved inside the directory `root`."""
        parent, name = _split(self.target)
        with Close(open_directory(root, parent)) as fd:
            try:
                os.symlink(self.source, name, dir_fd=fd)
            except FileExistsError:
                if not stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode):
                    raise
                if os.readlink(name, dir_fd=fd) != self.source:
                    raise


class Overlay(_Mount):
    def __init__(
        self,
        lowerdirs: tuple[StrPath, ...],
        # Without an upperdir and a workdir, the kernel mounts the overlay read-only.
        upperdir: StrPath | None,
        workdir: StrPath | None,
        target: StrPath,
        *,
        strict_unmount: bool = False,
    ) -> None:
        super().__init__(target, strict_unmount=strict_unmount)
        if (upperdir is None) != (workdir is None):
            raise ValueError("Overlay takes an upperdir and a workdir, or neither")
        if upperdir is None and len(lowerdirs) < 2:
            raise ValueError("a read-only Overlay needs at least two lowerdirs")
        self.lowerdirs = tuple(os.fspath(path) for path in lowerdirs)
        self.upperdir = None if upperdir is None else os.fspath(upperdir)
        self.workdir = None if workdir is None else os.fspath(workdir)

    @override
    def _open(self) -> int:
        return _overlay(self.lowerdirs, self.upperdir, self.workdir)


Filesystem = Bind | File | Devices | Tmpfs | Symlink | Overlay


def _filesystem_key(filesystem: Filesystem) -> tuple[tuple[str, ...], bool]:
    parts = tuple(part for part in filesystem.target.split("/") if part and part != ".")
    return parts, isinstance(filesystem, Bind)


def _loopback_up() -> None:
    """Bring `lo` up in a fresh network namespace."""
    import fcntl
    import socket
    import struct

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # struct ifreq: the name, the flags, and padding the kernel does not read for these two.
        request = struct.pack("16sH22x", b"lo", 0)
        flags = struct.unpack_from("16sH", fcntl.ioctl(probe, SIOCGIFFLAGS, request))[1]
        fcntl.ioctl(probe, SIOCSIFFLAGS, struct.pack("16sH22x", b"lo", flags | IFF_UP))


def check_kernel() -> None:
    """Fail with a message that names the kernel version when the kernel is too old for tine's mounts."""
    # Linux 6.15 adds open_tree_attr() together with cloning a mount out of a detached mount, which
    # tine needs to build its mounts. The call below passes an empty path, so it fails on every kernel and
    # needs no privileges. A kernel with open_tree_attr() fails it with ENOENT, and an older kernel fails
    # it with ENOSYS.
    result = _LIBC.syscall(
        ctypes.c_long(_NR_OPEN_TREE_ATTR),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(b""),
        ctypes.c_uint(0),
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
    )
    if result < 0 and ctypes.get_errno() == errno.ENOSYS:
        raise SandboxOSError(
            errno.ENOSYS,
            "tine needs open_tree_attr() (Linux 6.15 or newer); it is unavailable on "
            f"Linux {os.uname().release}. A seccomp filter can also block this system call.",
        )


def unshare_sandbox(
    *,
    become_root: bool = False,
    isolate_network: bool = False,
) -> None:
    """Enter the namespaces of a sandbox without replacing the root.

    A mount that the caller makes between this call and `enter_sandbox()` is only visible inside the
    sandbox's mount namespace.
    """
    user_namespace = _acquire_privileges(
        become_root=become_root,
        network=isolate_network,
    )

    namespaces = CLONE_NEWNS
    if isolate_network and _has_capability(CAP_NET_ADMIN):
        namespaces |= CLONE_NEWNET

    try:
        unshare(namespaces)
    except OSError as error:
        if error.errno == errno.EPERM:
            raise SandboxOSError(
                errno.EPERM,
                "tine cannot create the sandbox mount namespace; check the host's namespace policy",
            ) from error
        raise
    if namespaces & CLONE_NEWNET:
        _loopback_up()

    if not user_namespace:
        _set_propagation(_ROOT, MS_SLAVE)


def _switch_root(root: int) -> None:
    """Make the directory `root` the root of the process."""
    # pivot_root() only accepts a new root that is attached in the mount namespace. Attach the new root on
    # top of the old root first.
    _move_mount(root, AT_FDCWD, _ROOT)
    os.fchdir(root)
    if _LIBC.pivot_root(b".", b".") < 0:
        if ctypes.get_errno() == errno.EINVAL:
            raise SandboxOSError(
                errno.EINVAL,
                "tine cannot switch to the sandbox root: pivot_root returned EINVAL. "
                "Inspect the host root with `findmnt -o FSTYPE,PROPAGATION /`; "
                "an initramfs root may require booting from a mounted filesystem.",
            )
        _error("pivot_root")
    # pivot_root(".", ".") puts the old root on top of the new root.
    umount2(".", MNT_DETACH)
    os.chdir(_ROOT)


def enter_sandbox(
    filesystems: tuple[Filesystem, ...],
    chdir: StrPath | None = None,
    *,
    suppress_chown: bool = False,
    suppress_sync: bool = False,
) -> None:
    """Replace the root with `filesystems`. Call `unshare_sandbox()` and enter every mount first.

    `suppress_chown` makes chown calls succeed without effect, and `suppress_sync` does the same for sync
    calls. The seccomp filter uses the libseccomp of the new root.
    """
    for filesystem in filesystems:
        if not os.path.isabs(filesystem.target):
            raise ValueError(f"sandbox destination must be absolute: {filesystem.target}")

    # In a user namespace that maps a single user, a chown to any other user fails, and package installs
    # chown their files. The user namespace that unshare_sandbox() creates maps a single user. Read the
    # map before the root changes, because the new root may not contain /proc.
    chown = suppress_chown and _single_user_namespace()

    with Close(_tmpfs()) as root:
        for filesystem in sorted(filesystems, key=_filesystem_key):
            filesystem.attach(root)
        _switch_root(root)

    _suppress_syscalls(chown=chown, sync=suppress_sync)

    if chdir is not None:
        os.chdir(chdir)
