# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Python bootstrap targets with an automatically generated, isolated ty check."""

load("@prelude//:rules.bzl", "python_bootstrap_binary", "python_bootstrap_library")
load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("//box:runtime.bzl", "BoxInfo", "box_run")

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
    environments = {box.label.name: box[BoxInfo].layers for box in ctx.attrs.boxes}
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
        "boxes": attrs.list(attrs.exec_dep(providers = [BoxInfo]), default = []),
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
        box_run(ctx.attrs.box[BoxInfo], relaxed = True),
        "python3",
        "-B",
        tree.project(ctx.attrs.main.short_path),
    )
    return [DefaultInfo(default_output = tree), RunInfo(args = command)]

_box_python_binary = rule(
    impl = _box_python_binary_impl,
    attrs = {
        "box": attrs.dep(providers = [BoxInfo], doc = "box whose interpreter and packages run it"),
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

python = struct(
    binary = _binary,
    flat_tree = flat_tree,
    library = _library,
    typecheck = ty_check,
)
