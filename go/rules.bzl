# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Build and test a Go project from its own source tree: one online, verified module fetch, then an offline build."""

load("//:specs.bzl", "specs")
load("//box:defs.bzl", "box")
load("//distribution:defs.bzl", "distribution")
load("//platform:architecture.bzl", "architecture")
load("//project:defs.bzl", "project")

_PRIVATE = "__tine"

GoPackageInfo = provider(
    doc = "The test binaries of a go_package, for a go_test to run.",
    fields = {
        "box": provider_field(box.Info),
        "src": provider_field(Artifact),
        # The test binaries and the manifest that build.py writes beside them. It is None when the
        # go_package sets no `test_packages`.
        "tests": provider_field(Artifact | None),
    },
)

GoReleasesInfo = provider(
    doc = "The Go releases that a go.releases target declares, for a go_package to pick its toolchain from.",
    fields = {
        # The committed snapshot, which names one release per minor version.
        "snapshot": provider_field(Artifact),
        # A dynamic value whose GoToolchainsInfo contains the unpacked Go root of each release in the
        # snapshot.
        "toolchains": provider_field(DynamicValue),
    },
)

GoToolchainsInfo = provider(
    doc = "The unpacked Go root of each release in a go.releases snapshot.",
    fields = {
        # The dictionary is keyed by the release as go.dev spells it, such as go1.26.10.
        "toolchains": provider_field(dict[str, Artifact]),
    },
)

def _go_build_impl(
    actions: AnalysisActions,
    bin: OutputArtifact,
    build: RunInfo,
    build_mode: str | None,
    cgo: bool | None,
    cgo_cflags: list[str],
    fetch: RunInfo,
    gocache: OutputArtifact | None,
    goarch: str,
    incremental: bool,
    linker_flags: list[str],
    packages: dict[str, str],
    src: Artifact,
    tags: list[str],
    test_packages: list[str] | None,
    tests: OutputArtifact | None,
    toolchain: Artifact | None,
    toolchains: ResolvedDynamicValue | None,
    workspace: ArtifactValue,
) -> list[Provider]:
    """Declare what the project's go.mod asks for, once it has been found and can be read."""
    module = workspace.read_json()

    # `toolchains` contains every release that the go.releases target declares. Buck downloads a release
    # only when an action reads it, and the actions below read only the release that the workspace
    # action picked.
    if toolchains != None:
        toolchain = toolchains.providers[GoToolchainsInfo].toolchains[module["go_version"]]

    # The fetch is the one online step: go downloads the modules go.mod requires and verifies them
    # against go.sum. The fetch reads only go.mod, go.sum and the go.mod of each module that go.mod
    # replaces with a directory, so an edit to a Go source file does not rerun it. The module cache
    # is kept across reruns, so a dependency bump downloads only the modules missing from it.
    module_cache = None
    if module["sum"] != None:
        module_cache = project.kept_dir(actions, _PRIVATE + "/module-cache")
        actions.run(
            cmd_args(
                fetch,
                specs.args(
                    actions,
                    _PRIVATE + "/go-fetch.spec.json",
                    {
                        "mod": module["mod"],
                        "module_cache_dir": module_cache.as_output(),
                        "modules": {path: src.project(path) for path in module["modules"]},
                        "sum": src.project(module["sum"]),
                        "toolchain": toolchain,
                    },
                ),
            ),
            allow_cache_upload = not incremental,
            category = "go_fetch",
            local_only = True,
            no_outputs_cleanup = incremental,
        )

    actions.run(
        cmd_args(
            build,
            specs.args(
                actions,
                _PRIVATE + "/go-build.spec.json",
                {
                    "bin": bin,
                    "build_mode": build_mode,
                    "cgo": cgo,
                    "cgo_cflags": cgo_cflags,
                    "goarch": goarch,
                    "gocache": gocache,
                    "linker_flags": linker_flags,
                    "module_cache_dir": module_cache,
                    "packages": packages,
                    "root": module["root"],
                    "src": src,
                    "tags": tags,
                    "test_packages": test_packages,
                    "tests": tests,
                    "toolchain": toolchain,
                },
            ),
        ),
        allow_cache_upload = not incremental,
        category = "go_build",
        no_outputs_cleanup = incremental,
    )
    return []

_go_build = dynamic_actions(
    impl = _go_build_impl,
    attrs = {
        "bin": dynattrs.output(),
        "build": dynattrs.value(RunInfo),
        "build_mode": dynattrs.value(str | None),
        "cgo": dynattrs.value(bool | None),
        "cgo_cflags": dynattrs.value(list[str]),
        "fetch": dynattrs.value(RunInfo),
        "goarch": dynattrs.value(str),
        "gocache": dynattrs.option(dynattrs.output()),
        "incremental": dynattrs.value(bool),
        "linker_flags": dynattrs.value(list[str]),
        "packages": dynattrs.value(dict[str, str]),
        "src": dynattrs.value(Artifact),
        "tags": dynattrs.value(list[str]),
        "test_packages": dynattrs.value(list[str] | None),
        "tests": dynattrs.option(dynattrs.output()),
        "toolchain": dynattrs.value(Artifact | None),
        "toolchains": dynattrs.option(dynattrs.dynamic_value()),
        "workspace": dynattrs.artifact_value(),
    },
)

def _go_package_impl(ctx: AnalysisContext) -> list[Provider]:
    src = ctx.attrs.src

    # Most projects hold one program. Its import path is only known once the sources are built, so
    # the binary takes the target's name; the driver refuses a module with more than one candidate.
    packages = ctx.attrs.packages or {ctx.label.name: "./..."}
    bin, outputs = project.binaries_dir(ctx.actions, _PRIVATE + "/bin", packages.keys(), "go_package {}: packages".format(ctx.label.name))

    # go's own build cache. An action's outputs are the only place it may leave state behind, and buck
    # clears them before rerunning it unless told not to. A declared output is also uploaded to the
    # cache, only do that for incremental builds; otherwise build in scratch space.
    gocache = project.kept_dir(ctx.actions, _PRIVATE + "/gocache") if ctx.attrs.incremental else None
    tests = project.kept_dir(ctx.actions, _PRIVATE + "/tests") if ctx.attrs.test_packages != None else None

    # The go_workspace action checks that module_root contains a go.mod. go_workspace runs only after
    # src is built, and building src can mean a git fetch. Checking the form of the path at analysis
    # time reports an empty, absolute or `..` path before src is built.
    module_root = ctx.attrs.module_root
    if module_root != None and (not module_root or module_root.startswith("/") or ".." in module_root.split("/")):
        fail("go_package {}: module_root must be a relative path inside src without '..': {}".format(ctx.label.name, repr(module_root)))

    toolchain = None
    releases = None
    if ctx.attrs.toolchain != None:
        releases = ctx.attrs.toolchain.get(GoReleasesInfo)
        if releases == None:
            toolchain = ctx.attrs.toolchain[DefaultInfo].default_outputs[0]

    build_box = box.layer(ctx, ctx.attrs.box, ctx.attrs.buildroot_packages)
    workspace = ctx.actions.declare_output(_PRIVATE + "/workspace.json")
    ctx.actions.run(
        cmd_args(
            ctx.attrs._workspace[RunInfo],
            specs.args(
                ctx.actions,
                _PRIVATE + "/go-workspace.spec.json",
                {
                    "module_root": module_root,
                    "name": ctx.label.name,
                    "out": workspace.as_output(),
                    "snapshot": releases.snapshot if releases != None else None,
                    "src": src,
                },
            ),
        ),
        allow_cache_upload = True,
        category = "go_workspace",
    )

    ctx.actions.dynamic_output_new(
        _go_build(
            bin = bin.as_output(),
            build = box.run(box = build_box, exe = ctx.attrs._build),
            build_mode = ctx.attrs.build_mode,
            cgo = ctx.attrs.cgo,
            cgo_cflags = ctx.attrs.cgo_cflags,
            fetch = box.run(box = build_box, exe = ctx.attrs._fetch, network = True),
            gocache = gocache.as_output() if gocache != None else None,
            goarch = architecture.spelling(ctx.attrs._arch, "go"),
            incremental = ctx.attrs.incremental,
            linker_flags = ctx.attrs.linker_flags,
            packages = packages,
            src = src,
            tags = ctx.attrs.tags,
            test_packages = ctx.attrs.test_packages,
            tests = tests.as_output() if tests != None else None,
            toolchain = toolchain,
            toolchains = releases.toolchains if releases != None else None,
            workspace = workspace,
        ),
    )
    entry = box.run(build_box, relaxed = True, name = ctx.label.name)
    return project.binaries_providers(outputs, entry) + [GoPackageInfo(box = build_box, src = src, tests = tests)]

_go_package = rule(
    impl = _go_package_impl,
    attrs = {
        # `box` is a dep rather than an exec_dep, so tine//go:build.box builds from the release of the
        # distribution that the binaries are built for.
        "box": attrs.dep(
            providers = [box.Info],
            default = "tine//go:build.box",
            doc = "box to build in, with the Go toolchain unless `toolchain` is set",
        ),
        "build_mode": attrs.option(
            attrs.enum(["exe", "pie"]), default = None, doc = "go's -buildmode for the binaries and tests, the toolchain default when unset"
        ),
        "buildroot_packages": attrs.list(
            attrs.string(),
            default = [],
            doc = "packages to install on top of `box`, such as gcc and the development packages that cgo binds",
        ),
        "cgo": attrs.option(attrs.bool(), default = None, doc = "force cgo on or off, box toolchain default when unset"),
        "cgo_cflags": attrs.list(attrs.string(), default = [], doc = "extra C compiler flags for a cgo build"),
        "incremental": attrs.bool(doc = "keep go's caches across dev-mode rebuilds"),
        "linker_flags": attrs.list(attrs.string(), default = [], doc = "flags for the Go linker, passed as -ldflags"),
        "module_root": attrs.option(attrs.string(), default = None, doc = "directory of the go.mod to build, relative to src; the outermost go.mod when unset"),
        "packages": attrs.dict(attrs.string(), attrs.string(), default = {}, doc = "output name to main package; unset builds the module's only one"),
        "src": attrs.source(allow_directory = True, doc = "the project's source directory, go.mod and go.sum included"),
        "tags": attrs.list(attrs.string(), default = [], doc = "build tags selecting the project's optional files"),
        "test_packages": attrs.option(attrs.list(attrs.string()), default = None, doc = "Go package patterns whose tests the build compiles for a go_test"),
        # The toolchain's go runs inside the box. The toolchain is an exec_dep, so Buck configures it for the
        # box's architecture instead of the architecture of the binaries.
        "toolchain": attrs.option(
            attrs.exec_dep(),
            default = None,
            doc = "a Go root directory, with go in bin/, to build with instead of the box's go, or a go.releases target to build with a release that it picks from the project's .go-version",
        ),
        # `_arch` is the architecture the binaries run on. On a cross build, it differs from the box's.
        "_arch": attrs.string(default = architecture.configured()),
        "_build": attrs.exec_dep(providers = [RunInfo], default = "tine//go:build"),
        "_fetch": attrs.exec_dep(providers = [RunInfo], default = "tine//go:fetch"),
        "_workspace": attrs.exec_dep(providers = [RunInfo], default = "tine//go:workspace"),
    },
    supports_incoming_transition = True,
)

def go_package(
    name: str,
    src: str | None = None,
    dev: bool | None = None,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Build a checked-out Go project against the modules its go.sum pins.

    The source defaults to the checkout named after the target. Its outermost go.mod marks the
    module root unless `module_root` names another. The go.mod is read once the source has been
    built, so a project whose tree arrives from a fetch needs nothing committed here.
    """
    _go_package(
        name = name,
        incremental = project.is_dev(name, source = src, override = dev),
        src = src if src != None else name,
        **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs),
    )

# Keep in sync with GO_RELEASES_LABEL in tools/catalog.py, which refreshes the targets that carry it.
_GO_RELEASES_LABEL = "tine:go-releases"

def go_releases_snapshot_path(name: str) -> str:
    """Where the committed snapshot of a go.releases target lives, relative to its package.

    Keep in sync with `_go_releases_path` in tools/catalog.py, which writes it.
    """
    return "snapshot/go/{}.json".format(name.removesuffix(".releases"))

def _go_toolchains_impl(actions: AnalysisActions, arch: str, snapshot: ArtifactValue, target: str) -> list[Provider]:
    toolchains = {}
    for release, archives in snapshot.read_json().items():
        archive = archives.get(arch)
        if archive == None:
            fail("go.releases {}: the snapshot contains no {} archive for {}".format(target, release, arch))
        download = actions.declare_output(release + ".tar.gz")
        actions.download_file(download.as_output(), archive["url"], sha256 = archive["sha256"], size_bytes = archive["size"])
        root = actions.declare_output(release, dir = True)

        # TODO: Unpack with ctx.actions.extract_archive once the pinned buck2 has it, so that the build
        # doesn't need the host's tar.
        # GNU tar applies --strip-components after it removes a leading slash. With
        # --strip-components=1, a member named go//home/user/.bashrc is written to /home/user/.bashrc.
        # A plain extraction keeps every member inside the output, and the Go root is its go/ directory.
        actions.run(
            cmd_args("/bin/sh", "-c", 'mkdir -p "$1" && tar -xzf "$2" -C "$1"', "sh", root.as_output(), download),
            category = "go_unpack",
            identifier = release,
        )
        toolchains[release] = root.project("go")
    return [GoToolchainsInfo(toolchains = toolchains)]

_go_toolchains = dynamic_actions(
    impl = _go_toolchains_impl,
    attrs = {
        "arch": dynattrs.value(str),
        "snapshot": dynattrs.artifact_value(),
        "target": dynattrs.value(str),
    },
)

def _go_releases_impl(ctx: AnalysisContext) -> list[Provider]:
    # A catalog declares go.releases before refresh-catalog writes its first snapshot. The catalog
    # has to load without the snapshot, because refresh-catalog runs the [snapshot] sub-target to
    # write it.
    snapshot = ctx.attrs.snapshot if ctx.attrs.snapshot != None else ctx.actions.write_json("empty-snapshot.json", {})
    toolchains = ctx.actions.dynamic_output_new(
        _go_toolchains(arch = ctx.attrs._arch, snapshot = snapshot, target = str(ctx.label.raw_target())),
    )
    spec = specs.args(
        ctx.actions,
        "snapshot.spec.json",
        {"current": snapshot, "goarch": {arch: architecture.spelling(arch, "go") for arch in architecture.ARCHITECTURES}},
    )
    run_snapshot = [DefaultInfo(), RunInfo(args = cmd_args(ctx.attrs._snapshot[RunInfo], spec))]

    # The default outputs of go.releases are empty, so building go.releases runs no download.
    return [
        DefaultInfo(sub_targets = {"snapshot": run_snapshot}),
        GoReleasesInfo(snapshot = snapshot, toolchains = toolchains),
    ]

_go_releases = rule(
    impl = _go_releases_impl,
    attrs = {
        "labels": attrs.list(attrs.string(), default = []),
        "snapshot": attrs.option(attrs.source(), default = None, doc = "the committed URL, sha256 and size of each release's archive, per architecture"),
        # The go.releases target is a go_package's exec dependency, so this is the architecture of the box.
        "_arch": attrs.string(default = architecture.configured()),
        "_snapshot": attrs.exec_dep(providers = [RunInfo], default = "tine//go:releases"),
    },
)

def go_releases(name: str, labels: list[str] = [], **kwargs) -> None:
    """Declare the Go releases of the committed snapshot, for a go_package to pick its toolchain from.

    refresh-catalog writes the snapshot to snapshot/go/<name without .releases>.json in the package.
    """
    if not name.endswith(".releases"):
        fail("go.releases name must end with '.releases': {}".format(name))
    committed = glob([go_releases_snapshot_path(name)])
    _go_releases(
        name = name,
        labels = [_GO_RELEASES_LABEL] + labels,
        snapshot = committed[0] if committed else None,
        **kwargs,
    )

def _go_test_impl(ctx: AnalysisContext) -> list[Provider]:
    package = ctx.attrs.package[GoPackageInfo]
    if package.tests == None:
        fail("go_test {}: {} sets no `test_packages`, so its build compiles no tests".format(ctx.label.name, ctx.attrs.package.label))
    command = cmd_args(
        box.run(box = package.box, exe = ctx.attrs._runner),
        specs.args(
            ctx.actions,
            "go-test.spec.json",
            {
                "args": ctx.attrs.args,
                "src": package.src,
                "tests": package.tests,
            },
        ),
    )
    return [
        DefaultInfo(),
        RunInfo(args = command),
        ExternalRunnerTestInfo(
            type = "custom",
            command = [command],
            labels = ctx.attrs.labels,
            # The sandbox chroots, unshares and mounts, so it cannot run on a remote executor.
            default_executor = CommandExecutorConfig(local_enabled = True, remote_enabled = False),
        ),
    ]

_go_test = rule(
    doc = """Run the tests that a go_package's build compiled, by `buck test` inside the package's box.""",
    impl = _go_test_impl,
    attrs = {
        "args": attrs.list(attrs.string(), default = [], doc = "arguments for each test binary, such as -test.short"),
        "labels": attrs.list(attrs.string(), default = [], doc = "passed to the test runner, for `buck test` filtering"),
        "package": attrs.dep(providers = [GoPackageInfo], doc = "the go_package whose tests to run"),
        "_runner": attrs.exec_dep(providers = [RunInfo], default = "tine//go:runner"),
    },
    supports_incoming_transition = True,
)

def go_test(
    name: str,
    distro: str | None = None,
    visibility: list[str] | None = None,
    target_compatible_with: list[str] | Select | None = None,
    **kwargs,
) -> None:
    """Run the tests of a go_package once per distribution of the package, or for `distro` alone."""
    _go_test(name = name, **(distribution.distributed(name, distro, visibility, target_compatible_with) | kwargs))
