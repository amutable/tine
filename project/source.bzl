# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The source tree of a project that has BUCK files in subdirectories.

A directory source cannot contain a subpackage, so each package lists its own files and collects the
listings of its direct subpackages. The pattern follows `prelude/utils/source_listing_impl.bzl`.
"""

load("@prelude//utils:source_listing.bzl", "SourceListingInfo")

# In a glob() pattern, `*` and `**` never match a name that starts with a dot. Only a pattern component
# such as `.*` matches that name. A pattern therefore matches a path only if the path contains exactly as
# many names that start with a dot as the pattern contains such components. all_files() uses two patterns
# for each count from 1 to _DOT_NAMES: one for a path that ends in such a name, and one for the files
# below it.
_DOT_NAMES = 4

# The prelude names its own source listings `source_listing`. A different name lets a package contain both.
_LISTING = "source_tree_listing"

def all_files(directory: str = "") -> list[str]:
    """Return the files below `directory` in this package with at most _DOT_NAMES names in their path that start with a dot."""
    prefix = directory + "/" if directory else ""
    patterns = []
    for count in range(_DOT_NAMES + 1):
        pattern = "**" + "".join(["/.*" if index == 0 else "/**/.*" for index in range(count)])
        patterns += [prefix + pattern] + ([prefix + pattern + "/**"] if count else [])
    return glob(patterns)

def _source_listing_impl(ctx: AnalysisContext) -> list[Provider]:
    prefix = ctx.label.package + "/" if ctx.label.package else ""
    sources = {}
    for dep in ctx.attrs.deps:
        subpackage = dep.label.package.removeprefix(prefix)
        sources.update({subpackage + "/" + path: artifact for path, artifact in dep[SourceListingInfo].sources.items()})
    for source in ctx.attrs.srcs:
        sources[source.short_path] = source
    return [DefaultInfo(), SourceListingInfo(sources = sources)]

_source_listing = rule(
    impl = _source_listing_impl,
    attrs = {
        "deps": attrs.list(attrs.dep(providers = [SourceListingInfo]), doc = "the listings of the direct subpackages"),
        "srcs": attrs.list(attrs.source(), doc = "the files of the package"),
    },
)

def source_listing() -> str:
    """List the files of this package and of its subpackages as the target `:source_tree_listing`.

    Every BUCK file below a `source_tree()` must call this. The listing of the parent package depends on the
    listing of each subpackage, so a BUCK file without the call fails the build. Returns the label of the
    listing.
    """
    prefix = package_name() + "/" if package_name() else ""

    # tine writes the version of the project, its mounts and the address of the cache into
    # `.buckconfig.local`. As an input, the file would give a build different cache keys on different
    # machines.
    srcs = [path for path in all_files() if path != ".buckconfig.local" or package_name()]
    _source_listing(
        name = _LISTING,
        srcs = srcs,
        deps = ["//{}{}:{}".format(prefix, subpackage, _LISTING) for subpackage in __internal__.sub_packages()],
        visibility = ["PUBLIC"],
    )
    return ":" + _LISTING

SourceTreeInfo = provider(
    doc = "A source tree that a build reads in place.",
    fields = {
        # `dev` is True when `[tine] dev` lists the directory that a build of the tree reads.
        "dev": provider_field(bool),
        # `path` is the directory of a checkout, or the project path of a package directory with its listed files
        # as hidden inputs.
        "path": provider_field(Artifact | cmd_args),
        # `sources` maps the path of each listed file in the tree to the file, or is None for a checkout.
        "sources": provider_field(dict[str, Artifact] | None),
    },
)

def _source_tree_impl(ctx: AnalysisContext) -> list[Provider]:
    sources = ctx.attrs.listing[SourceListingInfo].sources
    listing = ctx.actions.write(ctx.label.name + ".txt", "".join([path + "\n" for path in sorted(sources)]))

    # A build reads the package directory in place. The listed files are hidden inputs, so that an edit to
    # one of them reruns the build.
    path = cmd_args(ctx.attrs.directory, hidden = sources.values())
    return [DefaultInfo(default_output = listing), SourceTreeInfo(dev = ctx.attrs.dev, path = path, sources = sources)]

source_tree_rule = rule(
    impl = _source_tree_impl,
    attrs = {
        "dev": attrs.bool(doc = "whether `[tine] dev` lists the project path of the package"),
        "directory": attrs.string(doc = "the project path of the package"),
        "listing": attrs.dep(providers = [SourceListingInfo]),
    },
)
