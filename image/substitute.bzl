# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Expand a template a project ships into a file an image installs."""

load("//:specs.bzl", "specs")

def _substitute_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.label.name)
    ctx.actions.run(
        cmd_args(
            ctx.attrs._substitute[RunInfo],
            specs.args(
                ctx.actions,
                "substitute.spec.json",
                {
                    "name": ctx.label.name,
                    "out": out.as_output(),
                    "replacements": ctx.attrs.replacements,
                    "src": ctx.attrs.src,
                },
            ),
        ),
        category = "substitute",
    )
    return [DefaultInfo(default_output = out)]

_substitute = rule(
    impl = _substitute_impl,
    attrs = {
        "replacements": attrs.dict(attrs.string(), attrs.string(), doc = "placeholder -> what to put in its place"),
        "src": attrs.source(doc = "the template"),
        "_substitute": attrs.exec_dep(providers = [RunInfo], default = "tine//image:substitute"),
    },
)

def substitute(name: str, src: str, replacements: dict[str, str], **kwargs) -> None:
    """Write `src` with its placeholders expanded, as a file named after the target.

    Each placeholder has to appear in the template, so a project renaming one fails the build rather
    than leaving a marker in an installed file.
    """
    if not replacements:
        fail("substitute {}: declare the placeholders to expand".format(name))
    _substitute(name = name, src = src, replacements = replacements, **kwargs)
