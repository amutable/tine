# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public Git API, exported as the `git` namespace."""

load("@prelude//:rules.bzl", "git_fetch")
load("//project:defs.bzl", "project")
load("//project:source.bzl", "SourceTreeInfo")

_MOUNT_TARGET_LABEL = "tine:mount-target"

def _checkout_impl(ctx: AnalysisContext) -> list[Provider]:
    # `src` is a single directory source rather than a glob(). A glob() pattern matches a dotfile only if
    # the pattern itself contains the dot, so a `**` glob would leave out tracked dotfiles.
    # The output of a populated checkout is its directory in the project, and a consumer reads that
    # directory as it is on disk. Archive packages depend on the implicit checkout target, so the output of
    # an unpopulated checkout is an empty directory in buck-out.
    name = ctx.label.name.removesuffix(".git")
    tree = ctx.attrs.src if ctx.attrs.src else ctx.actions.symlinked_dir(name, {})
    return [
        DefaultInfo(
            default_output = tree,
            sub_targets = {path: [DefaultInfo(default_output = tree.project(path))] for path in ctx.attrs.sub_targets},
        ),
        SourceTreeInfo(dev = False, path = tree, sources = None),
    ]

_checkout = rule(
    doc = "Expose a checkout in the current package as a git_fetch-compatible tree.",
    impl = _checkout_impl,
    attrs = {
        "labels": attrs.list(attrs.string(), default = [], doc = "labels used to query this target"),
        "src": attrs.option(attrs.source(allow_directory = True), default = None, doc = "the populated checkout"),
        "sub_targets": attrs.list(attrs.string(), default = [], doc = "tree paths exposed as sub-targets"),
    },
)

def _fetched_impl(ctx: AnalysisContext) -> list[Provider]:
    info = ctx.attrs.fetch[DefaultInfo]
    return [info, SourceTreeInfo(dev = False, path = info.default_outputs[0], sources = None)]

# The prelude's git_fetch() provides only DefaultInfo. rpm.package() takes a source tree only from a target
# that provides SourceTreeInfo, like a checkout does.
_fetched = rule(
    doc = "Expose a git_fetch() as a source tree.",
    impl = _fetched_impl,
    attrs = {
        "fetch": attrs.dep(doc = "the git_fetch() target"),
        "labels": attrs.list(attrs.string(), default = [], doc = "labels used to query this target"),
    },
)

def fetch(name: str, repo: str, rev: str, sub_targets: list[str] = [], visibility: list[str] | None = None, labels: list[str] = [], **kwargs) -> None:
    """Fetch `rev` unless this package contains a non-empty checkout named after the target."""
    directory = name.removesuffix(".git")
    labels = [_MOUNT_TARGET_LABEL] + labels
    if project.populated(directory):
        _checkout(name = name, labels = labels, src = directory, sub_targets = sub_targets, visibility = visibility, **kwargs)
    else:
        # `tine mount` finds checkout slots by the label. Only the public target has it, because tine maps
        # the name of every labeled target to a directory.
        git_fetch(name = name + ".fetch", repo = repo, rev = rev, sub_targets = sub_targets, **kwargs)
        _fetched(name = name, fetch = ":" + name + ".fetch", labels = labels, visibility = visibility)

def checkout(name: str, labels: list[str] = [], **kwargs) -> bool:
    """Expose the directory named after the target as a tree, empty until committed or mounted.

    Returns whether it currently holds anything.
    """
    populated = project.populated(name)
    _checkout(name = name, labels = [_MOUNT_TARGET_LABEL] + labels, src = name if populated else None, **kwargs)
    return populated

git = struct(
    checkout = checkout,
    fetch = fetch,
)
