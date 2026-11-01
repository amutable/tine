# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""An action that creates a directory, to test the umask the launcher runs Buck with."""

load("//box:defs.bzl", "box")

def _umask_probe_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("probe", dir = True)
    ctx.actions.run(
        cmd_args(box.run(box = ctx.attrs._box[box.Info]), "mkdir", "-p", cmd_args(out.as_output(), format = "{}/directory")),
        category = "umask_probe",
    )
    return [DefaultInfo(default_output = out)]

umask_probe = rule(
    impl = _umask_probe_impl,
    attrs = {
        "_box": attrs.exec_dep(providers = [box.Info], default = "tine//tools:dev.box"),
    },
)
