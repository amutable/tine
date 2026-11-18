# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public HTTP download API, exported as the `http` namespace."""

# Explicit loads keep Starlark type checking precise.
load("@prelude//:rules.bzl", "http_file")
load("//platform:architecture.bzl", "architecture")

def _tool_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src
    if ctx.attrs.unzstd != None:
        out = ctx.actions.declare_output(ctx.label.name)
        ctx.actions.run(
            cmd_args(ctx.attrs.unzstd[RunInfo], src, out.as_output()),
            category = "unzstd",
        )
        src = out
    elif ctx.attrs.untar != None:
        out = ctx.actions.declare_output(ctx.label.name, dir = True)
        ctx.actions.run(
            cmd_args(ctx.attrs.untar[RunInfo], src, out.as_output()),
            category = "untar",
        )
        src = out
    root = src.project(ctx.attrs.strip_prefix) if ctx.attrs.strip_prefix else src
    if not ctx.attrs.directory:
        program = root.project(ctx.attrs.path) if ctx.attrs.path != None else root
        return [DefaultInfo(default_output = program), RunInfo(args = cmd_args(program, ctx.attrs.args))]
    providers = [DefaultInfo(default_output = root)]
    if ctx.attrs.path != None:
        # `buck run` needs the whole unpacked archive, because a program such as go reads the files beside it.
        providers.append(RunInfo(args = cmd_args(root.project(ctx.attrs.path), ctx.attrs.args, hidden = src)))
    return providers

_tool = rule(
    impl = _tool_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = []),
        "directory": attrs.bool(default = False),
        "path": attrs.option(attrs.string(), default = None),
        "src": attrs.source(),
        "strip_prefix": attrs.option(attrs.string(), default = None),
        # `untar` and `unzstd` are python.binary targets, and the pinned Python runs them. The pinned Python
        # is an http_tool too, so a dep on `untar` or `unzstd` in every http_tool makes it depend on itself.
        # Only a download that needs `untar` or `unzstd` sets the attribute. The pinned Python's own archive
        # uses `tine//http:host-untar` instead.
        "untar": attrs.option(attrs.exec_dep(providers = [RunInfo]), default = None),
        "unzstd": attrs.option(attrs.exec_dep(providers = [RunInfo]), default = None),
    },
)

def _host_python_script_impl(ctx: AnalysisContext) -> list[Provider]:
    return [DefaultInfo(default_output = ctx.attrs.src), RunInfo(args = cmd_args("python3", "-SIB", ctx.attrs.src))]

host_python_script = rule(
    impl = _host_python_script_impl,
    attrs = {
        "src": attrs.source(),
    },
)

def _download(spec: dict) -> dict[str, Select]:
    """The `urls`, `sha256` and `size_bytes` of Buck's http rules for a tine.lock.json entry.

    The artifact is downloaded from the GitHub release of the entry's `repository`, or from the entry's
    `url`.
    """
    base = spec["url"] if "url" in spec else "https://github.com/{}/releases/download/{}".format(spec["repository"], spec["release"])
    platforms = spec["platforms"]
    return {
        "sha256": architecture.select({cpu: e["sha256"] for cpu, e in platforms.items()}),
        # Without a size buck asks the server for one with a HEAD request before every download, which is
        # a second chance for a release host to fail a build that has the bytes pinned already.
        "size_bytes": architecture.select({cpu: e["size"] for cpu, e in platforms.items()}),
        "urls": architecture.select({cpu: ["{}/{}".format(base, e["artifact"])] for cpu, e in platforms.items()}),
    }

def http_pinned_file(name: str, spec: dict, executable: bool = False, visibility: list[str] | None = None) -> None:
    """Pin a file for each supported CPU, downloaded and sha256-verified by Buck.

    `spec` is a tine.lock.json entry: a `release`, and per-CPU `artifact`, `sha256` and `size`. The
    target's output is the download, named `name`.
    """
    http_file(name = name, executable = executable, visibility = visibility, **_download(spec))

def http_tool(
    name: str,
    spec: dict,
    args: list[str] | None = None,
    path: str | None = None,
    compressed: bool = False,
    directory: bool = False,
    bootstrap: bool = False,
    visibility: list[str] | None = None,
) -> None:
    """Pin a prebuilt program as http.file does, for `buck run` and as an executable dependency.

    The download is the program itself, or an archive that contains it, whose entry in `spec` may have a
    `strip_prefix`. `path` selects a member of an archive; `compressed` decompresses a bare
    zstd-compressed binary, which Buck's http rules cannot unpack themselves. `directory` makes the whole
    unpacked archive the default output, and `path` then names the program that `buck run` runs.
    `bootstrap` marks the pin of the Python that runs tine's drivers, whose archive the host's python3 unpacks.
    """
    if directory and compressed:
        fail("http.tool {}: a compressed download is a single binary, not a directory".format(name))
    archive = path != None or directory
    http_pinned_file(name = name + "-download", spec = spec, executable = not archive)
    strip_prefix = architecture.select({cpu: e.get("strip_prefix") for cpu, e in spec["platforms"].items()}) if archive else None
    untar = None
    if archive:
        untar = "tine//http:host-untar" if bootstrap else "tine//http:untar"
    _tool(
        name = name,
        args = args or [],
        directory = directory,
        path = path,
        src = ":{}-download".format(name),
        strip_prefix = strip_prefix,
        untar = untar,
        unzstd = "tine//http:unzstd" if compressed else None,
        visibility = visibility,
    )

http = struct(
    file = http_pinned_file,
    tool = http_tool,
)
