# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run commands with pinned userspace through tine's Linux sandbox.

Every build action starts this launcher. Keep imports, including transitive imports, to the absolute
minimum to minimize startup time.

The default mode provides a clean, isolated build environment. `--relaxed` retains the
pinned userspace but exposes host devices, services, environment, cwd, and network.
Target-root setup belongs to rootfs.py rather than this launcher.
"""

import os
import stat
import sys
import warnings  # noqa: F401 (os.execvpe imports it after the host Python is no longer reachable)

from isolation import (
    Bind,
    Close,
    Devices,
    ExitStack,
    File,
    Filesystem,
    SandboxOSError,
    Symlink,
    Tmpfs,
    enter_sandbox,
    open_layers,
    unshare_sandbox,
)

TYPE_CHECKING = False

# Older host interpreters evaluate annotations when defining the functions.
if TYPE_CHECKING or sys.version_info < (3, 14):
    from typing import NoReturn

# Hermetic sandbox mount point of the project (host cwd). A path of tine's own rather than just keeping
# the host path: a project under /var/lib or /root would otherwise have to be mounted inside one of the
# box's own read-only directories. No distribution ships a /tine, so nothing can be in the way.
# A chrooted operation cannot use this path (would otherwise leak into the image). That binds the project
# under /run instead; see PROJECT in image/layer.py.
_PROJECT = "/tine/project"

# Kernel APIs and ephemeral trees supplied by the sandbox.
_PROVIDED = frozenset({"proc", "sys", "dev", "run", "tmp", "boot"})

# Relaxed mode takes userspace from tools and everything else from the host.
_TOOLS_DIRS = ("usr", "opt")
_TOOLS_LINKS = ("bin", "sbin", "lib", "lib32", "lib64")
_HOST_SKIP = frozenset({"proc", "nix", "etc", *_TOOLS_DIRS, *_TOOLS_LINKS})
_HOST_ETC = ("machine-id",)

# Deterministic environment replacing the sandbox's inherited host environment.
_BASE_ENV = {
    "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": "/root",
    # Large staging trees, so the disk-backed /var/tmp rather than the /tmp tmpfs.
    "TMPDIR": "/var/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}

_HELP = """\
usage: sandbox --tools TOOLS [OPTIONS...] [--] COMMAND [ARGUMENTS...]

  -h, --help                 Show this help
  --tools TOOLS              Read-only pinned tools tree (required); repeat to
                             stack layers, lowest first
  --ro-bind SRC:DST          Add a read-only bind mount (repeatable)
  --setenv K=V               Set an environment variable (repeatable)
  --source-date-epoch EPOCH   Set SOURCE_DATE_EPOCH
  --bind-cwd                 Bind and enter the project root
  --network                  Grant network access (default: unshared)
  --box NAME                 Enter a named development box (requires --relaxed)
  --relaxed                  Expose host devices, services, environment, cwd, and network
"""


def _fail(message: str) -> NoReturn:
    # The shared util module also loads networking and compression, which every action would pay for.
    sys.exit(f"tine: {message}")


def _prompt_prefix(name: str, previous: str, prefix: str) -> str:
    marker = f"({previous})"
    if previous and marker in prefix:
        return prefix.replace(marker, f"({name})", 1)
    return f"({name}){prefix}"


class Launch:
    """Pair the sandbox's filesystems with the process it launches."""

    def __init__(
        self,
        filesystems: tuple[Filesystem, ...],
        chdir: str | None,
        command: tuple[str, ...],
        environment: dict[str, str],
    ) -> None:
        self.filesystems = filesystems
        self.chdir = chdir
        self.command = command
        self.environment = environment


class Options:
    def __init__(self) -> None:
        self.tools: list[str] = []
        self.ro_bind: list[tuple[str, str]] = []
        self.setenv: dict[str, str] = {}
        self.source_date_epoch: int | None = None
        self.bind_cwd = False
        self.network = False
        self.box: str | None = None
        self.relaxed = False
        self.cmd: list[str] = []


def _relocate(value: str, cwd: str) -> str:
    """Point an absolute path into the project at where the sandbox mounts the project."""
    if value == cwd:
        return _PROJECT
    return value.replace(cwd + "/", _PROJECT + "/")


def _relaxed_path(path: str) -> str:
    directories = path.split(os.pathsep) if path else []
    missing = [d for d in _BASE_ENV["PATH"].split(os.pathsep) if d not in directories]
    return os.pathsep.join(directories + missing)


def _mode(tools: int, path: str, *, follow_symlinks: bool = True) -> int:
    """Return the mode of `path` in the tools tree, or 0 if it doesn't exist."""
    try:
        return os.stat(path, dir_fd=tools, follow_symlinks=follow_symlinks).st_mode
    except FileNotFoundError:
        return 0


def _relaxed(tools: int) -> list[Filesystem]:
    """Mount pinned userspace over a host-integrated root."""
    out: list[Filesystem] = []
    for name in _TOOLS_DIRS:
        if stat.S_ISDIR(_mode(tools, name)):
            out.append(Bind(name, "/" + name, readonly=True, root=tools))
    for name in _TOOLS_LINKS:
        mode = _mode(tools, name, follow_symlinks=False)
        if stat.S_ISLNK(mode):
            out.append(Symlink(os.readlink(name, dir_fd=tools), "/" + name))
        elif stat.S_ISDIR(mode):
            out.append(Bind(name, "/" + name, readonly=True, root=tools))
    for name in sorted(os.listdir("/")):
        if name in _HOST_SKIP:
            continue
        entry = "/" + name
        if os.path.islink(entry):
            out.append(Symlink(os.readlink(entry), entry))
        else:
            out.append(Bind(entry, entry))
    if stat.S_ISDIR(_mode(tools, "etc")):
        out.append(Bind("etc", "/etc", readonly=True, root=tools))
    for f in _HOST_ETC:
        host = os.path.join("/etc", f)
        if os.path.exists(host) and _mode(tools, f"etc/{f}"):
            out.append(Bind(host, host, readonly=True))
    return out + _identity(tools)


def _identity(tools: int) -> list[Filesystem]:
    """Make the invoking uid/gid resolvable inside the sandbox.

    Relaxed /etc comes from the tools tree, which lists only system users. On a host the caller's uid
    is resolved by nss-systemd via the bound /run, but where that is unavailable (e.g. a CI runner
    whose uid is served by neither files nor userdb) getpwuid() fails and callers like ssh-keygen
    abort. Append an entry for the caller to the passwd/group tables and bind them over /etc; a file
    bind stacks over the read-only /etc mount, which a plain write could not.
    """
    uid, gid = os.getuid(), os.getgid()
    name = os.environ.get("USER") or ""
    home = os.environ.get("HOME") or ""
    if not name or not name.isascii() or ":" in name or "\n" in name:
        name = f"u{uid}"
    if not home or ":" in home or "\n" in home:
        home = "/root"
    tables = {
        "passwd": f"{name}:x:{uid}:{gid}::{home}:/bin/sh\n",
        "group": f"{name}:x:{gid}:\n",
    }
    out: list[Filesystem] = []
    for base, entry in tables.items():
        content = ""
        if _mode(tools, f"etc/{base}"):
            with open(
                os.open(f"etc/{base}", os.O_RDONLY | os.O_CLOEXEC, dir_fd=tools), encoding="utf-8"
            ) as handle:
                content = handle.read()
        out.append(File(content + entry, "/etc/" + base))
    return out


def _box(name: str) -> dict[str, str]:
    """Announce the box in the environment, counting depth when boxes nest."""
    previous = os.environ.get("TINE_BOX", "") if os.environ.get("TINE_IN_BOX") else ""
    if previous:
        _, separator, level = previous.rpartition(":")
        nested = separator and level.isascii() and level.isdecimal() and level[0] != "0"
        name = f"{name}:{int(level) + 1 if nested else 2}"
    out = {"TINE_BOX": name, "TINE_IN_BOX": "1"}

    # Starship owns the prompt layout; TINE_BOX is rendered through its env_var module instead.
    if os.environ.get("STARSHIP_SHELL"):
        return out
    prefix = os.environ.get("SHELL_PROMPT_PREFIX", "")
    out["SHELL_PROMPT_PREFIX"] = _prompt_prefix(name, previous, prefix)
    return out


def _value(argument: str, argv: list[str]) -> str:
    option, separator, value = argument.partition("=")
    if not separator:
        if not argv or argv[-1].startswith("--") or argv[-1] == "-h":
            _fail(f"{option} requires a value")
        value = argv.pop()
    if not value:
        _fail(f"{option} requires a value")
    return value


def _parse(argv: list[str] | None) -> Options:
    """Read the request, rejecting the flag combinations that describe no sandbox."""
    # argparse's imports and parser setup would run for every build action.
    args = Options()
    argv = list(reversed(sys.argv[1:] if argv is None else argv))
    while argv:
        argument = argv.pop()
        if argument == "--":
            break
        if not argument.startswith("-") or argument == "-":
            argv.append(argument)
            break
        if argument in ("-h", "--help"):
            print(_HELP, end="")
            raise SystemExit(0)

        option = argument.partition("=")[0]
        if option == "--tools":
            args.tools.append(_value(argument, argv))
        elif option == "--ro-bind":
            source, separator, target = _value(argument, argv).partition(":")
            if not source or not separator or not os.path.isabs(target):
                _fail("--ro-bind requires SRC:/DST")
            args.ro_bind.append((source, target))
        elif option == "--setenv":
            key, separator, value = _value(argument, argv).partition("=")
            if not key or not separator:
                _fail("--setenv requires NAME=VALUE")
            args.setenv[key] = value
        elif option == "--source-date-epoch":
            value = _value(argument, argv)
            try:
                args.source_date_epoch = int(value)
            except ValueError:
                _fail(f"{option} requires an integer, got {value!r}")
        elif option == "--box":
            args.box = _value(argument, argv)
        elif argument == "--bind-cwd":
            args.bind_cwd = True
        elif argument == "--network":
            args.network = True
        elif argument == "--relaxed":
            args.relaxed = True
        else:
            _fail(f"unrecognized option: {argument}")

    args.cmd = list(reversed(argv))
    if not args.tools:
        _fail("--tools is required")
    if not args.cmd and not args.box:
        _fail("no command given (expected `-- cmd ...`)")
    if args.relaxed and args.bind_cwd:
        _fail("--bind-cwd is for hermetic builds; --relaxed sees the host cwd already")
    if args.box and not args.relaxed:
        _fail("--box describes an interactive host-integrated shell; it requires --relaxed")
    return args


def _tty() -> str | None:
    try:
        return os.ttyname(2) if os.isatty(2) else None
    except FileNotFoundError:
        return None


def _which(command: str, path: str) -> str | None:
    if "/" in command:
        candidates = [command]
    else:
        candidates = (
            [os.path.join(directory, command) for directory in path.split(os.pathsep)] if path else []
        )
    for candidate in candidates:
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


def _interactive_shell(environment: dict[str, str]) -> str:
    """Choose an executable shell from the mounted box."""
    path = environment.get("PATH", os.defpath)
    preferred = environment.get("SHELL")
    if preferred and (shell := _which(preferred, path)) is not None:
        return shell
    if (shell := _which("bash", path)) is not None:
        environment["SHELL"] = shell
        # Starship belongs to the unavailable host shell. Let fallback bash use the standard marker.
        if environment.pop("STARSHIP_SHELL", None) is not None:
            previous = os.environ.get("TINE_BOX", "") if os.environ.get("TINE_IN_BOX") else ""
            if name := environment.get("TINE_BOX"):
                prefix = environment.get("SHELL_PROMPT_PREFIX", "")
                environment["SHELL_PROMPT_PREFIX"] = _prompt_prefix(name, previous, prefix)
        return shell
    _fail("no shell installed in box ($SHELL and bash were not found)")


def _launch(args: Options, tools: int) -> Launch:
    """Translate the command-line request into the Python sandbox interface.

    `tools` is a file descriptor of the tools tree, which the binds of the tools tree take their sources
    from.
    """
    filesystems: list[Filesystem] = []
    cwd = os.getcwd() if args.bind_cwd else None

    # Recreate usr-merge symlinks instead of binding through them.
    if args.relaxed:
        filesystems += _relaxed(tools)
    else:
        # os.listdir() needs a file descriptor that is open for reading, and `tools` is an O_PATH file
        # descriptor.
        with Close(os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=tools)) as directory:
            names = sorted(os.listdir(directory))
        for name in names:
            if name in _PROVIDED:
                continue
            mode = _mode(tools, name, follow_symlinks=False)
            if stat.S_ISLNK(mode):
                filesystems.append(Symlink(os.readlink(name, dir_fd=tools), "/" + name))
            elif stat.S_ISDIR(mode):
                filesystems.append(Bind(name, "/" + name, readonly=True, root=tools))

    for src, dest in args.ro_bind:
        filesystems.append(Bind(src, dest, readonly=True))

    chdir: str | None = None
    command = tuple(args.cmd)
    if cwd:
        filesystems.append(Bind(cwd, _PROJECT))
        chdir = _PROJECT

        # A build action names its artifacts project-relative, but `buck run` calls the same command with
        # an absolute path, so translate it for our PROJECT mount. Only the command needs it: a bind
        # source is resolved on the host, and a setenv value carries a path inside the sandbox already.
        command = tuple(_relocate(argument, cwd) for argument in command)

    # Package scripts require writable API and temporary filesystems.
    filesystems.append(Bind("/proc", "/proc"))
    if not args.relaxed:
        filesystems.append(Devices("/dev", _tty()))
        filesystems.append(Tmpfs("/run"))
        filesystems.append(Tmpfs("/tmp", mode="1777"))

        # Everything large stages under /var/tmp, which TMPDIR points at: back it with Buck's
        # on-disk per-action scratch directory rather than a tmpfs, since staging trees run into
        # gigabytes and should not go into RAM. /tmp keeps the tmpfs above, for small and
        # short-lived files only.
        #
        # Entered outside a run action there is no scratch directory: `buck run` on a box or
        # on one of its lock targets, and every `buck test`. Those keep a tmpfs here too, which dies
        # with the mount namespace.
        staging = None
        if cwd and "BUCK_SCRATCH_PATH" in os.environ:
            staging = os.path.join(cwd, os.environ["BUCK_SCRATCH_PATH"])
        if staging is None:
            filesystems.append(Tmpfs("/var/tmp", mode="1777"))
        else:
            backing = os.path.join(staging, "var-tmp")
            os.makedirs(backing, exist_ok=True)
            filesystems.append(Bind(backing, "/var/tmp"))

    environment = dict(os.environ) if args.relaxed else dict(_BASE_ENV)
    if args.relaxed:
        # The host's PATH can lack a directory that the box installs programs into. A Debian box
        # installs tpm2-abrmd into /usr/sbin, and an Arch host has no /usr/sbin on PATH. Without the
        # base directories, running tpm2-abrmd in a Debian box on an Arch host fails with "command
        # not found".
        environment["PATH"] = _relaxed_path(environment.get("PATH", ""))
    if not args.relaxed and args.bind_cwd and "BUCK_SCRATCH_PATH" in os.environ:
        environment["BUCK_SCRATCH_PATH"] = os.environ["BUCK_SCRATCH_PATH"]
    if args.source_date_epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(args.source_date_epoch)
    environment.update(args.setenv)
    if args.box:
        environment.update(_box(args.box))
    if args.relaxed:
        # Resolve through the host /run while keeping the tools tree's /etc.
        if os.path.exists("/etc/resolv.conf"):
            filesystems.append(
                Bind(
                    "/etc/resolv.conf",
                    "/etc/resolv.conf",
                    readonly=True,
                    nofollow=True,
                )
            )
        chdir = chdir or os.getcwd()
    elif args.network:
        # Preserve box CA trust but use the host resolver and its /run target.
        filesystems.append(
            Bind(
                "/etc/resolv.conf",
                "/etc/resolv.conf",
                readonly=True,
                nofollow=True,
            )
        )
        filesystems.append(Bind("/run", "/run", readonly=True))

    return Launch(filesystems=tuple(filesystems), chdir=chdir, command=command, environment=environment)


def main(argv: list[str] | None = None) -> NoReturn:
    args = _parse(argv)
    # `layers` lists the layers of the tools tree top first, which is the order of overlayfs lower
    # directories.
    layers = tuple(reversed(args.tools))
    try:
        unshare_sandbox(
            become_root=not args.relaxed,
            isolate_network=not args.relaxed and not args.network,
        )
        with ExitStack() as stack:
            # The tools tree is opened after unshare_sandbox(), so that an overlay of its layers belongs to
            # the sandbox's user namespace.
            tools = open_layers(layers)
            stack.enter_context(Close(tools))
            launch = _launch(args, tools)
            # The mounts take their sources from paths in the host's root, so they are entered before
            # enter_sandbox() changes the root. _clone_project() already entered `project`.
            for filesystem in launch.filesystems:
                if not isinstance(filesystem, Symlink):
                    stack.enter_context(filesystem)
            enter_sandbox(
                launch.filesystems,
                launch.chdir,
                suppress_chown=not args.relaxed,
                suppress_sync=not args.relaxed,
            )
    except SandboxOSError as error:
        print(error.message, file=sys.stderr)
        raise
    # Shell paths must be tested after entering, against the userspace the box mounted over the host.
    command = launch.command or (_interactive_shell(launch.environment),)
    try:
        os.execvpe(command[0], command, launch.environment)
    except FileNotFoundError:
        raise SystemExit(127) from None
    raise SystemExit(127)


if __name__ == "__main__":
    main()
