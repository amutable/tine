# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Python libraries, binaries and tests with isolated type checks."""

load("@prelude//:rules.bzl", "python_bootstrap_binary", "python_bootstrap_library")
load("@prelude//decls:common.bzl", "buck")
load("@prelude//decls:toolchains_common.bzl", "toolchains_common")
load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("@prelude//python_bootstrap:toolchain.bzl", "PythonBootstrapToolchainInfo")
load("@prelude//test:inject_test_run_info.bzl", "inject_test_run_info")
load("@prelude//toolchains:python.bzl", "python_bootstrap_toolchain")
load("@prelude//utils:selects.bzl", "selects")
load("//box:defs.bzl", "box")

_TYPECHECK_LABEL = "python-typecheck"

def flat_tree(srcs: list[Artifact], deps: list[Dependency]) -> dict[str, Artifact]:
    """The flat namespace a bootstrap target imports in: its own sources over its deps' transitive ones."""
    tree = {}
    for source in [source for dep in deps for source in dep[PythonBootstrapSources].srcs] + srcs:
        # The tree is flat, so two files sharing a name would silently shadow one another.
        if tree.get(source.short_path, source) != source:
            fail("{} and {} are both named {} in the flat tree; rename one".format(tree[source.short_path], source, source.short_path))
        tree[source.short_path] = source
    return tree

def _ty_check_impl(ctx: AnalysisContext) -> list[Provider]:
    # ty walks `.py` only, so an extensionless command has to be named for it to be checked; a source
    # written in the Starlark subset carries its own suffix and is not Python at all.
    check = [source.short_path for source in ctx.attrs.srcs if source.extension in ("", ".py")]
    if not check:
        return [DefaultInfo(default_output = ctx.actions.write("checked", ""))]

    # A dep's sources are here to resolve imports, not to be checked: each is checked by its own
    # target, in the environment that target declares.
    # The isolated tree needs its own configuration so ty never discovers settings from a parent.
    contents = flat_tree(ctx.attrs.srcs, ctx.attrs.deps)
    config = ctx.attrs._config
    if ctx.attrs.pyproject != None:
        config = ctx.actions.declare_output("ty.toml")
        ctx.actions.run(
            cmd_args(ctx.attrs._config_tool[RunInfo], "--base", ctx.attrs._config, "--pyproject", ctx.attrs.pyproject, "--output", config.as_output()),
            category = "ty_config",
        )
    contents["ty.toml"] = config
    tree = ctx.actions.symlinked_dir("tree", contents)

    # A driver's third-party imports resolve inside the box it runs in, so check it once per declared
    # box. Always name an environment, since ty otherwise falls back to the host interpreter and a
    # driver's imports would resolve against whatever the developer happens to have installed.
    environments = {box_dep.label.name: box_dep[box.Info].layers for box_dep in ctx.attrs.boxes}
    outputs = []
    for name, layers in (environments or {"stdlib": []}).items():
        output = ctx.actions.declare_output("checked-" + name)
        command = cmd_args(
            ctx.attrs._ty_check[RunInfo],
            "--ty",
            ctx.attrs._ty[RunInfo],
            "--python",
            ctx.attrs._python[DefaultInfo].default_outputs[0],
            "--project",
            tree,
            "--output",
            output.as_output(),
        )
        for layer in layers:
            command.add("--layer", layer)
        command.add([tree.project(path) for path in check])
        ctx.actions.run(command, category = "ty", identifier = name)
        outputs.append(output)
    return [DefaultInfo(default_outputs = outputs)]

_ty_check = rule(
    impl = _ty_check_impl,
    attrs = {
        "boxes": attrs.list(attrs.exec_dep(providers = [box.Info]), default = []),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = []),
        "labels": attrs.list(attrs.string(), default = []),
        "pyproject": attrs.option(attrs.source(), default = None),
        "srcs": attrs.list(attrs.source()),
        "_config": attrs.source(default = "tine//:ty-config"),
        "_config_tool": attrs.exec_dep(default = "tine//tools:ty-config", providers = [RunInfo]),
        "_python": attrs.exec_dep(default = "tine//tools:python3"),
        "_ty": attrs.exec_dep(default = "tine//tools:ty", providers = [RunInfo]),
        "_ty_check": attrs.exec_dep(default = "tine//tools:ty-check", providers = [RunInfo]),
    },
)

def ty_check(
    name: str,
    srcs: list[str] | Select,
    deps: list[str] | Select | None = None,
    boxes: list[str] | Select | None = None,
) -> None:
    """Check `srcs` in a flat tree of their transitive runtime, in every box named.

    The boxes hang off the check rather than the driver, which would otherwise depend on a box its
    own drivers build.
    """
    manifests = glob(["pyproject.toml"])
    _ty_check(name = name, boxes = boxes, deps = deps, labels = [_TYPECHECK_LABEL], pyproject = manifests[0] if manifests else None, srcs = srcs)

def _library(
    name: str,
    srcs: list[str] | Select,
    deps: list[str] | Select | None = None,
    boxes: list[str] | Select | None = None,
    typecheck: bool = True,
    **kwargs,
) -> None:
    """Declare a flat bootstrap library and the ty check over its own sources."""
    python_bootstrap_library(name = name, srcs = srcs, deps = deps, **kwargs)
    if typecheck:
        ty_check(name = name + "-ty", srcs = srcs, deps = deps, boxes = boxes)

def _box_python_binary_impl(ctx: AnalysisContext) -> list[Provider]:
    tree = ctx.actions.copied_dir("tree", flat_tree([ctx.attrs.main], ctx.attrs.deps))
    command = cmd_args(
        box.run(ctx.attrs.box[box.Info], relaxed = True),
        "python3",
        "-B",
        tree.project(ctx.attrs.main.short_path),
    )
    return [DefaultInfo(default_output = tree), RunInfo(args = command)]

_box_python_binary = rule(
    impl = _box_python_binary_impl,
    attrs = {
        "box": attrs.dep(providers = [box.Info], doc = "box whose interpreter and packages run it"),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = []),
        "main": attrs.source(doc = "the entry point"),
    },
)

def _binary(
    name: str,
    main: str | Select,
    deps: list[str] | Select | None = None,
    box: str | None = None,
    boxes: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare a flat Python binary and the ty check over its entry point.

    The bootstrap interpreter has only the standard library. A binary that imports packages from a box
    passes `box`. The box's interpreter then runs the binary with the host's network, environment and
    files.
    """
    if box == None:
        python_bootstrap_binary(name = name, main = main, deps = deps, **kwargs)
    else:
        if boxes != None:
            fail("python.binary {}: a binary with box is checked in that box, so it takes no boxes".format(name))
        _box_python_binary(name = name, box = box, main = main, deps = deps, **kwargs)
        boxes = [box]
    ty_check(name = name + "-ty", srcs = [main], deps = deps, boxes = boxes)

def _python_test_impl(ctx: AnalysisContext) -> list[Provider]:
    # A test can read files beside __file__, so copy its inputs to keep those reads in the declared tree.
    root = ctx.actions.copied_dir("tree", flat_tree(ctx.attrs.srcs, ctx.attrs.deps))
    if ctx.attrs.box != None:
        # The box mounts the project at a different path, so buck run must pass relative artifact values.
        setenv = {name: cmd_args(value, relative_to = ctx.label.project_root) for name, value in ctx.attrs.env.items()}
        command = cmd_args(box.run(ctx.attrs.box[box.Info], setenv = setenv), "python3")
        env = {}
    else:
        command = cmd_args(ctx.attrs._python_bootstrap_toolchain[PythonBootstrapToolchainInfo].interpreter)
        env = ctx.attrs.env
    command.add("-B", "-m", "unittest", "discover", "-s", root, "-p", "*_test.py")
    return [DefaultInfo(default_output = root)] + inject_test_run_info(
        ctx,
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            env = env,
            labels = ctx.attrs.labels,
            run_from_project_root = True,
            use_project_relative_paths = True,
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False) if ctx.attrs.box != None else None,
        ),
    )

_python_test = rule(
    impl = _python_test_impl,
    attrs = {
        "box": attrs.option(attrs.dep(providers = [box.Info]), default = None),
        "deps": attrs.list(attrs.dep(providers = [PythonBootstrapSources]), default = []),
        "env": attrs.dict(attrs.string(), attrs.arg(), default = {}),
        "labels": attrs.list(attrs.string(), default = []),
        "srcs": attrs.list(attrs.source()),
        "_python_bootstrap_toolchain": toolchains_common.python_bootstrap(),
    }
    | buck.inject_test_env_arg(),
)

def _test(
    name: str,
    srcs: list[str] | Select,
    box: str | Select | None = None,
    deps: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Run unittest discovery with bootstrap Python or a box, and typecheck in the same environment."""
    _python_test(name = name, box = box, srcs = srcs, deps = deps, **kwargs)
    ty_check(name = name + "-ty", srcs = srcs, deps = deps, boxes = selects.apply(box, lambda value: [] if value == None else [value]))

def _host_python() -> list[str] | None:
    """The command of the host's python3 that `tine.use_host_python` selects, or None for the pinned one.

    bin/tine resolves the interpreter into the generated config, with an identity of its build.
    """
    if read_root_config("tine", "use_host_python") != "true":
        return None
    path = read_root_config("tine", "host-python")
    identity = read_root_config("tine", "host-python-identity")
    if path == None or identity == None:
        fail("tine.use_host_python is set, but bin/tine has not resolved the host's python3; run Buck through `tine buck`")

    # -E, -s and -S keep the host's PYTHON* variables, user site directory and sitecustomize out of
    # the build. Python only records an -X option it does not know, so this one puts the
    # interpreter's build on every command line and an upgrade reruns the actions.
    return [path, "-B", "-E", "-s", "-S", "-X", "tine-host-python=" + identity]

def _host_python_toolchain_impl(ctx: AnalysisContext) -> list[Provider]:
    return [DefaultInfo(), PythonBootstrapToolchainInfo(interpreter = cmd_args(ctx.attrs.command))]

_host_python_toolchain = rule(
    impl = _host_python_toolchain_impl,
    attrs = {"command": attrs.list(attrs.string())},
    is_toolchain_rule = True,
)

def _bootstrap_toolchain(name: str, **kwargs) -> None:
    """Declare the bootstrap toolchain on the pinned interpreter, or on the host's `python3` if asked to.

    The pinned interpreter needs a glibc loader at the standard path. A host without one sets
    `tine.use_host_python = true` in the project's config.
    """
    host = _host_python()
    if host == None:
        python_bootstrap_toolchain(name = name, interpreter = read_config("tine", "python_interpreter", "tine//tools:python3"), **kwargs)
    elif read_config("tine", "python_interpreter") != None:
        fail("tine.use_host_python and tine.python_interpreter both choose the bootstrap interpreter; set only one")
    else:
        _host_python_toolchain(name = name, command = host, **kwargs)

def _bootstrap_interpreter() -> str:
    """The path of the interpreter the bootstrap toolchain runs, for an attribute that expands macros.

    ty only reads the pinned interpreter's tree and never runs it, so its checks keep that one.
    """
    host = _host_python()
    return "$(location_exec tine//tools:python3)" if host == None else host[0]

python = struct(
    binary = _binary,
    bootstrap_interpreter = _bootstrap_interpreter,
    bootstrap_toolchain = _bootstrap_toolchain,
    flat_tree = flat_tree,
    library = _library,
    test = _test,
    typecheck = ty_check,
)
