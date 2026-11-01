# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Turn a loaded Cargo.lock's crate list into the vendored tree an offline build reads."""

load("//:specs.bzl", "specs")

# The attributes one crate-graph vendoring costs a rule. `assemble_vendor` names its outputs
# unconditionally, so a rule may call it once.
VENDOR_ATTRS = {
    "_vendor": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:vendor"),
}

def assemble_vendor(actions: AnalysisActions, tool: RunInfo, crates: list[dict[str, str]], root: str) -> Artifact:
    """Declare the vendored crate tree one project's Cargo.lock pins.

    The lock's checksum is the tarball's, so buck verifies each download itself. Git dependencies
    never enter this tree (see build.py).
    """
    crate_files = {}
    for crate in crates:
        name = "{}-{}.crate".format(crate["name"], crate["version"])

        # These per-download names must stay singular: the plural ones are the directories
        # collecting them.
        artifact = actions.declare_output(root + "/crate", name, has_content_based_path = True)
        actions.download_file(artifact, crate["url"], sha256 = crate["sha256"])
        crate_files[name] = artifact

    vendor = actions.declare_output(root + "/vendor", dir = True)
    actions.run(
        cmd_args(
            tool,
            specs.args(
                actions,
                root + "/cargo-vendor.spec.json",
                {
                    "crates": actions.copied_dir(root + "/crates", crate_files),
                    "out": vendor.as_output(),
                },
            ),
        ),
        allow_cache_upload = True,
        category = "cargo_vendor",
    )
    return vendor
