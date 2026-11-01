# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run a package's tests inside a box's hermetic sandbox."""

load("@prelude//python_bootstrap:python_bootstrap.bzl", "PythonBootstrapSources")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//distribution:defs.bzl", "distribution")
load("//python:defs.bzl", "python")

def _box_python_test_impl(ctx: AnalysisContext) -> list[Provider]:
    # Copied, not symlinked: a test reaches the module under test through its own __file__, which
    # must not escape into the source checkout behind buck's back.
    root = ctx.actions.copied_dir("tree", python.flat_tree(ctx.attrs.srcs, ctx.attrs.deps))

    # -B keeps __pycache__ out of the tree; discover's top-level dir defaults to the start dir.
    command = cmd_args(
        box_run(box = ctx.attrs.box[BoxInfo], setenv = ctx.attrs.env),
        "python3",
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        root,
        "-p",
        "*_test.py",
    )
    return [
        # The tree so a failure can be reproduced by hand, and RunInfo so `buck run` on a suite
        # works; every prelude test rule exposes the latter through inject_test_run_info.
        DefaultInfo(default_output = root),
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            labels = ctx.attrs.labels,
            # The sandbox chroots, unshares and mounts, so it cannot be shipped to a remote
            # executor. tine's own execution platform is local-only, but a consuming project's
            # need not be.
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

_box_python_test = rule(
    doc = """One package's `*_test.py` files, run by `buck test` in a flat tree inside a box.

    Same-package sources go in `srcs`; anything from another package comes in through `deps` on a
    `python.library`. Both land beside the tests, so ordinary Python modules are imported
    directly; a test uses `Path(__file__).parent` only for non-module inputs such as `.bzl` files
    written in the Python subset and extensionless commands such as `bin/tine`.
    """,
    impl = _box_python_test_impl,
    attrs = {
        "box": attrs.dep(providers = [BoxInfo], doc = "box whose hermetic sandbox runs the tests"),
        "deps": attrs.list(
            attrs.dep(providers = [PythonBootstrapSources]),
            default = [],
            doc = "sources from other packages",
        ),
        "env": attrs.dict(
            attrs.string(),
            attrs.arg(),
            default = {},
            doc = "environment for the tests; a value may name an artifact with `$(location //target)`",
        ),
        "labels": attrs.list(attrs.string(), default = [], doc = "passed to the test runner, for `buck test` filtering"),
        "srcs": attrs.list(attrs.source(), doc = "the tests and the sources they exercise"),
    },
)

def box_python_test(
    name: str,
    box: str,
    srcs: list[str] | Select,
    deps: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare a box-hosted Python suite and the ty check over its sources, in the box that runs them."""
    _box_python_test(name = name, box = box, deps = deps, srcs = srcs, **kwargs)
    python.typecheck(name = name + "-ty", srcs = srcs, deps = deps, boxes = [box])

def _box_sh_test_impl(ctx: AnalysisContext) -> list[Provider]:
    if (ctx.attrs.script == None) == (ctx.attrs.test == None):
        fail("box_sh_test: exactly one of script and test is required")

    # An inline script gets the strict mode a committed one sets for itself, so a one-liner does
    # not have to remember it to fail the way every other assertion does.
    test = ctx.attrs.test or ctx.actions.write("test.sh", ["set -euo pipefail", ctx.attrs.script])

    # The sandbox binds the project and works there, so an artifact argument reaches the script as
    # the same project-relative path a build would name.
    command = cmd_args(
        box_run(box = ctx.attrs.box[BoxInfo]),
        "bash",
        test,
        ctx.attrs.args,
    )
    return [
        DefaultInfo(default_output = test),
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            labels = ctx.attrs.labels,
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

_box_sh_test = rule(
    doc = """One shell script asserting something about what a build produced, run inside a box.

    The shell is either a committed script (`test`) or written inline (`script`), and is handed
    `args`, which name build artifacts with `$(location //target)` exactly as an image operation
    does. Artifacts arrive that way rather than through the shell, so an inline script stays shell
    and needs no escaping. Running it in a box rather than on the host is what makes the tools
    it reaches for the pinned ones, so an assertion cannot pass or fail on what a developer happens
    to have installed.

    `box` builds for the distribution of the test. A box whose release follows the distribution, such
    as `tine//tools:dev.box`, then builds from the release of the image that the test checks.
    """,
    impl = _box_sh_test_impl,
    attrs = {
        "args": attrs.list(attrs.arg(), default = [], doc = "arguments to the script, artifacts included"),
        "box": attrs.dep(providers = [BoxInfo], doc = "box whose hermetic sandbox runs the script"),
        "labels": attrs.list(attrs.string(), default = [], doc = "passed to the test runner, for `buck test` filtering"),
        "script": attrs.option(
            attrs.string(),
            default = None,
            doc = "the shell to run, for an assertion a file of its own would only bury",
        ),
        "test": attrs.option(attrs.source(), default = None, doc = "the script to run"),
    },
    supports_incoming_transition = True,
)

def box_sh_test(
    name: str,
    distro: str | None = None,
    distributed: bool = True,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Declare a shell test once per distribution of the package, or for `distro` alone.

    A test of an output that is the same for every distribution, such as a box of a fixed release,
    passes `distributed = False`. Otherwise the test runs once per distribution, and each run builds
    the output again.
    """
    if not distributed:
        if distro != None:
            fail("box_sh_test {}: a test with distributed = False takes no distro".format(name))
        _box_sh_test(name = name, visibility = visibility, target_compatible_with = target_compatible_with, **kwargs)
        return
    _box_sh_test(name = name, **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs))
