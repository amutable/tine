# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run shell tests inside a box's hermetic sandbox."""

load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//distribution:defs.bzl", "distribution")

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
