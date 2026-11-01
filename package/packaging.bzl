# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The action that checks out the packaging commit that a source checkout pins."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load(":buildroot.bzl", "BuildrootInfo")

def pinned_packaging(
    ctx: AnalysisContext,
    buildroot: BuildrootInfo,
    git: str,
    driver: Dependency,
    pin: Artifact,
    distro: str,
    subdir: str | None = None,
) -> Artifact:
    """Return the tree of the packaging commit that `pin` names for `distro`, or its `subdir` only.

    `git` is the package of the buildroot's release that contains git.
    """
    packaging = ctx.actions.declare_output("__tine/packaging", dir = True)
    ctx.actions.run(
        cmd_args(
            # The tools box of the buildroot contains no git.
            box.run(box = box.layer(ctx, buildroot.box, [git]), exe = driver, network = True),
            # The pin file is the fetch's only input, so an edit elsewhere in the checkout does not rerun the fetch.
            specs.args(ctx.actions, "packaging.spec.json", {"distro": distro, "out": packaging.as_output(), "pin": pin, "subdir": subdir}),
        ),
        allow_cache_upload = True,
        category = "packaging",
        local_only = True,
    )
    return packaging
