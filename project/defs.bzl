# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Source directories, dev mode and build outputs of a project, exported as the `project` namespace."""

load(":source.bzl", "all_files", "source_listing", "source_tree_rule")

_SECTION = "tine"
_DEV = "dev"

def _project_path(package: str, name: str, *, cell: str) -> str:
    root = read_root_config("cells", cell, "")
    return "/".join([part.strip("/") for part in (root, package, name) if part and part != "."])

def is_label(source: str) -> bool:
    """Whether `source` is a target label rather than a directory of this package."""
    return ":" in source

def _source_path(source: str) -> str:
    """Map a source target label to its mounted checkout path, e.g. `cell//pkg:app.git` to `<cell root>/pkg/app`."""
    cell = get_cell_name()
    package = package_name()
    target = source
    if is_label(source):
        path, _, target = source.rpartition(":")
        if "//" in path:
            cell, package = path.split("//", 1)
            cell = cell.removeprefix("@") or get_cell_name()
        elif path:
            package = path
    target = target.split("[", 1)[0].removesuffix(".git")
    return _project_path(package, target, cell = cell) or "."

def is_dev(
    name: str,
    *,
    source: str | None = None,
    override: bool | None = None,
) -> bool:
    """Whether a project or the checkout selected by its source target is configured for dev mode."""
    if override != None:
        return override

    # An unset `[tine] dev` splits into one empty entry, and an empty entry would match an empty project path.
    configured = [entry.strip() for entry in read_root_config(_SECTION, _DEV, "").split(",") if entry.strip()]
    project_path = _project_path(package_name(), name, cell = get_cell_name()) or "."
    if source == None:
        return project_path in configured
    return _source_path(source) in configured

def has_files(directory: str) -> bool:
    """Whether `directory` in this package contains a file that Buck does not ignore."""
    return bool(all_files(directory))

def source_tree(name: str, **kwargs) -> None:
    """Declare `name` as the source tree of this package and of its subpackages, which a build reads in place.

    The default output of `name` lists the files that the tree tracks as inputs.
    """
    directory = _project_path(package_name(), "", cell = get_cell_name()) or "."
    source_tree_rule(name = name, dev = is_dev(""), directory = directory, listing = source_listing(), **kwargs)

def populated(directory: str) -> bool:
    """Whether `directory` in this package contains a file, or is mounted for dev mode."""
    return has_files(directory) or is_dev(directory)

def kept_dir(actions: AnalysisActions, name: str) -> Artifact:
    """Declare a directory output that an action keeps across reruns."""

    # Before each run, Buck copies a kept output with a content-based path back in full. The copied
    # files get fresh timestamps. An incremental build then rebuilds everything. For an output that the
    # driver empties first, the copy is wasted work.
    # The path is not content-based outside dev mode either. If the kind of path changed with dev mode,
    # a switch would leave the output of the old kind at this path. A kept action never removes that
    # output.
    return actions.declare_output(name, dir = True, has_content_based_path = False)

def binaries_dir(actions: AnalysisActions, name: str, binaries: list[str], owner: str) -> (Artifact, dict[str, Artifact]):
    """Declare the kept directory `name` and an output in it for each name in `binaries`.

    `owner` starts a failure message, and names the rule, the target and the attribute of `binaries`.
    """
    for index, binary in enumerate(binaries):
        # A binary name is a path component and a sub-target name. A target pattern such as
        # `:hello[hello-cli]` cannot name a sub-target that contains `:`, a bracket or a space.
        if not binary or binary in [".", ".."] or [c for c in ["/", "\\", ":", "[", "]", " "] if c in binary]:
            fail("{}: invalid output name {}".format(owner, repr(binary)))
        if binary in binaries[:index]:
            fail("{}: duplicate output name {}".format(owner, repr(binary)))

    # The build driver makes the project read-only and needs a writable bind mount for each output
    # directory. One directory for all binaries needs a single mount. A binary name also cannot collide
    # with another output of the rule, because no other output is in that directory.
    bin = kept_dir(actions, name)
    return bin, {binary: bin.project(binary) for binary in binaries}

def binaries_providers(outputs: dict[str, Artifact], box: RunInfo) -> list[Provider]:
    """Return the providers of a rule whose outputs are the binaries in `outputs`.

    `box` enters the box that built them, and is run by the `box` sub-target.
    """

    # `buck run` runs a binary on the host, as a developer would after building it by hand. The box
    # contains no runtime packages, so a dynamically linked binary would not run there either.
    sub_targets = {name: [DefaultInfo(default_output = out), RunInfo(args = cmd_args(out))] for name, out in outputs.items()}
    if "box" in sub_targets:
        fail("a binary named `box` would replace the `box` sub-target")
    sub_targets["box"] = [DefaultInfo(), box]
    providers = [DefaultInfo(default_outputs = outputs.values(), sub_targets = sub_targets)]
    if len(outputs) == 1:
        providers.append(RunInfo(args = cmd_args(outputs.values()[0])))
    return providers

project = struct(
    binaries_dir = binaries_dir,
    binaries_providers = binaries_providers,
    has_files = has_files,
    is_dev = is_dev,
    is_label = is_label,
    kept_dir = kept_dir,
    populated = populated,
    source_listing = source_listing,
    source_tree = source_tree,
)
