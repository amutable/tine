# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build a Rust project from its own source tree, offline and SBOM-visible."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//platform:architecture.bzl", "architecture")
load("//project:defs.bzl", "project")
load(":vendor.bzl", "VENDOR_ATTRS", "assemble_vendor")

_PRIVATE = "__tine"

def _cargo_build_impl(
    actions: AnalysisActions,
    auditable: cmd_args,
    bin: OutputArtifact,
    binaries: list[str],
    build: RunInfo,
    fetch: RunInfo,
    incremental: bool,
    lock: ArtifactValue,
    src: Artifact,
    target: OutputArtifact | None,
    triple: str,
    vendor: RunInfo,
) -> list[Provider]:
    """Declare everything the lock names, once it has been built and can be read."""
    workspace = lock.read_json()
    repositories = {}
    for commit, fields in workspace["git"].items():
        # The prelude's git_fetch tool, run directly rather than through its rule: that rule hands
        # out the work tree, and cargo resolves a replaced git source against the repository.
        git_dir = actions.declare_output(_PRIVATE + "/git", commit[:12] + ".git", dir = True)
        work_tree = actions.declare_output(_PRIVATE + "/git", commit[:12] + ".work-tree", dir = True)

        # `git init` records an absolute path to the work tree it makes, which breaks when mounting it
        # into a sandbox under a different path. Nothing needs the work tree, so make it a bare repo.
        # Sadly git_fetch() can't do that. That is a second command, and an action runs one, so
        # it needs a shell wrapper.
        actions.run(
            cmd_args(
                "/bin/sh",
                "-c",
                """set -eu
git_dir=$1
shift
"$@" --git-dir="$git_dir"
git --git-dir="$git_dir" config --unset core.worktree
git --git-dir="$git_dir" config --bool core.bare true""",
                "sh",  # $0, which only names the shell in its own diagnostics
                git_dir.as_output(),
                fetch,
                cmd_args(work_tree.as_output(), format = "--work-tree={}"),
                cmd_args(fields["git"], format = "--repo={}"),
                cmd_args(commit, format = "--rev={}"),
            ),
            allow_cache_upload = True,
            category = "git_fetch",
            identifier = commit[:12],
            local_only = True,
        )
        repositories[commit] = {"fields": fields, "repo": git_dir}

    actions.run(
        cmd_args(
            build,
            specs.args(
                actions,
                _PRIVATE + "/cargo-build.spec.json",
                {
                    "auditable": auditable,
                    "bin": bin,
                    "binaries": binaries,
                    "git": repositories,
                    "root": workspace["root"],
                    "src": src,
                    "target": target,
                    "triple": triple,
                    "vendor": assemble_vendor(actions, vendor, workspace["crates"], _PRIVATE),
                },
            ),
        ),
        allow_cache_upload = not incremental,
        category = "cargo_build",
        no_outputs_cleanup = incremental,
    )
    return []

_cargo_build = dynamic_actions(
    impl = _cargo_build_impl,
    attrs = {
        "auditable": dynattrs.value(cmd_args),
        "bin": dynattrs.output(),
        "binaries": dynattrs.value(list[str]),
        "build": dynattrs.value(RunInfo),
        "fetch": dynattrs.value(RunInfo),
        "incremental": dynattrs.value(bool),
        "lock": dynattrs.artifact_value(),
        "src": dynattrs.value(Artifact),
        "target": dynattrs.option(dynattrs.output()),
        "triple": dynattrs.value(str),
        "vendor": dynattrs.value(RunInfo),
    },
)

def _cargo_package_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src
    bin, outputs = project.binaries_dir(ctx.actions, _PRIVATE + "/bin", ctx.attrs.binaries, "cargo_package {}: binaries".format(ctx.label.name))

    # Cargo's own build directory. An action's outputs are the only place it may leave state behind,
    # and buck clears them before rerunning it unless told not to. A declared output is also uploaded to
    # the cache, only do that for incremental builds; otherwise build in scratch space.
    target = project.kept_dir(ctx.actions, _PRIVATE + "/target") if ctx.attrs.incremental else None

    build_box = box.layer(ctx, ctx.attrs.box, ctx.attrs.buildroot_packages)
    resolved = ctx.actions.declare_output(_PRIVATE + "/workspace.json")
    ctx.actions.run(
        cmd_args(
            ctx.attrs._lock[RunInfo],
            specs.args(
                ctx.actions,
                _PRIVATE + "/cargo-lock.spec.json",
                {
                    "name": ctx.label.name,
                    "out": resolved.as_output(),
                    "src": src,
                },
            ),
        ),
        allow_cache_upload = True,
        category = "cargo_lock",
    )

    ctx.actions.dynamic_output_new(
        _cargo_build(
            auditable = specs.executable(ctx.attrs._auditable),
            bin = bin.as_output(),
            binaries = ctx.attrs.binaries,
            build = box.run(box = build_box, exe = ctx.attrs._build),
            fetch = ctx.attrs._fetch[RunInfo],
            incremental = ctx.attrs.incremental,
            lock = resolved,
            src = src,
            target = target.as_output() if target != None else None,
            triple = architecture.spelling(ctx.attrs._arch, "rust"),
            vendor = ctx.attrs._vendor[RunInfo],
        ),
    )
    return project.binaries_providers(outputs, box.run(build_box, relaxed = True, name = ctx.label.name))

_cargo_package = rule(
    impl = _cargo_package_impl,
    attrs = {
        "binaries": attrs.list(attrs.string(), doc = "binaries to take out of the build"),
        # `box` is a dep rather than an exec_dep, so tine//cargo:build.box builds from the release of the
        # distribution that the binaries are built for.
        "box": attrs.dep(providers = [box.Info], default = "tine//cargo:build.box", doc = "box with the Rust toolchain"),
        "buildroot_packages": attrs.list(
            attrs.string(), default = [], doc = "packages to install on top of `box`, such as the development packages the project links against"
        ),
        "incremental": attrs.bool(doc = "keep cargo's build directory across dev-mode rebuilds"),
        "src": attrs.source(allow_directory = True, doc = "the project's source directory, Cargo.lock included"),
        # `_arch` is the architecture the binaries run on. On a cross build, it differs from the box's.
        "_arch": attrs.string(default = architecture.configured()),
        "_auditable": attrs.exec_dep(providers = [RunInfo], default = "tine//tools:cargo-auditable"),
        "_build": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:build"),
        "_fetch": attrs.exec_dep(providers = [RunInfo], default = "prelude//git/tools:git_fetch"),
        "_lock": attrs.exec_dep(providers = [RunInfo], default = "tine//cargo:lock"),
    }
    | VENDOR_ATTRS,
    supports_incoming_transition = True,
)

def cargo_package(
    name: str,
    binaries: list[str],
    src: str | None = None,
    dev: bool | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build a checked-out Rust project against the crates its Cargo.lock pins.

    The source defaults to the checkout named after the target. Its Cargo.lock pins every fetch
    the build needs and is read once the source has been built, so a project whose tree arrives
    from a fetch needs nothing committed here.
    """
    if not binaries:
        fail("cargo_package {}: declare the binaries to take out of the build".format(name))
    directory = src if src != None else name

    # git.fetch() builds `:hello.git` from the directory `hello` of this package when that directory
    # contains files. The check therefore looks for `target/` in `hello` for a label of that form.
    checkout = directory.removeprefix(":").removesuffix(".git") if directory.startswith(":") else directory

    # glob() returns nothing for a directory that Buck ignores. If Buck does not ignore target/, Buck
    # digests cargo's build output as a source of this build. Every local `cargo build` then reruns this
    # build.
    if not project.is_label(checkout) and project.has_files(checkout + "/target"):
        fail(
            "cargo_package {}: {}/target is a source of the build; add `target/` to a .gitignore that covers it".format(
                name,
                checkout,
            ),
        )
    _cargo_package(
        name = name,
        binaries = binaries,
        incremental = project.is_dev(name, source = src, override = dev),
        src = directory,
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )
