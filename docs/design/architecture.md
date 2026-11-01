<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# tine architecture

This document describes the architecture implemented in this repository, the decisions that shaped it,
and the work that remains. It is a living architecture document, not a chronological implementation plan.

Statements under **Current architecture** describe code that exists. **Roadmap** describes accepted or
possible future work and labels open questions explicitly. When implementation and this document disagree,
the implementation is authoritative and this document should be corrected.

Anything specific to one native package system belongs to that system's own section. The rest of this
document describes machinery that does not know which system it is driving, and names none.

User-facing guides live in [docs/user](../user): [images.md](../user/images.md) covers building and
running images, and [importer.md](../user/importer.md) covers maintaining packages with the importer.

## Purpose and scope

tine uses Buck2 to build native packages and compose operating-system images. The long-term goal is a
monorepo in which a useful core package set is rebuilt from source, scheduled in dependency order, cached
by content, and suitable for remote execution. The current implementation already provides:

- three native package systems, each with pinned repositories and a repository-owned package artifact pool;
- bootstrap box roots containing the pinned userspace used by build actions;
- configured package managers and shared buildroots for several OS releases;
- package builds from imported source metadata, including self-hosted buildroot dependencies;
- layered filesystem images, deterministic archives, bootable GPT disks, and a VM runner.

The system does not yet claim complete source provenance, remote execution, or a full release pipeline.
Images currently mix locally built packages with pinned upstream packages.

## Current architecture

### Repository and cell layout

This repository is the `tine` cell; a consuming project points a `tine` cell at a checkout of it and adds
its own package tree:

```text
//packages/               (consuming project) independently versioned package sources and BUCK files
//examples/image-local-packages/ (consuming project) bootable image from self-built packages
tine//examples/image/     image smoke targets, built for each distribution
tine//examples/box/       pinned interactive development environment
tine//distribution/       the axis an image's distribution is selected on
tine//package/            package-system-neutral providers and installation flow
tine//package_system/     one directory per package system: its repository, resolver, installer,
                          extractor, and optional indexer and builder
tine//box/                box bootstrap and sandbox command construction
tine//rootfs/             bind/overlay mounting and stored-delta translation
tine//image/              layers, compositions, archive, directory, disk and boot artifact rules and
                          drivers, and VM runners
tine//git/                pinned Git repositories with local checkout overrides
tine//cargo/              vendored crate trees and offline Rust source builds
tine//go/                 go.sum-verified module fetches and offline Go source builds
tine//http/               prebuilt tools that Buck downloads and verifies
catalog//                 default repositories, locks, releases, package managers, and buildroots
tine//tools/              pinned development and catalog-refresh commands, and the boxes they run in
tine//bin/                the command a checkout builds through, which fetches and configures Buck2
```

The default catalog is the `catalog` cell, in the `catalog/` directory of the tine checkout. It declares
the release selection, mirrors, box choice, repository additions, and policy overrides. A project replaces
it by declaring its own `catalog` cell, built with tine's family macros or low-level rules. Every
`catalog//` label then resolves to the project's catalog, including the labels in tine's own targets, and
the build does not use tine's catalog. Buck reads cells only from the root cell's configuration. `bin/tine`
therefore writes the fallback cell into the project's generated `.buckconfig`, unless `tine.toml` declares
the cell. The project's `//buildroots` package maps importer-generated `//buildroots/<family>:<release>`
names to catalog targets.

Catalog targets use `<family>.<release>[.<component>].<role>` names. A rolling channel occupies the release
segment like any other release. Singular `.repository` targets own remotes, plural `.repositories` targets
define universes, and release, box, package-manager, and buildroot targets use their corresponding
suffixes. Low-level declaration macros require the suffix appropriate to their role. The family catalog
macros instead take a `<family>.<release>` prefix and declare the complete repository, universe, release,
package-manager, and buildroot bundle. A box's identity describes its provenance rather than every
release that may consume it.

The package source tree lives in the OS.git repository (which consumes this `tine` cell) under `packages/`.
It is intentionally not part of the reusable `tine` cell: package policy and imported source data change
independently of build machinery.

Buck2 looks every rule's toolchain up in a cell named `toolchains`. tine declares the only one it needs,
the bootstrap Python interpreter, in this cell's root package and aliases `toolchains` to `tine`;
`tine init` writes that alias into the consuming project's generated `.buckconfig`, so no project
declares a toolchain of its own. The obvious shape, a `toolchains/` cell in this repository, is
impossible: Buck2 forbids a nested cell inside an external cell, so every project consuming tine as one
needed a copy of that directory. An alias resolves through an external cell where a nested cell cannot
exist. A project that does need toolchains beyond the bootstrap one declares the cell itself, dropping
the alias and forwarding what it does not declare:

```Starlark
toolchain_alias(name = "python_bootstrap", actual = "tine//:python_bootstrap", visibility = ["PUBLIC"])
```

### Building with out-of-tree checkouts

A cell root must be project-relative, so Buck cannot point a cell directly at an external directory.
`tine mount` records local overrides in `.buck/tine-mounts.toml`, mapping project-relative targets to
absolute source directories. Targets are cell roots or local-checkout slots declared by `git_fetch()`.
They cannot overlap or cover `.buck/` or `.buckconfig.d/`: nested mounts would depend on application
order, and those directories hold the mount table and configuration layers. Invalid declarations stop
the command rather than silently falling back to the checked-in directory.

For example, `tine mount` can make `/work/lib` appear at `vendor/lib` inside a project. tine sets up the
bind mounts in a private mount namespace before starting the Buck client, so uncommitted source edits
are visible without changing Buck's project-relative paths.

Before running Buck, tine selects the configured tine cell's `bin/tine`, whether or not any mounts were
needed. If its path differs from the current wrapper after resolving symlinks, tine re-executes it so
the command, rules, and Buck2 pin come from the same checkout. After creating mounts, tine always
re-executes: a mount may have replaced the wrapper at the same path. A configured cell without
`bin/tine` is an error, not a reason to keep running another checkout's wrapper.

#### Carrying the mount table

Every `tine buck` command runs Buck's daemon as a thread of its own process, with `--no-buckd`. The
daemon starts inside the namespace the command just created and dies with it, so a command always builds
with the mounts it declared, and no daemon survives to serve a later command an old copy of any setting.

What remains to carry is the mount table itself. A nested Buck inside a run target inherits the
namespace, and it must use the mounts as they are rather than reread declarations that another command
may have changed since. tine records the mounted project paths and the saved Git metadata locations in
the `TINE_MOUNTS` environment variable. The variable travels by process inheritance, exactly like
membership in the namespace itself, so it reaches precisely the processes that are in the namespace it
describes, and dies with them. A value that does not decode is an error, not a reason to quietly build
the namespace again: the holder is already mounted, and remounting would reread declarations that the
holding command deliberately froze.

Re-executing inside an existing mount namespace preserves both the namespace and its recorded list of
mounted paths. tine does not reread declarations that another command may already have changed. Git
ignores are read through the mounted paths too, so they describe the checkout the build will use. The
ordinary `.buckconfig` and `.buckconfig.local` files stay shared rather than being frozen with the
mounts. They still follow the refresh rules described under
[Shared configuration and nested commands](#shared-configuration-and-nested-commands).

#### Git metadata in mounted checkouts

Submodules and linked worktrees can use a `.git` file that points to metadata outside the checkout. For
example, `/work/main/lib/.git` might contain `gitdir: ../.git/modules/lib`, referring to
`/work/main/.git/modules/lib`. After mounting that checkout at `vendor/lib`, the same pointer would look
under `vendor/.git/modules/lib` instead.

Before mounting, tine resolves each gitfile with `git rev-parse --absolute-git-dir` and records the
result in the `TINE_MOUNTS` variable, as a map from mount targets to absolute metadata paths. Both
relative and absolute gitfile pointers are recorded. Ordinary `.git` directories remain accessible
through the bind mount and need no saved path. Neither directories nor gitfiles are rewritten.

Ignore queries run from the mounted checkout with `--git-dir=<saved path>` and `--work-tree=.`. The
explicit worktree matters because a submodule's `core.worktree` can still name its original source path.
If that path now holds a replacement checkout, following it would read the wrong `.gitignore`. Giving
Git both paths keeps the query on the mounted tree while retaining the saved metadata's `info/exclude`
rules and tracked-file index, so tracked files are not mistaken for ignored build output.

During configuration refresh, tine reads the map once from the variable and passes it to both the
project and tine-cell ignore queries, keyed by mounted checkout paths. With no mounts, it skips this
read.

#### Lock scope

A `flock()` on `.buck/tine-mount.lock` protects edits to the mount table, covering the read, validation,
and atomic replacement. Validation can run a nested `tine buck uquery` to discover new mount targets.
Buck commands take no mount lock: otherwise that query would wait for the mount command that is waiting
for the query.

A build can read either the old or new table, but cannot see a partly written one. Replacing a source
directory does not have to go through `tine mount`, so locking mount-table edits cannot prevent the
source-replacement case above.

#### Shared configuration and nested commands

For a consuming project, an ordinary `tine buck` command regenerates the entire project `.buckconfig`
from the selected tine checkout's `.buckconfig`, applying `[buckconfig.*]` overrides from `tine.toml`
and `tine.local.toml`. Mount setup and wrapper selection happen first, so the same checkout supplies
both the wrapper and its defaults. Direct edits to the generated defaults are discarded; project
settings belong in TOML instead.

Cell selection uses the effective configuration, including `.buckconfig.local`. A local cell override
can select a different wrapper and defaults without being copied into the generated `.buckconfig`.
If TOML does not declare the tine cell, existing cell registrations in `.buckconfig` are retained while
other defaults are regenerated. This also supports consuming projects that declare their cells only
in native Buck configuration.

When the tine cell is the project root itself, it is a standalone checkout. Its `.buckconfig` is source
configuration and is not regenerated; put Buck overrides directly in that file rather than in TOML.

The `.buckconfig.local` update only replaces tine's generated block, which contains project ignores,
Git-derived image version components and, while mounts are declared, the dev targets. Text outside that
block is preserved.
Configured ignores are merged with VCS metadata exclusions and Git ignores; a project-owned
`[project] ignore` in `.buckconfig.local` is rejected because it would replace the generated list.
`[tine] dev` lists the project paths that `project.is_dev()` builds in dev mode. The block writes the
project's own entries followed by every mounted target, so a mount selects dev mode without configuration.
Without mounts the block leaves the key out. While mounts are declared, a `[tine] dev` in `.buckconfig.local`
is rejected. The block comes first in that file, and a later entry would replace the merged value. A tool
that runs Buck directly under a build reads the same file and sees the same dev targets. A `-c` flag on the
outer command would not reach such a tool.
The version components live under `[tine]` as `version-base`, `version-count`, `version-height`,
`version-commit`, and, for uncommitted work, `version-dirty`. Each image renders those components against
its own label budget; see "Image versioning" in [images.md](../user/images.md).

Generated settings go into a file rather than command-line flags because Buck reads project ignores
from configuration files at startup, without command-line overrides. A file also
avoids the 128 KiB limit on a single argument, and `buck2 complete` accepts no configuration flags.
Target completion and `tine completion` select the configured wrapper without refreshing shared
configuration. Target completion uses the selected checkout's cached Buck2 without downloading. tine
rewrites Buck2's completion script so target queries run through `tine buck` too.

The selected tine checkout pins Buck2 in `tine.lock.json`. Projects can override the pin in the
`[buck2]` table of `tine.toml` or `tine.local.toml`, with per-platform fields under
`[buck2.platforms.<platform>]`.

tine exports the pinned binary's path as `BUCK2_BINARY`. A tool running under a build never invokes the
`tine` wrapper, but directly calls the exported binary (`util.buck2_argv()`), inherits the current
mounts, and runs against the shared configuration the invoking command already wrote.

### Component model

The package model separates identity and policy from the exact inputs used by an action:

```text
PackageSystemInfo (one package system's drivers)
        │
        ├── PackageRepositoryInfo ──┐
        │                           ├── RepositoryUniverseInfo
        │                           │          │
        └───────────────────────────┴── OsReleaseInfo
                                               ├── box transaction ── BoxInfo ──┬── image tooling
                                               │                                │
                                               │                                ├── image installation
                                               └── PackageManagerInfo ──────────┤
                                                                                └── BuildrootInfo
                                                                                       └── package build
```

The providers have deliberately narrow roles:

- `PackageSystemInfo` bundles the drivers for one native binary-package ecosystem: snapshot, extract,
  install, package database capture, plan, and optionally repository indexing and build, plus the
  paths that database occupies in an installed root, the file suffix a selected package is named with,
  and whether its planner reuses prebuilt repository metadata. The builder and the indexer are
  optional, and a system whose repository metadata is already what its resolver reads declines the
  cache instead of leaving it on. A package build declared against a buildroot of another package
  system is refused by name, and so is a local repository of a system with no indexer.
- `PackageRepositoryInfo` represents one repository and binds it to a package system. Its target name is
  the repository ID; remote declarations also expose their pinned directory and base URL. Priority is
  configuration policy, not an intrinsic repository property. A repository is not inherently owned by an
  OS release.
- `LocalPackageRepositoryInfo` identifies a repository assembled from package artifacts in the build graph.
  It lists package directories, not the box that produced them; a consumer generates its repository
  metadata with its own box.
- `RepositoryUniverseInfo` defines one homogeneous solve universe: required repositories, named optional
  groups, and groups enabled by default. Selection preserves declaration order, de-duplicates identical
  targets, and rejects conflicting repository IDs.
- `OsReleaseInfo` associates named native package sets with one repository universe and supplies the base
  of a box. Its target label carries the release identity.
- `PackageManagerInfo` is an immutable solve environment without a box. It lists its repositories in
  order with their effective priorities, and names its package system and its release's package sets. A
  consumer, such as an image or a buildroot, configures the repositories for the box it runs in. Each
  `ConfiguredPackageRepositoryInfo` record then contains the repository ID, materialized directory,
  effective priority, base URL, verifier and optional declaration dependency. A derived manager
  inherits the repositories and can add repositories without repeating inherited release policy.
- `LocalPackageUniverseInfo` describes a universe of locally built packages together with the imported
  runtime Requires/Provides metadata needed to select an install request's closure among them.
- `BuildrootInfo` materializes the shared base root from explicit packages or a release package set,
  and names the box that installed it. A package build on the root runs in that box.
- `BoxInfo` contains a runnable root filesystem as a stack of layers, and the sandbox used to enter it.
  Its target label establishes provenance.

Solver caches, local repository indexes and the keyrings that images and buildroots verify with are anonymous
targets. Each is keyed by its box and package system, and by its inputs: a solver cache by its repository and
architecture, an index by its package directories, and a keyring by its keys and the time they are checked
at. Consumers that run in the same box therefore share one artifact, while distinct solver contexts remain
isolated.

This split is visible in the default catalog. Several releases of one family are separate OS releases whose
package managers solve against their own repositories, and a release may model
some of its repositories as required, some as a default group, and some as an optional group a package
manager enables. A family macro declares that whole standard target bundle and its package sets while
accepting overrides for mirrors, repositories, priorities, and package policy. Buildroots resolve the
release's `buildroot` package set rather than duplicating native package names in the buildroot
declaration.

An initial image normally fixes one package manager for the lifetime of the logical image, and every derived
image and terminal output inherits it. An initial image runs in the tools box, as "Box bootstrap" describes,
and a derived image runs in the box of its parent. An image without a package manager cannot install native
packages. A package target names a buildroot because its shared base root, not OS identity alone, is its
relevant input.

### Catalog pinning and refresh

Normal builds do not resolve against live network repositories. The catalog contains one required and three
optional generated forms:

- `snapshot/repo/<name>.<architecture>.json` pins one repository's build metadata for one of the
  architectures its mirror serves, and that architecture's complete package inventory,
  keyed by SHA-256 checksum. The metadata is files named by where each lands in the materialized
  repository: some fetched against a stated checksum, some carried verbatim where the pin narrowed what
  the mirror served. What goes in them belongs to the package system; placing them does not. Where the
  mirror itself names metadata by content, the snapshot pins each stream by its own checksum and drops
  the ones the resolver will not read; where it does not, the snapshot pins the bytes the refresh saw,
  which stays buildable only against a mirror that serves immutable snapshots and not against an
  ordinary rolling one;
- `snapshot/box/<name>.<architecture>.json` optionally freezes a box transaction for one architecture.
  Remote records contain
  `{source, repo, pkg_checksum, package_id, url, size}`: the checksum verifies the bytes, while `url` and
  `size` record the last known transport after rolling repository metadata stops advertising that package.
  The target's `.repository` or `.box` suffix is not repeated in the snapshot filename;
- `snapshot/key/<FINGERPRINT>.key` holds one signing key a repository declares. The declaration in the
  catalog's BUCK names the key by fingerprint, which is what a reviewer checks against the distribution's
  published one, together with the URL to fetch it from. `refresh-catalog` fetches a missing key file
  once and never updates it: a key is immutable by fingerprint, and whether the file is the declared key
  is checked by the build with the package system's own tools, not by the refresh;
- `snapshot/go/<name>.json` pins one Go release for every minor version from Go 1.21 on. It contains the
  URL, sha256 and size of each release's archive per architecture, as go.dev's release list publishes them.
  `go.releases` reads the file, so its declaration names no release. Go keeps publishing patch releases of
  an older minor version after a newer minor version is out, so `--advance` moves each minor version to its
  newest patch release.

A box with a resolver box and no committed transaction resolves through that predecessor as a normal
cacheable build action. The generated transaction is an input to the existing dynamic package selectors,
so the box builds in one invocation without mutating the source tree. Its package selection changes only
when its authored policy, resolver box, or pinned repository inputs change.

`tine buck run tine//tools:refresh-catalog` refreshes the `catalog//` cell in several
phases. Pass another package of repositories and boxes after `--`, for example
`tine buck run tine//tools:refresh-catalog -- my_project//boxes`:

0. With `--advance`, advance every repository pinned to a mirror that publishes snapshots, by rewriting
   the pin in the declaration. A remote repository rule owns its own pin arguments and carries them as metadata under a
   namespace it owns, so a package system joins this phase by declaring a pin. How the newest snapshot is
   found is the mirror's business: one enumerates its snapshots through a gateway, while another publishes
   a tree per day and exposes no index at all, but does record when it last finished one, which is the
   only thing distinguishing a complete day from a half-written one. Repositories sharing one pin
   advance together, because a release's repositories are only guaranteed to solve together when they come
   from the same snapshot, and a pin never moves backwards. A release macro forwards its own pin arguments
   to the repositories it owns, and overriding a release's mirrors is all or nothing for the same reason.
   Advancing is scoped to the repositories the same run is about to re-snapshot: a base URL from one
   snapshot composing package locations pinned in another builds nothing. `verify-catalog` skips this
   phase and checks the committed pins.
1. Fetch every signing key the selected repositories declare and the catalog does not hold yet, on the
   host. `verify-catalog` reports a missing one.
2. Run every remote repository's `[snapshot.<architecture>]` sub-target on the host, once per
   architecture its mirror serves. The snapshot driver downloads and verifies repository metadata, drops
   unused streams, validates package locations, and writes deterministic, pure snapshot JSON. It does not
   carry packages forward from an earlier snapshot. It only reads metadata, so any host runs it. Every
   `go.releases` target's `[snapshot]` sub-target runs in the same phase, unless `--box` selects boxes
   without `--go`. It snapshots the releases in the committed snapshot, and with `--advance` the newest
   patch release of every minor version from Go 1.21 on instead. `--go` alone refreshes only the
   `go.releases` targets, so their advance needs no repository advance.
3. Run the selected boxes' `<box>.lock.<architecture>` targets against the freshly pinned repository trees
   and atomically replace their optional frozen transactions. The target box's release, repository
   selection, package list, and architecture define the solve. A lock target carries its architecture as
   an incoming transition, so it reads that architecture's repositories. It resolves in its box on the
   host, so a host lacking one of the boxes cannot verify the whole catalog. Thus CI verifies it only on
   x86_64 for now, as Arch is not available on arm64.

Snapshot and resolve take the result from the driver's stdout: a resolve runs in a sandbox that binds the
project and nothing else, so stdout is the one destination that needs no writable path. The tool then atomically
replaces the committed file, or, for `verify-catalog`, compares in memory and leaves the checkout as it
found it.

The catalog tool asks Buck for the selected package's canonical targets and derives the snapshot directory
from their canonical cell and package. `--box` limits which box transactions are resolved, and scopes
the repositories refreshed to those the selected boxes depend on. The pins stay where they are, so a box
whose package list changed re-resolves in place; `--advance` first moves each selected pin to the newest
snapshot its mirror offers. A refresh writes as it goes, since the advanced pin is what the snapshots are
taken at and a snapshot has to be on disk before the box reading it resolves, so a failed one restores
every file it wrote: the advanced pin beside the old snapshots would build neither catalog.

A committed box lock retains any package transport needed to build that exact transaction. The repository
package pool combines those retained transports with its current snapshot, so every intermediate refresh
state remains buildable and an interrupted refresh can simply be re-run. Where the repository's metadata
rather than the package carries the proof of a package, a lock also records the metadata it was resolved
against, as `metadata` entries, and the repository materializes that generation under `retained/` while
the lock still needs a package the current metadata dropped, so the retained transport fetches nothing a
verifier cannot vouch for. The lock records the pin with it, for a verifier whose clock is its own to judge
the generation as of then, the way the current one is judged as of the repository's; a rolling repository
records nothing, since its metadata URLs do not outlive the mirror's next advance. A verifier reads a
retained generation only for a package the pinned metadata does not vouch for, so a generation that fails
to verify fails those packages and no other. A lockless box always resolves from the current pinned
snapshot and therefore needs no retained transport or metadata for packages absent from it.

Transport retention does not turn a rolling mirror into an archive. A URL may eventually disappear; a
clean-cache rebuild then needs a durable archive/content store, while an already fetched artifact can still
come from Buck's content-addressed cache. The lock preserves the identity, expected size, and last route so
that availability can be supplied independently without changing the solve.

`verify-catalog` performs the same generation and fails when committed JSON differs. Repository snapshots
are ordinary Buck source inputs, so changes invalidate only consumers of the changed data.

The catalog tool asks Buck for the targets carrying each role's label, so a package system joins the
snapshot and resolve phases by labelling its repositories rather than by being named in the tool.
Advancing a pin is not there yet: the tool holds a table mapping each system's repository label to the
pin attribute it rewrites and to the function that asks that mirror for its newest snapshot.

A remote repository declaration derives its optional snapshot by stripping `.repository` from the target
name and looking under `snapshot/repo/`. This lets a new repository target analyze before its first refresh;
consuming its empty package pool fails with an explicit instruction to refresh the catalog. A box that
resolves itself needs a usable committed bootstrap transaction. A new box instead resolves through the box
its release names, or through a `resolver_box` of its own; the predecessor supplies only the execution
environment for the planner, while the new box's release, repositories, packages, and architecture define
the generated transaction. Refreshing the
catalog is optional for that box and freezes the generated result at the conventional lock path.

The refresh convention keeps repository and box declarations plus their generated data in the active
catalog's root Buck package, with generated data grouped under `snapshot/{repo,box,key,go}/`. This makes
target-name-derived paths and the package-local optional `snapshot/box/*.<architecture>.json` retention
inputs agree, the latter narrowed to the architecture being built for.

### Authoritative repository package pools

Each remote repository target owns separate dynamic values for its pinned metadata and package pool.
The pool expands the union of the current snapshot inventory and remote transports retained by committed
box locks into one digest-checked package artifact per checksum, and nothing else: a package is
installed, and bootstrapped from, exactly as its repository serves it. A selected package is named with the
one suffix its package system declares, whatever a repository happens to serve it under: an ecosystem that
has changed compressors may still carry a package built before the change, and the byte content, not the
extension, is what a checksum-keyed pool identifies.

The repository target is the canonical action owner, so boxes, buildroots, and images share its downloads.
A package removed from the latest snapshot remains in the pool while a committed box lock references its
pinned URL and size.

`select_package_artifacts()` reads a resolved transaction, looks up each `(repository, checksum)` in the
authoritative pool, and creates a symlinked directory containing the requested representation. It never
creates a second download. Buck materializes only artifacts selected by a consuming transaction, while every
consumer shares their owning actions.

Packages built in this repository use transaction entries with `source = "local"` and a location into an
input package directory. They are projected directly from the producing target rather than copied into a
second pool.

A repository that declares signing keys verifies package signatures after fetching them. The repository
cannot verify them itself, because that takes a box and the box a release names is built from the release's
own repositories; so the image, buildroot or box rule that installs the packages declares the verification
with the box it runs in: one keyring per set of declared key files (validated against the committed
fingerprints), shared by the repositories declaring that set, or the key files themselves for a system whose
verify program reads them as they are, and the selector then verifies what a transaction selects from that
repository in one action, publishing the closure's copies of those packages. One action per closure and
repository, as the overhead of launching the sandbox and `rpmverify` per package is unbearably high. The
unverified pool is only being used by the repository configuration code.

Why repository ownership matters:

- one digest and one action graph node define each upstream package;
- box, buildroot, and image closures become cheap selectors;
- repository snapshot skew fails at the lookup boundary instead of silently downloading different bytes.

### Box bootstrap

A box is a reproducible execution environment built from one base OS release. A family macro declares the
root box of each release. `<family>.<release>.box` contains the package system's own resolver, installer and
indexer, Python, core utilities and sandbox dependencies, and it resolves every other box of the release.
`tine//distribution:tools.box` adds the image-building tools, the VM stack and the PKCS#11 client, from one
list that selects per family where names differ. One declaration serves every release. The tools box takes
the distribution of the target that depends on it, and selects the release of that distribution from
`catalog//:releases.bzl`. It adds its packages to the root box of that release. A dependency without a
distribution, such as an `exec_dep`, gets the tools box of the catalog's default release. An image runs in
the tools box, so it builds with the tools of its own release. A package manager names no box, and an image
runs the plan and install of its package manager in the tools box as well. The tools box resolves during the
build, so a new tool leaves the root box and its lock alone. The base release identifies where this userspace
came from, not the only release it may operate on.

A box runs on the build host. An image, a buildroot, a VM runner, a test and the AMI of a disk take their box
as an ordinary dependency, so that the box takes their distribution. A box whose release follows the
distribution, such as the tools box or `tine//tools:dev.box`, then builds from the release of the image.
`box.sh_test` builds a test once per distribution of its package, or for the distribution that its `distro`
names. A signing key takes the tools box as an execution dependency with `attrs.exec_dep`, because a key
doesn't depend on a release. A command line takes a box with `$(location_exec)`. A target named on the
command line or reached as an ordinary dependency is configured for the target platform instead. To build
each box once, the box has an incoming transition to the host's platform plus the constraints of its
release's distribution. Each distribution declares that platform beside it, as
`<family>.<release>.distribution.host`. tine's execution platform registers no exec marker constraint, so the
execution configuration is the same as the host platform's. `buck build`, `buck run`, execution dependencies
and ordinary dependencies therefore all reach the same box. This assumes one execution platform, for the
host's architecture. An execution platform for another architecture, such as a remote arm64 one, still gets
the box built for the host. Its actions then run binaries for the wrong architecture.

A box's packages can `select()` on the architecture, and `distribution.select()` on the release or its
family. A box lock is configured for its own architecture and its release's
distribution, so it resolves the packages for that architecture and distribution. A child box's lock reads
its parent's packages from the parent's lock for the same architecture, because the parent box itself is
configured for the host. A child box can therefore only keep a lock for an architecture that its parent keeps
one for.

A remote repository serves one architecture, and its content depends on nothing else. Its rule has an
incoming transition to that architecture's target platform. A box lock, the box and an image of the same
architecture therefore reach the same repository, and each file of the repository is downloaded once.

A lockless box uses its resolver box, by default the one its release names, to produce its build
transaction and perform the authoritative installation. Its target root therefore contains only the
requested packages and their dependencies; it does not need Python, package-manager libraries, or
other construction tools unless they are part of its intended runtime.
A locked box's `<box>.lock.<architecture>` target runs the explicit update command through its resolver
box; a root box has none and runs it through its own completed root. A resolver box only changes where
resolution and installation execute, not the repositories, requested packages, architecture, or root built
for the new box. Box resolution does not consume
package-manager priority policy; its repositories use the native default priority until bootstrap needs an
explicit policy of its own.

Only a box declared `root = True`, which has no predecessor, bootstraps its own installation tools in
two stages:

1. The minimal extractor unpacks the same package closure into `stage1` without running scriptlets
   or creating a package database. An extractor reads the packages its repository serves, whatever
   framing they carry, so the pool never has to derive a second form for the bootstrap.
2. The package-system installer runs from `stage1` and properly installs the closure into `stage2`,
   including scriptlets and the package database. `stage2` becomes the reusable `BoxInfo` root, a single
   layer.

Before that second stage, `stage1` verifies the same closure against the release's declared signing keys,
the way a predecessor box verifies a successor's packages. That catches an unsigned or tampered package
and a wrong key, but the verifier itself was extracted unchecked, so a root box is not a root of trust
independent of the packages it bootstraps from.

The `<family>.<release>.box` of every release is a root box.

A first lock is the one thing a root box cannot produce for itself, since resolving needs a box to
resolve in. Pointing the new box's `resolver_box` at an existing box that can run the new package
system's planner breaks that cycle for one refresh; making it `root` afterwards leaves the box
self-sufficient. That predecessor has to carry the new system's package manager, which a different
family may well package. This is a one-time exposure per new root box, not a standing dependency.

Box configuration prefers a target-provided systemd factory `nsswitch.conf`, but writes a deterministic
files/DNS fallback for minimal roots. Resolver integration and target configuration therefore do not impose
specific implementation packages on a derived box.

The second-stage install is authoritative for package metadata, ownership behavior available through the
unprivileged sandbox, and scriptlets.

The host contract is intentionally small; its short list of requirements is documented in
[images.md](../user/images.md).

### Box layers

If a box and its `parent` come from the same repositories, the box is a delta layer over its parent's
layers, like with images. It enforces addition only, and fails if its lock would update or remove parent
packages. One likely cause is an outdated committed lock, which needs a catalog refresh. The other is that
resolving both package lists together picks other packages than the parent got, for example
`libcurl` for a box that needs it where the parent has `libcurl-minimal`; `layered = False` then installs
the box on its own.

The sandbox merges the layers per top-level directory with a read-only overlay. Unlike `rootfs()`, it does
not decode stored whiteout markers, as that would walk every layer on every action, so the installer refuses
a box layer that deletes from the layers below.

If the parent has different repositories, the box only shares the parent's package list and is installed
on its own.

`cargo.package()` and `go.package()` add their `buildroot_packages` to their box in a layer that an anonymous
target builds. Buck keys an anonymous target on its attributes, the box and the sorted package list, so
every target that adds the same packages to the same box shares one layer. The layer resolves during the
build, because it has no target that a committed lock could belong to.

### Execution isolation and target roots

All build actions run through `box.run()` and `box/sandbox.py`. The sandbox mounts the box's
userspace read-only over an otherwise isolated namespace, supplies API and temporary filesystems, clears the
host environment, disables network by default, and gives build actions unprivileged fakeroot behavior. A
build action runs as root, and its sync calls succeed without doing anything. When the sandbox maps only
one user ID, for example when tine runs as an unprivileged user, the action's chown calls also succeed
without doing anything.

The project is mounted at `/tine/project` and the action runs there, rather than at the path it is checked
out under. A checkout is then free to live anywhere, including under a directory the box populates
itself such as `/var/lib`, and an absolute path that leaks into a build output is the same for every
checkout. The sandbox clones the project's mount tree before it creates its user namespace, so the kernel
doesn't lock a mount below the project, such as a `tine mount`, and overlayfs accepts the project as a lower
directory. Each chrooted `run` or `copy` mounts the project read-only under the target root's `/run` for
the duration of that operation. Package scripts and remove globs cannot reach the project through that
mount. apivfs covers `/run` with a tmpfs so the mount point is not captured into the image.

The sandbox only creates the execution environment. Drivers own their target-root layout through
`rootfs.rootfs()`:

- a fresh install or image layer binds an output directory at `/buildroot`;
- an incremental install or image layer mounts an ordered lower stack plus a persisted upper;
- a package build mounts its buildroot stack with an ephemeral upper and binds action scratch at `/build`;
- pack/disk operations merge a stack with an ephemeral upper so cleanup does not modify stored layers.

This division keeps one namespace boundary while letting each driver express the root it needs. Nesting a
second sandbox inside a box would duplicate isolation, complicate mounts, and make remote execution
harder.

`box.run(relaxed = True)` is reserved for interactive leaves. The box still supplies userspace, but
devices, `/run`, environment, current directory, and network come from the host, and the command remains the
invoking user. The box's `nss-systemd` reads native identities from the host's UserDB services under
`/run`. This avoids importing host NSS modules or shadow databases, which may be incompatible with the
pinned userspace. A box target's own `RunInfo` and `image_vm` are the interactive consumers; build
actions never use relaxed mode.

### Native package installation

Native package installation has three phases shared by buildroots and images:

1. **Plan.** Run `PackageSystemInfo.plan` against each configured repository's materialized directory,
   effective priority, and solver cache. Weak dependencies are disabled. Existing lower layers are mounted
   read-only so installed packages can satisfy an incremental request.
2. **Select.** Use the resulting transaction to select package files from repository pools, verified after
   download if the repository declares signing keys, named with the package system's declared suffix so its
   installer finds them. A local repository is materialized by
   an anonymous indexing target using the consumer's box. The same path handles
   package-build inputs and lets the solve choose between local and upstream packages.
   Extra packages arrive on two mutually exclusive paths: a package build passes its explicit
   `buildroot_packages` outputs, while a package manager with attached `local_packages` computes the
   request's runtime closure at analysis time from imported metadata and offers exactly the locally built
   packages in it. Buildroots reject managers with local packages, because buildroot contents must come from
   the explicit, cycle-checked self-hosting locks.
3. **Install.** Run `PackageSystemInfo.install` over the exact package directory, whose file names carry
   the package system's suffix. `install_packages()` owns a fresh root or incremental buildroot delta. An
   image layer instead invokes the same installer against its already-mounted root so package and
   filesystem operations have one output owner.

What a request looks like, rather than how one package system answers it, belongs to the neutral layer:
`package/transaction.py` owns the transaction schema both planners write and Starlark reads back, and
`package/installer.py` mounts the root an install spec names and captures it afterwards, so a driver is
left with its own transaction and nothing else. `package/repository.bzl` owns the pool merge, whose
rule (the repository's current route wins while it carries the content, a lock's transport is the
fallback once it does not, and a size that disagrees is skew) is stated once for both.

The package manager assigns its repositories their default priorities: local repositories use 50 and remote
repositories use 99, with target-specific overrides applied by `package.manager()`. A planner
action carries its repositories as `{id, directory, priority, baseurl}` objects in its spec.
`encode_repositories()` projects them from `ConfiguredPackageRepositoryInfo`; the record's `dependency` is
deliberately stripped because Buck dependencies are analysis-only and not JSON-serializable. `plan.py`
reads each object into its `Repository` named tuple. The directory selects the pinned local repository
metadata, while the base URL records the transport for remote packages selected into a transaction.

Package-specific `buildroot_packages` use the same representation. Their ordered package directories are
materialized as an anonymous repository with ID `extra`, local priority, no base URL, and no declaration
dependency. Transaction selection maps its local locations directly back to the producing package outputs.

Fresh buildroot installs are anonymous targets keyed by package manager, sorted install specs, and local
package inputs. Buck therefore shares the base buildroot analysis/action graph across packages that use the
same build profile. Package-specific BuildRequires layers remain inline and are installed over the shared
base.

After installation, the installer parks the package database and scrubs the package-manager and ldconfig
bookkeeping that would otherwise make identical roots differ; what parking takes is each package system's
own business. The action that owns the root then captures names and overlay metadata into Buck-storable
form. A fresh root receives the `uninitialized` machine-id marker systemd initializes on first boot;
incremental installs preserve any existing machine ID.

`LocalPackageInfo` intentionally does not carry the producer's box. `local_repository` checks that its
packages use one native package system and remains a box-independent declaration. An image or a buildroot
that installs from it materializes deterministic repository metadata with the box it runs in; anonymous
materializations with the same box, package system, and ordered package directories share one action.

### The RPM package system

The first implementation of `PackageSystemInfo` covers the RPM family. Everything below is confined to its
drivers under `package_system/rpm/` and to the catalog policy that selects them.

libdnf5 resolves and rpm installs. A repository's build metadata is a filtered `repomd.xml` plus the
primary, filelists, and group streams libdnf5 needs, each named by its own checksum, so a snapshot pins
content rather than bytes and drops the streams nothing will read. Parsing it is expensive enough that the
planner reuses it across solves: the `make-cache` verb prebuilds one cache per configured repository, which
is why this system leaves `solver_cache` at its default. Packages end in `.rpm` and the database lives at
the usr-merged `/usr/lib/sysimage/rpm`. A repository pinned with `rpm.remote_repository()`'s
`rpmrepo_mirror`/`rpmrepo_snapshot` carries that pin as `rpmrepo.*` metadata and advances to the newest
snapshot its gateway enumerates.

`rpmkeys` verifies package signatures. The `keyring` driver imports a repository's declared key files into
rpm 6's filesystem keyring, which names each key by its fingerprint and so checks the declaration without
an OpenPGP parser. The `verify` driver checks each package against it at verify level `signature` (upstream
rpm's default `digest` accepts an unsigned package where Fedora's `all` does not). No key is imported into
an assembled root's rpmdb, and the installer leaves libdnf5's own checks off.

The bootstrap extractor frames the header off a package and decompresses the payload itself, so it reads
exactly what the repository serves. It supports the v4/newc payload form the pinned repositories use and
does not implement RPM v6's index-based payload metadata.

After installation the driver checkpoints and vacuums the SQLite rpmdb, removes its WAL/SHM/lock files, and
scrubs libdnf5 and ldconfig bookkeeping. Documentation and language filtering are rpm's own `nodocs` and
`_install_langs`. A locally built package keeps the `<directory>/<file>` location convention that maps it
back to the input directory it came from.

The default catalog declares Fedora Rawhide from the `fedora_release()` bundle, which also declares the
release's root box. Another Fedora release gets a root box of its own. It offers no CentOS Stream release:
nothing publishes immutable CentOS Stream composes, so its pinned metadata stops resolving the moment the
mirror advances, which is not a repository this can pin.

#### Import and build flow

The OS.git repository's `packages/` tree contains imported source-package metadata and generated BUCK
files. The importer emits data; the Starlark in `package_system/rpm/generated.bzl` validates that data and
creates targets.

For each branch, `rpm_branch()` currently:

- combines common and the configured architecture's BuildRequires;
- indexes each imported binary package's name, `Provides`, and file paths;
- maps BuildRequires capabilities to source-package targets;
- computes strongly connected components and drops ordinary intra-cycle edges to upstream packages;
- retains explicitly configured buildroot-only edges and rejects cycles they reintroduce;
- creates one `rpm_package` target per source package;
- publishes the branch's binary-package runtime metadata as a `:_local_packages` target for
  manager-attached local package selection.

This is a static, import-time self-hosting approximation. It is useful today but is not the planned final
dependency lock: rich dependency parsing is intentionally limited, the graph is one for every architecture,
and runtime package closures are still delegated to libdnf5 at buildroot-plan time.

An `rpm_package` action:

1. obtains the shared base root from its `BuildrootInfo`;
2. resolves and installs its BuildRequires delta, preferring RPMs from `buildroot_packages` over upstream;
3. overlays the base and delta, stages its spec/sources in Buck action scratch, and runs `rpmbuild -ba`,
   or `rpmbuild -bb --build-in-place` on a volatile overlay of a populated source tree. overlayfs shows the
   directory under a mount rather than the mounted tree, so each directory mount below the source tree,
   such as a `tine mount`, gets a volatile overlay of its own. The driver discovers directory mounts with
   `listmount()` and `statmount()` using unique mount IDs. With `in_place_packaging`, a separate action
   first fetches the dist-git commit that the tree pins, and the build takes its spec and Source/Patch files
   from that commit. The project is read-only until the driver exits;
4. freezes `%autorelease`, `_buildhost`, the dist tag, and the per-package source date epoch;
5. collects binary RPMs and the source RPM into one output directory, carrying its package-system identity
   in `LocalPackageInfo`;
6. exposes each declared binary subpackage as a Buck sub-target and checks that declared outputs exist. With
   pinned packaging, it prints the subpackages that differ from the declared ones instead of failing,
   because the pinned spec can declare other subpackages than the imported metadata.

The build currently uses `--nocheck`. Automatically generated debuginfo/debugsource RPMs are retained in the
directory output but are tolerated rather than exposed as declared sub-targets. Successful build scratch is
discarded; failed scratch remains available for diagnosis.

A hand-written `rpm.package()` can name the distribution of its buildroot's release with `distro`. The
target is then incompatible with an architecture that the release does not serve. An imported package
names none, and gets no per-release aliases either.

#### Limitations

- The imported self-host dependency graph is inferred from stored BuildRequires/Provides/file metadata; it
  does not run RPM's dynamic BuildRequires protocol.
- Build cycles fall back to upstream RPMs for ordinary intra-SCC edges, so the package set is not a fully
  self-hosted fixed point.
- Builds use `--nocheck`; package test policy is not implemented.
- Debuginfo/debugsource outputs are not first-class declared sub-targets.
- The bootstrap extractor supports the pinned v4/newc payload form, not RPM v6 metadata.

Entry points: `package_system/rpm/{rules,catalog,generated}.bzl` and
`package_system/rpm/{snapshot,plan,install,pkgdb,createrepo,build,extract,rpmfile}.py`.

### The alpm package system

The second implementation of `PackageSystemInfo` covers Arch Linux. It declares no rule of its own:
the repository rule, how a pinned repository is materialized, and how one is declared are all neutral,
so what a package system adds is its pin, whatever else its snapshot driver needs to name its metadata,
and the drivers themselves.

libalpm resolves and pacman installs. `alpm.py` binds libalpm through ctypes, the way `kmod.py`
binds libkmod, and `plan.py` drives a transaction against the pinned databases with it; `install.py` runs
`pacman --upgrade` over the exact closure that produced. Both are the same library, so a plan and
its installation cannot disagree about which package provides a capability or which version is
newer, and none of those semantics are reimplemented here.

Binding the library rather than driving its front end is what makes the plan a data structure
instead of text. A resolved package carries its own checksum, size and file name, so a transaction
is written from what resolved it rather than from a second pass over the databases. Removals are
visible, and since a transaction is a set of packages to add and nothing downstream can express
one, an install that would remove something a lower layer carries is refused by name rather than
silently dropped. And a capability more than one package provides arrives as a callback rather
than a prompt with nobody to answer it: the solve records the providers and fails naming them,
because a build cannot be asked and a snapshot advance could otherwise change the choice silently.
Naming the wanted provider among the install specs settles it. The one capability an image here
would otherwise have to choose a provider for is `initramfs`, which `linux` requires: the solve is
told to assume it installed instead, because tine builds the initrd itself and a generator's
install hook would only write one into a tree that discards it.

The soname is the one the box pins, so a pacman major release fails to load with that name in
the message rather than resolving against a different ABI. Applying a transaction stays with the
command: it needs no structured result, only an exit code, and driving a commit through the
library would mean owning its progress, conflict and scriptlet callbacks for nothing.

What the planner owns is the frame: the pinned databases are staged where alpm looks for them, the
layer stack below becomes the root the solve resolves against, and each resolved package is named
by the content checksum its repository published. Nothing is fetched and only the throwaway root
staged below is written to, so a solve stays a pure function of the pins.

A package is a tar under whichever compressor its era used, but the graph names selected packages
`.pkg.tar.zst` and nothing else. Arch has served zstd for years; one package left in `extra` under
the old `.xz` is recognized where a database is read, is never in a closure, and is due to be
rebuilt. Telling a package from its detached signature is a separate question from naming one, and
only the first has to accept an older compressor.

`alpm.py` also reads and writes the formats around that library: what a repository database says
about a package, how to write one, and how to open a package's tar. That is not a duplicate of what
the library does but a consequence of where it runs, since the snapshot and extract drivers are host
tools and the host contract does not include libalpm. Two constraints of the format shape the
drivers. alpm reserves every top-level entry beginning with a dot for its own metadata, so the
bootstrap extractor skips all of them rather than a fixed list. And alpm refuses a `%FILENAME%`
containing a separator, so the `<directory>/<file>` location convention that maps a locally built
package back to its input directory cannot be a path here: `index.py` encodes the directory in the
file name instead, and `plan.py` decodes it.

A repository database carries no checksum of its own and no content-addressed name, so its snapshot
pins the bytes the refresh saw. That is what makes the Arch Linux Archive's dated trees the usable
mirror and an ordinary rolling one unusable: `pacman.remote_repository()`'s
`archive_mirror`/`archive_snapshot` pin, carried as `archlinux.*` metadata, names a day, and since
the archive publishes a tree per day rather than an index to enumerate, the refresh reads the marker
recording when it last finished one.

Installation runs hooks and install scriptlets as they would run on a real system. Documentation and
language filtering map onto `NoExtract`, which is where pacman expresses them. The architecture is
pinned from the spec rather than taken from the build host's uname, and the hook directory is named
explicitly, because alpm rebases its system hook directory onto the target root but not the
administrator's, and the box's must not run against the image. Afterwards the driver replaces the
wall-clock `%INSTALLDATE%` alpm records with the assembly epoch, and drops ldconfig's auxiliary
cache, the sync databases and the transaction log.
It rewrites only the entries that actually change: alpm keeps one file per package, so rewriting an
entry a lower layer already parked would copy that whole database up into this layer's delta.

The database stays at Arch's own `/var/lib/pacman`, since Arch has no usr-merged location for it. The
package-database artifact is read from the assembled layer stack rather than from a terminal output,
so a `/usr`-only disk does not affect it. Each entry's `mtree` is dropped from that capture: it is
what pacman alone reads to verify an installed tree, and it is the largest part of an entry after the
file list.

alpm needs no prebuilt solver cache. A repository database is the metadata pacman reads directly,
not a format to convert in advance, so this package system declares `solver_cache = False` and no
cache target is created for it. There is no package builder either: `arch_release()` defines a
`buildroot` package set, but no `makepkg` rule consumes it, so `PackageSystemInfo.build` is unset.

`arch.rolling.box` is the one that made this second package system prove itself. It is a root box:
bootstrapping it is an alpm extraction followed by a pacman install, and it then resolves its own
lock from its own root. Its first lock could not come from itself, so one refresh pointed
`resolver_box` at the RPM box, which packages pacman; making it `root` afterwards left the box
self-sufficient.

Limitations specific to this system:

- It cannot build packages, so an Arch image can only install upstream ones and no target builds a
  local alpm repository. The local-package naming rule is unit-tested, but nothing exercises that
  path end to end.
- Arch signs each package with an individual packager's key and vouches for those through
  certifications by its main keys, both shipped in `archlinux-keyring`. The declared signing keys are
  the main keys; the keyring driver merges the declared files with the box's copy of that package, drops
  what its revoked list withdraws, and certifies the declared keys locally with marginal ownertrust, so
  that a packager's key is valid once three declared main keys certify it: `pacman-key --populate`'s
  model, with the catalog rather than the package choosing the main keys. A signature is read from the
  pinned database's `%PGPSIG%`, or from the database a frozen box lock retains once the pinned one
  stopped describing a package the lock selected; the keyring judges both as of the repository's
  pin, since it computes validity once when built, so a packager key that expired between the two
  pins is refused until the lock is refreshed. The keyring's gpg clock is stopped at
  the archive day the repository is pinned to, so key expiry is judged as of the snapshot and a build
  of it verifies the same way however much later it runs; an unpinned repository judges as of the build.
- Pinning a database by content means a rolling mirror goes stale the moment it advances; only an
  archive with immutable dated trees is usable as a pinned repository.

Entry points: `package_system/pacman/{rules,catalog}.bzl` and
`package_system/pacman/{alpm,snapshot,plan,install,pkgdb,index,extract,keyring,verify}.py`.

### The deb package system

The third implementation of `PackageSystemInfo` covers Debian. Two upstream tools do the work. APT
decides which packages to install and in which order, and dpkg installs them.

#### Resolution is APT's own

A build needs the chosen package set as data: each package named, with the checksum its repository
published. `apt-get --print-uris` states exactly that. In place of fetching anything it lists every
package it would fetch, one per line, with the size and checksum the index states for it.

The planner stages each pinned repository under a directory of its own and points APT at those. The
URI of a fetch therefore names the repository APT read the package from, which matters when two
repositories carry the same name and version with different bytes.

Nothing about dependency resolution is reimplemented here, so the outcome is the set APT would have
installed. A solve that needs to remove a package fails in APT itself, since nothing downstream can
express a removal.

APT reads configuration from the box it runs in, which a build must not depend on. Most settings can
be overridden on the command line, but the two that say where configuration is read from are applied
before the command line is. So the planner writes a configuration file naming those and points
`APT_CONFIG` at it.

#### Verification follows Debian's one signature

Debian signs a single file per suite, `InRelease`, and nothing below it. Everything else is reached
from there by checksum:

1. `InRelease` carries the signature, and states the checksum of every index.
2. An index, `Packages`, states the checksum of every `.deb` it lists.

The verifier walks that chain at build time with `sqv`, the tool APT itself verifies with on this box.
It checks the signature on the pinned `InRelease` against the keys the repository declares, then the
index against that `InRelease`, then the bytes of each selected package against that index.

`sqv` names the key behind each good signature, and only a key named as it was declared counts. A key
file holding some other key than the one it is declared as therefore vouches for nothing, and no
OpenPGP is parsed here to find that out. `sqv` reads the declared key files as they are, so this system
has no `keyring` driver.

Both pinned files keep the path the mirror serves them under, such as `dists/testing/InRelease` and
`dists/testing/main/binary-amd64/Packages.xz`. That layout is what the signed `InRelease` is held to:

- It has to say it is the suite its directory names, by `Suite` or `Codename`. An archive signs every
  suite with the same keys, so the signature alone would accept the `InRelease` of another suite.
- It has to state the index at the path the index sits at. Every index of a suite is signed, so
  matching the bytes alone would accept the index of another component.

What the check is judged against is the time the repository is pinned to, not the time the build runs.
That time bounds two things. A signature made after it is refused, and an `InRelease` whose
`Valid-Until` had passed by then is refused. It does not bound the key: `sqv` judges a key as of the
signature it made, so a key that has expired since still vouches for what it signed. A build of a
pinned repository therefore verifies the same way years later. A repository with no pin is judged as of
the build instead.

A lock keeps the `InRelease` and the index it resolved against. A package the archive has dropped
since is still vouched for by those.

#### Installation lays a fresh root down before dpkg runs

dpkg runs the maintainer scripts a package ships, and in a fresh root the first of those scripts
cannot work. A `preinst` runs while its package is unpacked, it calls a shell, and no shell is
installed yet.

So a fresh root takes three steps, and a root that already has packages in it only the last:

1. The whole closure is extracted with no script run at all, which is what debootstrap does and why.
2. What the install was asked to leave out, such as documentation, is removed again. dpkg skips those
   paths itself, but it never removes one that is already on disk. Each such tree goes whole, and dpkg
   puts back what its rules keep of it.
3. APT is handed the closure as package files, with no repository to resolve against. It decides the
   order and runs dpkg, so a pre-dependency is configured before the package that needs it is
   unpacked.

A `policy-rc.d` file denies daemon startup while dpkg runs, and the install removes it afterwards.

Nothing sets up the merged-`usr` symlinks. `base-files` ships `/bin`, `/sbin`, `/lib` and `/lib64` as
links since 13.3, so it is extracted first: a package that ships a path under one of those would
otherwise make it a directory. A link that a directory is in the place of fails the install. A suite
older than trixie ships no such links, and cannot be installed this way.

#### The box bootstraps itself

`debian.testing.box` is a root box, meaning it resolves its own lock using its own root. Its first
lock could not come from itself. So one resolve pointed `resolver_box` at a throwaway Arch box, which
packages apt and dpkg, and making it `root` afterwards left it self-sufficient. The suite is testing
rather than stable because this image format needs systemd 258's repart, and stable is two releases
behind that.

Limitations specific to this system:

- It cannot build packages. A Debian image installs upstream ones only, so the system has no `build`
  driver and no `index` driver for a local repository either.
- Debian names the architecture in the `Packages` path, not in the mirror URL, so a repository cannot
  spell it with `$basearch`. It reads the name off the snapshot spec instead, which is what lets one
  declaration serve every architecture.
- A suite has to index the packages of architecture `all` in each architecture's own `Packages`, as
  Debian does. One that keeps them in `binary-all` only is refused at refresh, since one index is
  pinned per repository.
- A suite served from a directory below another, such as `stable/updates`, is refused.
- The snapshot pins the signed `InRelease` without checking its signature, and reads the chain from
  the plain `Release` served beside it. Checking it would need a keyring on the host, and the host
  contract is only a pinned Buck and a pinned Python. The build verifies the pinned file, so a refresh
  that pinned a forged one, or an index the signed one does not state, fails the next build rather
  than the refresh.

Entry points: `package_system/deb/rules.bzl`, `package_system/deb/{aptget,deb822,debfile,release}.py` and
`package_system/deb/{snapshot,plan,install,pkgdb,extract,verify}.py`.

### Rust source builds

A consuming repository can build a Rust project it has checked out instead of packaging it first.
`cargo.package()` takes the project directory as input, so a clone of a project that commits its lock needs
nothing added to it, and the single `Cargo.lock` in it identifies the workspace root. An action discovers
that lock after the sources have been built, so the checkout may itself be a fetched directory artifact; a
dynamic action then reads the resolved lock and declares what the build fetches. A project without
dependencies commits a lock too: the build passes `--locked`, and cargo cannot write a missing lock into the
read-only sources. The declaration contract is in [cargo.md](../user/cargo.md).

A lock entry's `checksum` is the SHA-256 of its crates.io tarball and `static.crates.io` serves that
tarball under a URL derived from name and version, so each registry crate becomes one hash-verified
`download_file`, the same treatment as repository packages, and deriving them needs no network; a lock
older than version 3 records no checksums and is rejected. A git dependency is pinned by the commit in its
lock source and becomes a repository fetch instead, transitive dependencies included, since the lock
always carries the full commit; each fetch
shallow-fetches its commit and fails unless `FETCH_HEAD` is exactly that hash, so the commit itself is
the integrity check. Anything from another registry is rejected with the package named.

A build is then two actions:

1. `cargo_vendor` unpacks the registry crates into `vendor/<name>-<version>/` with the
   `.cargo-checksum.json` cargo expects. Git dependencies never enter this tree.
2. `cargo_build` runs cargo in the package's box with the network unshared, against the vendored tree
   and the fetched repositories: each git source is replaced by its repository, served over git's local
   `file://` transport, so cargo takes its own checkout and resolves each crate inside its workspace,
   inheritance and sibling path dependencies included, and it insists on finding the locked commit in the
   replacement. Cargo classifies every git transfer as remote no matter the transport, so a build with
   git dependencies drops `--offline`; the unshared network is what keeps it offline. A cargo
   configuration file inside the checkout would outrank the one the driver writes, redirecting its
   sources, so the build refuses it. The declared binaries come out of `target/release`.

A vendored directory is not cargo's only offline mode (a pre-populated `cargo fetch` cache builds offline
too), but it is the only one buck can assemble from individually hash-verified downloads, and the cache
layout is cargo's private business. `--locked` then makes the build fail rather than resolve differently
from the committed lock the downloads were derived from. `cargo-auditable` embeds the crate graph in each
binary, which syft catalogs as `pkg:cargo` components, so a from-source binary reports its dependencies in
the image SBOM the way an installed package does.

The unit of caching is the project: any change to its sources reruns one action for the whole crate graph.
Splitting that into one action per crate would require the crate dependency graph rather than just the
lock, and is deliberately not attempted.

The rerun is made cheap instead for a `tine mount`ed checkout, for a developer working on that part: Cargo's
build directory then is a declared output that buck is told not to clear before rerunning the action, so
cargo finds the previous one and recompiles only what changed, exactly as it does in a working copy. Buck
keeps the binaries directory of a mounted build as well as its build directory, so the driver replaces the
binaries of the last run instead of adding to them. A build that finds no previous directory remains the
reference, which is what CI and any `buck clean` produce.

Every cargo build, mounted or not, runs in the action's scratch space, which the sandbox mounts at
`/var/tmp`, outside the project. The driver bind-mounts the source tree at a fixed path there. Cargo searches
every parent directory of the workspace for configuration and for a workspace manifest, and none of those
parents is in the consuming repository. The project is read-only while the driver runs, and so is the bind of
the source tree. Cargo's documentation says that a build script should write only into `OUT_DIR`, because two
builds that share a dependency can run its build script at the same time. A write into the sources therefore
fails the build instead of being discarded on an overlay, as `rpm.package()` must. The project's sources and
its vendored crates.io dependencies are read-only. Cargo checks a git dependency out into its home directory
in the scratch space, which is writable. The declared output directories are mounted writable in the scratch
space.

Fetched or committed sources declare no build directory, and cargo keeps its build directory in scratch
space: there is no edit cycle to speed up, and the large intermediate build artifacts are not uploaded to a
shared cache.

### Go source builds

`go.package()` gives a checked-out Go project the same treatment, sources in and declared binaries out. The
project carries no build file pointing at its own root, so a `go_workspace` action finds the `go.mod` among
the built sources, or reads the `go.mod` in the directory that `module_root` names, and a dynamic action
declares the two steps below from what it reports; as with `cargo.package()`, that is what lets the sources
be a fetched directory artifact rather than a checkout. The fetch's inputs are the `go.mod`, the `go.sum`
beside it, and the `go.mod` of each module that a `replace` directive points at a directory in the sources. A
dependency bump reruns the fetch, and an edit to a Go source file does not. But the pinning is delegated
rather than translated: A `go.sum` records `h1:` dirhashes over each module's contents, not the hash of any
bytes a proxy serves, so there is nothing a hash-verified `download_file` could check a download against.
Deriving byte hashes would mean a second, generated lock to keep refreshed. Instead go itself is the
verifier, and the build is two actions so the network stays confined to the first:

1. `go_fetch` is the online action: `go mod download` in the package's box with the network shared, reading
   only the fetch's inputs named above, so an edit to a Go source file never refetches. go checks a download
   against the committed `go.sum` where that pins it, and against the checksum database otherwise.
   Downloading deliberately does not extend `go.sum`, so an unpinned module is fetched here and rejected in
   the step below. The driver does reject a `go.sum` missing the module graph's `go.mod` hashes, which is the
   one incompleteness that downloading repairs silently. The output is go's module cache. The `go_workspace`
   action finds the `replace` directives in the `go.mod` itself instead of running `go mod edit -json`,
   because it runs on the host rather than in the box that provides go.
2. `go_build` compiles offline. The `cache/download` half of a module cache is exactly the layout a
   module proxy serves, so the driver points `GOPROXY` at it as a `file://` URL and go re-extracts every
   module from it, verifying against `go.sum` a second time (the fetched artifact is never trusted
   implicitly). `-mod=readonly` makes a lock that no longer agrees with `go.mod` a failure rather than a
   silent re-resolution, and `GOTOOLCHAIN=local` keeps go from downloading another toolchain. The build
   runs the box's go, the toolchain that `toolchain` names, or, for a `go.releases` toolchain, the release
   that `go_workspace` picks from the project's nearest `.go-version`.

The `packages` mapping selects the main packages to build and names their outputs. A checked-out project
often carries commands an image does not install; building those would cost time and may require
additional build requirements. The driver resolves each selection with `go list` and builds it with
`go build -o`, using the declared output name.

No auditable wrapper exists in this path because go itself embeds the module list in every binary it
links. syft catalogs these as `pkg:golang` components in the image SBOM. The declaration contract is in
[go.md](../user/go.md).

A local `go build` inside the checkout leaves no build tree behind: go's cache lives outside it, so there
is no `target/` equivalent for Buck's ignores to exclude. It does drop the
binary it built into the current directory, which then is a source like any other file there, so a
project is better built with `-o`.

The unit of caching is the project, not the package: one action per package would mean modelling the
package graph and the toolchain here, which is what rules_go exists for, and go's own content-keyed build
cache gets most of that back for none of it. That cache follows the same rule as cargo's build directory
above, and keys on file contents, so a mounted project's rerun recompiles only what actually changed. The
build runs in the source tree itself, in a read-only project, with the declared outputs mounted beside it.

The module cache is the one difference: `go_build` consumes it, so it is a declared output whatever the
sources are. For a mount buck keeps it too, so a dependency bump downloads only what is missing; old
module versions accumulate but are inert, since go takes only what `go.sum` names out of the proxy view.
Otherwise buck clears it before the fetch, and the cache content depends only on the fetch's inputs.

A `go.package()` that sets `test_packages` also compiles the tests of those packages in `go_build`, with
`go test -c`, one test binary per package. The test binaries use go's build cache of the same action, so the
project's packages compile once for the binaries and the tests. A test that does not compile fails the
build. The test compile runs without vet, because a vet finding would fail the build too. `go.test()` runs
each test binary under `buck test` in the package's box, in the package's directory, with the sources
read-only. A pattern in `test_packages` that matches a package outside `src` fails the build, because that
package's directory does not exist when the tests run.

### Filesystem layer representation

Buck directory artifacts cannot faithfully store overlay whiteout devices, opaque-directory xattrs, or a
backslash in a path component. tine stores filesystem deltas in a regular-file representation:

- `.wh.<name>` represents a whiteout;
- `.wh..wh..opq` represents an opaque directory;
- `.esc.<percent-escaped-name>` represents a path component Buck cannot store.

`rootfs.capture()` translates a native overlay upper into this form after unmounting. Before a later mount,
`rootfs.rootfs()` constructs sparse sidecar layers that translate the stored markers back to native
overlayfs whiteouts/xattrs and thaw escaped names. Stored layers remain ordinary Buck artifacts and can
therefore move through its CAS.

Directory modes are made traversable so Buck can materialize/delete them, and Buck's artifact model does
not preserve general ownership, capabilities, xattrs, or every mode bit. Authored tmpfiles snippets can
recreate paths, modes, and xattrs during terminal assembly. Ownership is deliberately normalized to uid/gid
zero rather than reconstructed. SELinux labels are not currently produced.

### Selecting a distribution

Which distribution an image is built from is a configuration, and the target carries it.
`tine//distribution` declares two constraint settings, `distribution` and `family`. A catalog release
declares one value of the distribution setting beside its other role targets, as
`<family>.<release>.distribution`, and that target also sets the value of its family. The target is the key a
`select()` branches on, and it is the incoming transition that sets the two values. An image that names no
package manager and no parent takes the package manager of its release from `distribution.package_manager()`,
which selects among the package managers of the package's releases:

```Starlark
distribution.select({
    "<family>.<release>": "catalog//:<family>.<release>.package-manager",
})
```

Every rule that declares a logical image accepts a `distro`. A target that names none is
buildable under any its package serves, and the rule declares one alias per distribution to say which,
so the names are never a list kept beside the images. `distribution.alias()` does the same for a target
tine does not own. The transition sets the constraint only where nothing has set it, so the
target being built decides and everything under it follows: an image's `parent` chain is not a
distribution of its own, it is whatever the leaf pulling it in is. One declaration therefore serves every
distribution, and only the alias names are per-distribution.

A release serves a list of architectures, and Arch serves only x86_64. Its repositories and release resolve
`target_compatible_with` to `select_incompatible()` for any other architecture. The release's boxes are built
for the host, so its boxes are incompatible on a host of another architecture. On such a host, the release
serves no architecture at all. Where a release serves no architecture, the transition leaves the distribution
unset, and `compatibility()` makes every distributed target and alias incompatible. A build of `//...` then
skips the Arch targets for arm64 and on an arm64 host. On such a host, no compatible target depends on an
Arch box, so a whole-cell run is `//...` on every host.

What varies between distributions stays where it belongs. A release names the packages a bootable system of
its own family needs, so an image asks for the `bootable` package set rather than for concrete package names.
Where a genuine difference remains, `distribution.select()` keys it by family or by release. Every
distribution also sets its value of the `tine//distribution:family` constraint, so a `fedora` key matches
every Fedora release. Buck picks a release key such as `fedora.43` over the `fedora` key, because the
configuration of the release's distribution contains the family constraint as well. `distribution.select()`
turns the release key into `catalog//:fedora.43.distribution`. `distribution.new()` fails for a distribution
outside the root package of the catalog cell, so every release's distribution has that label. A select
never reads the names in a package's table. A key such as `fedora` therefore always means the family,
whatever a package calls its distributions.

The repository supplies no default. A target declared for no distribution in particular names none of them in
its `select()`, and is `target_compatible_with` a value no platform carries wherever none was chosen,
so a build that has chosen one resolves, `//...` skips the rest, and naming one directly fails as
incompatible rather than quietly picking, reporting the missing choice by name. A default
would decide which distribution actually gets built and leave the others to rot. If the catalog lists
one release, no other release can go unbuilt. A package that takes its list from the catalog then builds
every target for that release by its own name and declares no aliases. The list comes from
`catalog//:releases.bzl`, or from a package's `PACKAGE` file where the package declares its own, and
every image rule defaults to it. The failure mode of repeating it per target is a target that forgets:
being compatible while depending on something incompatible is an error rather than a skip. Buck can't
look up another package's targets while it loads a `BUCK` file, so the catalog lists its releases in a
`.bzl` file that tine loads.

The list of distributions is deliberately not a buckconfig. A distribution is a property of the image,
so it belongs in the declaration, where it is visible to `buck2 uquery`, can differ between two targets in
one build, and does not reconfigure the world when it changes. The `distribution` key of the `tine`
buckconfig section only sets which entry of a package's table a target's own name builds. A developer
who builds one distribution all day sets it in `.buckconfig.local`. The aliases build every distribution
whether the key is set or not, so CI does not depend on it.

### Image construction

The `image.layer` rule creates either an initial image from a package manager, or a derived image from
`parent`. `image.ImageInfo` carries the package manager, the box the image runs in, an ordered stack of
filesystem deltas, deferred tmpfiles snippets, and the canonical lazy package-database and SBOM artifacts. A
derived image inherits the construction configuration but declares fresh metadata for its completed stack, so
one composition cannot silently switch package sources or tooling environments between stages.

Each `image.layer` call applies one ordered operation sequence in one action and persists exactly one overlay
upper. A layer's `packages` and `package_sets` are rule attributes rather than operations: it installs them
as one request before any operation runs, while `run`, `copy`, `mkdir`, `symlink`, and `remove` mutate the
same root afterwards. A package set resolves its symbolic name through the image's package manager during
analysis and joins the concrete names in the same request, deduplicated. Installation is a property of the
layer rather than a position in it because a layer resolves one closure at analysis time over the stack below
it: a second install could only ever be solved blind to what the first one added, so there is nothing an
ordering could mean. Making it an attribute is also what lets operation sequences compose, since two lists
that both need packages no longer collide, and what lets a sequence name a package set and extra packages
together, which a set alone cannot express because its members are known only during analysis. Installing
against the result of an earlier install is a second layer, which is where a closure resolved over that
result becomes available. `copy` introduces a declared Buck artifact at an absolute image path, preserving
its position relative to the other operations. `run` executes the image's own tools in a chroot by default;
`chroot = False` instead executes box tooling with the image available at `/buildroot`. Its `env` argument
overlays variables on the box or image environment for that command. Package installation runs outside the
chroot. `copy`, `merge_os_release`, `mkdir`, `symlink`, `write_file` and `remove` run inside it, so ordinary
absolute symlinks resolve relative to the image. The `image.layer()` declaration macro recursively flattens
operation lists,
allowing reusable helpers to return ordered groups of operations. Every rule that accepts operations is
wrapped in a declaration macro that flattens them the same way, so a helper returning an ordered group
works identically in `image.layer()` and in a composition; the underlying attribute stays a flat, typed
list so Buck can track every embedded source dependency. An `image.install()` target carries `packages`
beside its operations, so a reusable target declares what it needs and `image.ops.install_from()` folds that
into the installing layer's own request.

`install_langs` narrows the install to the translated files of the named languages, and `install_docs`
drops documentation while keeping licenses. Neither ever enters the tree, so the package database records
them as not installed rather than claiming files that are absent. They belong to one `image.layer` operation
sequence, not the logical image's lifetime: an initrd needs neither, while the root filesystem it boots may
want both. Each package system implements them in its own driver. A sequence that installs nothing ignores
both, so a composition can forward them to a layer whose operations happen to skip installation.

An `image.layer` exposes its newly persisted delta, the full stack and canonical metadata through
`image.ImageInfo`, and the same metadata through lazy `[manifest]`, `[pkgdb]` and `[sbom]` subtargets.
`image.ImageSbomInfo` gives typed consumers only the two SBOM formats. Declaring a logical image declares
its metadata with it, because they are intrinsic metadata facets of every logical image: an
`image.ImageInfo` therefore always carries an SBOM and a UAPI.16 file manifest, and a package database
whenever the image has a package manager at all. The two describe different questions and neither
subsumes the other: an SBOM says what the image was built from, the manifest says what it ships, down to
the digest of every regular file. An artifact referenced by a provider remains lazy. Selecting either
SBOM format runs one shared scan; the default image build runs none of the metadata actions. Filesystem
materialization remains an explicit terminal operation, so logical image construction does not depend on
an archive or disk format.

Package installation and image tooling remain separate concerns:

- the bootstrap `package_manager` determines what native packages can be resolved;
- the image's box supplies every layer driver and terminal image tool, and runs the manager's resolver and
  installer;
- `local_repository` declares compatible package outputs and infers their package system from
  `LocalPackageInfo`; an image or a buildroot that installs from it materializes its repository metadata
  with the box it runs in;
- a derived package manager adds such repositories to a base manager's configured selection;
- a manager's `local_packages` instead selects locally built packages per install by runtime closure.

A project can therefore expose locally built packages without adding them to its OS release, or a package
manager may instead attach a branch's generated local-packages universe; worked examples of both
declarations are in [images.md](../user/images.md).

Each install operation then computes the runtime closure of its requested packages at analysis time over
the imported Requires/Provides metadata, builds exactly the locally built packages in that closure, and
offers them to the solver ahead of the upstream repositories. Requested capabilities without a local
provider continue to resolve upstream, so partially imported branches simply mix. `image.ImageInfo`
accumulates its layers' install specs and seeds every later closure with them, keeping lower-layer packages
locally backed in later solves. Unlike a static `local_repository`, only the packages an install actually
pulls in are built; the universe target itself never forces a package build.

Logical images and terminal outputs are separate rule families. Terminal rules merge the stack only when
needed. The catalog of terminal rules (`image.directory`, `image.uki`, `image.bootable`,
and `image.vm`) and the `image.archive`, `image.sysext`, `image.confext`, `image.extension`,
`image.portable`, and `image.disk` composition rules are documented in
[images.md](../user/images.md). All of them, with the operation helpers and conventional partition layouts,
are members of the `image` struct exported from `tine//image:defs.bzl`; that facade is the public API, and
the modules behind it are implementation structure. Every public facade a consuming project loads from
exports one namespace struct the same way: `box`, `cargo`, `go`, `git`, `package`, `package_system/rpm`,
`package_system/deb`, `package_system/pacman`, `distribution`, `http`, and `tests`. Every rule resolves its
drivers through one `ImageToolsInfo` bundle at `tine//image:tools` instead of a private attribute per driver.

The package database and SBOMs are supply-chain outputs read from the assembled image, never shipped in it.
Declaring any logical image declares both lazy facets, so compositions inherit them rather than repeating a
metadata step. Each package system captures its database in its native shape. SBOM scanning of the whole
tree additionally catches
packages no package manager knows about, such as Go modules bundled into ELF binaries.

Terminal rules leave the package database and other package state intact. An extension drops the database
from the paths the image's package system declares, because a merged extension must not replace the host's.
Image cleanup is an explicit, configurable layer so output formats do not silently alter image contents.
Every terminal driver receives the ordered layer stack and the deferred tmpfiles snippets of its image. An
extension receives only the snippets that it adds to its base, because a snippet of the base would create a
file of the base in the extension. The driver applies the snippets with `systemd-tmpfiles --root` before
reading or emitting image content; a missing tool is an error whenever finalization is needed. The directives
run against a disposable overlay upper and can create paths or restore modes and xattrs. Image-shipped
tmpfiles configuration is not applied implicitly; enabling it will be an explicit output option once its
single-UID/GID behavior is defined. Ownership and named ACL entries are deliberately unsupported: all archive
entries use uid/gid zero.

Tar uses deterministic PAX archives and stores Linux xattrs using `SCHILY.xattr.*` headers. The newc cpio
format has no general xattr representation. Tar/cpio entries are ordered and mtimes are clamped to the fixed
assembly epoch. The cpio reader/writer aligns regular-file payloads and uses `copy_file_range` when possible
so large archives can share extents on reflink-capable filesystems. `compression = "zstd"` compresses the
finished archive in the same action, so the uncompressed form never becomes a Buck artifact; zstd's
multi-threaded output is byte-identical to its single-threaded output, so this stays reproducible. The initrd
uses it, while the kernel-modules cpio that `uki.py` appends stays raw because the distribution already
ships each module compressed; the kernel unpacks the concatenation as independently compressed segments.

`declare_ddi()` in `ddi.bzl` deliberately distinguishes `definitions` from `partitions`. Definitions describe
new partitions to populate directly from `image.ImageInfo`: repart mounts the delta stack with a disposable
overlay upper instead of first copying a directory artifact. Partition inputs are `image.DdiInfo` outputs
from an earlier split call; their blocks are copied into the new disk with their resolved type and UUID
preserved. Calls emit a disk by default. `split = True` additionally exposes each newly defined partition
with normalized metadata alongside its block artifact; `disk = False` makes such a call partition-only. No
partial disk is passed between actions. The partition artifacts are portable, so `image.DdiInfo` carries no
box: repart and the format conversions use the destination `image.ImageInfo.box`, and `image.vm` uses the
tools box of the image's release. `image.directory` is an independent terminal view and is never an input to
repart.

Partition layouts are always explicit inputs; `image.disk` doesn't choose one
implicitly. The reusable conventional layouts are listed in [images.md](../user/images.md).

Partition labels may contain `{image_id}` and `{version}` placeholders. `image.disk()` renders them from its
own image identity, and unrendered placeholders fail the build. The usr-verity layouts carry such labels
to produce the `<id>_<version>[_verity[_sig]]` names that systemd-sysupdate A/B slot matching expects.
Rendered labels pass through `image.partition()` again, so GPT's 36-character label limit is enforced on the
final value.

Verity data, hash, and optional signature partitions are produced together in the split action. The root or
usr hash is an artifact because its value is known only after execution. `image.DdiInfo.root_hash` carries
an optional `image.RootHashInfo`, allowing `image.uki` to consume the hash without another top-level
provider. One split call may produce at most one such hash. This artifact boundary also ensures the final
disk contains exactly the partition bytes whose hash was embedded in the UKI. The hash is also available
as the disk's `roothash` subtarget. Signature partitions require an explicitly declared key and
certificate.

A bootable `image.disk()` composes:

```text
base initrd package image (zstd cpio)
              ├──────┐
root filesystem layer ──> identity layer ──> split /usr + verity ──> hash ──> versioned UKIs
                                │                                               │
                                └──────────────────────┬────────────────────────┘
                                                       v
                                              ESP layer
                                              │          │
                                              │          └─> terminal views and supply-chain artifacts
                                              └─> ESP + system partitions ─> bootable-image result
```

The identity layer stamps `IMAGE_ID` and `IMAGE_VERSION` into the image's os-release. It is a separate
thin layer so that a per-commit version string invalidates only the version-embedding artifacts below it,
never package installation.

With Secure Boot key material, the identity layer also signs the systemd-boot binary in place, as a
`.signed` sibling under `/usr/lib/systemd/boot/efi`. The signed binary must live in the image's own
`/usr`, covered by the verity root hash, not just on the ESP: the booted system's `bootctl update`
(`systemd-boot-update.service`) reinstalls the bootloader from that path after an OS update, and an
unsigned binary there would fail Secure Boot verification on the next reboot. `bootctl` prefers the
`.signed` sibling when populating the ESP. Only the UKI is built and signed outside the tree: its
command line embeds the root hash, so it can only exist after `/usr` is sealed, and it lives solely on
the ESP. The verity partition itself stays unsigned in this scheme: the signed UKI command line pins the
verity root hash, so its trust derives from the Secure Boot signature.

`image.initrd()` declares the conventional initrd as a target of its own: a package image with `/init`
pointing to systemd and `/etc/initrd-release` pointing to `/etc/os-release`, installing the release's
`initrd` package set, so family catalog policy supplies concrete native package names. Its `packages`,
`package_sets`, and `ops` are added to those defaults rather than replacing them, so extending an initrd
never means restating what it already does, and `install_docs` and `install_langs` invert the defaults an OS
image wants, since documentation and translations only cost an initrd boot memory. It generates before it
prunes, so the hardware database it ships is compiled from the sources it then drops. The rule archives the
image into the cpio itself, at the `compression` it declares, and returns both as `image.InitrdInfo`
alongside the image's own providers. That cpio omits the package database from the paths the image's
package system declares: nothing in an initrd resolves a dependency or verifies a package, and the kernel
unpacks the whole cpio into tmpfs, so shipping it would only cost boot memory. The `[manifest]`, `[pkgdb]`
and `[sbom]` views
read the tree rather than the archive, so they still describe the complete installed set.
A bootable `image.disk` consumes `image.InitrdInfo` rather than a bare `image.ImageInfo`, so the archive is
the initrd target's own output and its compression is declared where the initrd is. A composition given
no `initrd` declares `<name>.initrd` for itself through the same macro, inheriting its package manager,
version, and distribution; the default is therefore an ordinary target that can be built and inspected on
its own rather than an anonymous step inside the disk image. The composition publishes the provider it was
given from `[initrd]` and returns the same
instance directly, so both interfaces describe exactly the same initrd.

`uki.py` appends the kernel-modules cpio and runs `ukify`. On x86 it also packs the microcode the image
ships under `/usr/lib/firmware/{amd,intel}-ucode` into a `.ucode` section: one concatenated file per vendor
in an uncompressed cpio, which the stub hands the kernel ahead of the initrds so the early loader finds it.
Nothing configures this; installing a microcode package is what asks for it. Narrowing the blobs to the
CPU the build runs on is deliberately not offered, since reading that host is not hermetic. That cpio
carries the modules `initrd_modules`
selects, closed over their dependencies and their firmware with libkmod, which reads the image's own depmod
index and no configuration from the box; `/usr` keeps the full set for the booted system. A pattern
matches a trailing run of a module's path components; a leading slash anchors it at the modules root
instead, and a trailing slash takes everything below a directory. Selecting by regex is not offered, nor
is selecting whatever the build host has loaded, the latter because reading that host is not hermetic.
`image.DEFAULT_INITRD_MODULES` is the committed set a general-purpose initrd needs to find and open the
root it was built for. One list serves kernels that ship different sets of modules, so a pattern
matching nothing is reported rather than fatal, in the manifest the rule publishes beside the UKI. The
UKI is named
`<image_id>_<version>_<arch>.efi` from the image identity. For now an image holds exactly one kernel — the
name (and sysupdate's matching of it) could not distinguish more. If several kernels per image ever become
a requirement, add naming configuration to `image.uki()` to disambiguate them. The ESP layer copies the UKI
directory into `EFI/Linux` and includes the operations returned by `image.ops.install_systemd_boot()`. Those
create the ESP path, run the box's `bootctl` with its paths in the command environment, and remove the
random seed. The final repart action creates the ESP while copying the previously split system partitions
into the same disk. It splits nothing itself: the system partitions arrive already split, and nothing updates
the ESP as a partition. The default system partition is a compressed EROFS `/usr` protected by
dm-verity; the generated `usrhash=` is embedded in every UKI. A caller places other files on the ESP, such
as device trees or bootloader entries, with `image.ops.copy()` operations to `/boot` or `/efi` in `ops`. The
system partitions leave out what `/boot` and `/efi` contain, so the files are only on the ESP, also with a
root partition that copies `/`.

Kernel command lines remain lists of arguments through the Starlark API and driver invocation. The UKI
driver appends any generated verity hash and joins the arguments only when writing ukify's command-line file.
Boot profiles become small PE binaries of `.profile` and `.cmdline` sections, built against the image's
addon stub and joined into every UKI; a profile's arguments extend the shared base command line (including
the verity hash), and kernel arguments are last-wins, so profiles can also override it.

A composed raw disk can be re-encoded into distributable formats without rebuilding it: the `[qcow2]`,
`[raw.zst]` and `[vmdk]` subtargets of a disk run `qemu-img` or `zstd` in the image's box. These are
alternative encodings of the same disk and remain subtargets rather than default outputs.

#### Bootable-image result

Bootability and output format remain independent terminal capabilities, but the artifacts declared by one
`image.disk()` invocation form one concrete product. The composition publishes one target at
the requested name. Its default output and `image.DdiInfo.disk` are the raw disk; its other terminal
views,
supply-chain metadata, and constituents are lazy subtargets:

```text
//examples/image:boot-demo
├── [uki]
├── [directory]
├── [qcow2]
├── [raw.zst]
├── [manifest]
├── [pkgdb]
├── [sbom]
│   ├── [spdx]
│   └── [cyclonedx]
├── [initrd]                    # default output: zstd cpio
│   ├── [manifest]
│   ├── [pkgdb]
│   └── [sbom]
│       ├── [spdx]
│       └── [cyclonedx]
├── [roothash]
└── [partitions]
    ├── [usr]
    └── [usr-verity]
```

Typed providers are the composition API; subtargets are the command-line interface. There is no aggregate
bootable-image provider. The rule returns its completed `image.ImageInfo`, `image.DdiInfo`,
`image.InitrdInfo`, `image.ImageDirectoryInfo`, and `image.UkiInfo` independently. `image.DdiInfo`
holds the composed raw disk, its independent partitions, and optional `image.RootHashInfo`, rather than
exposing separate top-level providers for these facets.
Its qcow2 and compressed-raw encodings are reachable only through their subtargets, each publishing one
`image.DiskConversionInfo`: a single provider type cannot appear twice in one result, and "the conversion"
of a target that has several is ambiguous anyway. Every other constituent subtarget publishes the same
provider instance as the main target. This lets a consumer request exactly the capability it needs without
fields duplicating another provider's data.
Storing an artifact in a provider does not build it. Optional actions run only when a consumer uses the
corresponding artifact or a user selects its subtarget. They must not appear in the result's
`DefaultInfo.default_outputs` or `DefaultInfo.other_outputs`. SPDX and CycloneDX still come from one scan,
so requesting either format runs the same SBOM action. A metadata view publishes itself the same way a
conversion does: `[sbom]` and `[pkgdb]` carry an `image.PublishedInfo` naming them after the image they
describe, so a release gathers what a scanner reads while an image's own published set stays what an
update transfers. Every `image.PublishedInfo` must name its image and give each file a kind, and may give
the version of its image. `image.artifacts` writes the image, the kinds and the version into the release's
description.

`image.disk` is one rule that owns the complete composition action graph. Provider-oriented action
helpers are shared with the standalone `image.layer`, `image.uki`, and terminal-format
rules, so the composition reuses their implementations without declaring private sibling targets or
forwarding through a result rule. This aggregation remains scoped to the concrete bootable-image product;
it does not restore a generic `image_result` around every logical `image.ImageInfo`.

`image.archive`, `image.portable` and the extension rules follow the same ownership model on a smaller
graph: one rule declares the logical image with its lazy package-database/SBOM views, and its terminal
archive or DDI. Each target returns `image.ImageInfo` alongside its terminal provider, so consumers never
depend on generated `.layer`, `.pkgdb`, or `.sbom` labels. The standalone terminal rules share the same
provider-oriented declaration functions.

The disk, directory, package database, and SBOM derive from the same ESP layer. The initrd is a second
logical image with its own package closure, declared and archived by its own target, so the composition
republishes the package database and SBOM that image carries rather than deriving anything. The `[initrd]`
subtarget exposes only the encompassing `image.InitrdInfo` as its typed contract, plus the image's metadata
as nested subtargets. The main target returns that same `image.InitrdInfo` directly. Supplying a custom
initrd therefore requires only one regular target and never a family of conventionally named siblings. VM
runners remain separate targets because execution policy and credentials are behavior, not facets of the
image artifact.

The standalone `bootable` rule extracts semantic boot artifacts lazily from a completed logical image
rather than forwarding whichever intermediate target created them; it earns its keep on images whose
kernels arrive through package installation rather than a composition-built UKI. A boot-specific selector
chooses the newest valid UKI by its embedded kernel release and writes a generic artifact manifest. The
image artifact driver only extracts a named path or PE section from that manifest. Without a UKI, the
selector chooses the newest standalone kernel. In either case, bootability requires an initrd matching that
exact release. A UKI remains an optional extraction: requesting it fails if the selected image has only
standalone artifacts. Selecting the `[directory]` subtarget does not assemble the disk, and repart never
materializes the directory.

Repart derives stable UUID seeds from target identity and logical configuration; callers can override them
explicitly. VM runners are declared separately from disk composition. Their execution box and runtime
policy are passed to `image_vm` rather than baked into the disk provider or image.

The image build tools live in the box and are not installed into the image merely to build it. Chrooted
`run` operations intentionally use the image's own binaries; non-chrooted runs explicitly use box tools
against `/buildroot`.

### Generating derived state

A package installs configuration that describes state rather than carrying it: module directories with no
index, `hwdb.d` with no compiled database, a locale list with no archive. On a running system scriptlets and
boot-time units produce it; an image being assembled is neither, so tine builds it explicitly. Each generator
is an operation of its own rather than a step of one pass, so it is positioned where the image wants it,
configured on its own terms, and adopted one at a time; what it writes persists into the delta that captures
it, so it is built once and cached with the layer rather than repeated by every terminal output. An `image`
gets the content its operations ask for and nothing else, which is the same reason terminal rules leave
package state alone.

Each generator is a no-op for an image that carries none of what it acts on, so none of them needs
per-distribution knowledge: `image.ops.depmod()` is silent without kernels, `image.ops.hwdb()` without
`hwdb.d`, `image.ops.locale_gen()` without an `/etc/locale.gen` asking for something. That is what lets the
compositions, which declare a whole product rather than one layer, end every image they build with all three
at their defaults without knowing what went into it. A generator named in `ops` replaces the composition's
copy instead of adding a second, so placing or configuring one stays possible. The extension rules are the
exception that generates nothing: an extension merges onto a system it does not own. A database generated in
the extension would replace the database of that system in the merged tree, and would go stale when that
system changes, exactly as its package database would.

Their tools are the box's, applied to the mounted image through the `--root` interface systemd gives them,
which is the same relationship every other layer driver has to the image and is what lets an image that
installs no systemd still be finalized. `depmod` runs from the box as well. `--basedir` does not relocate
the directories it reads its search order from, so the driver reads the image's `depmod.d` files inside the
image's root and passes a copy with `--config`. `depmod` would look in the box for an `external` directory
and for the target of an absolute module symlink. The driver therefore fails the build when the image's
configuration lists an `external` directory or a module is an absolute symlink. `locale-gen` is a
distribution's script around `localedef`, so the driver runs the same loop itself. It runs the box's
`localedef` with `--prefix` and `I18NPATH` set to directories in the image, and it finds each locale's
source and charmap inside the image's root. The layer runs in the tools box of the image's own release,
which resolves its packages from the release's repositories, its additional repositories included. An image
whose kmod and glibc come from those repositories gets an index and a locale archive in the formats it
reads. The `additional_repositories` and `local_packages` of a package manager do not reach the tools box,
so an image that installs kmod or glibc from them is finalized with the release's versions.

### Reproducibility and caching

Reproducibility is both a release property and a caching requirement. Current mechanisms include:

- repository metadata, package bytes, and source archives pinned by SHA-256;
- generated or committed box transactions containing repository/package identities;
- a fixed assembly `SOURCE_DATE_EPOCH` for roots that should be shared across consumers;
- per-package source date epochs for build output timestamps and headers;
- a fixed build host and frozen release-numbering macros;
- parked package databases and scrubbed package-manager caches;
- sorted transaction JSON, archive entries, source staging, and output collection;
- target/configuration-derived partition UUID seeding and normalized archive metadata;
- content-based paths for repository-owned package artifacts.

These measures make action-cache reuse meaningful and prepare the graph for remote execution. The repository
does not yet run a systematic build-twice reproducibility audit, and raw filesystem image byte-for-byte
reproducibility still needs dedicated validation.

Sharing those results between machines is a further, per-action decision. Uploading a result trades build
time against download size: assembling an image writes gigabytes in seconds, so fetching one from the
cache takes longer than building it locally and wastes a lot of bandwidth. Worthwhile actions are slow
and have small output: compilers (cargo, go, RPM builds) and package dependency solvers. These enable
`allow_cache_upload`, except where an action keeps its previous outputs: an incremental rerun drops
buck's strong "this exact input produces this exact output" guarantee, so it must not go into the shared
cache.

**An action only hits if every action above it is cached or byte-reproducible.** Adding an expensive
action to the list buys nothing while something upstream of it produces different bytes each run.

`buck log show` reports one `ActionExecution` record per action, carrying the `wall_time_us` and
`output_size` that decide whether it is worth caching, and the output digests that say whether it is
reproducible across two builds.

## Decision record

The following decisions remain the rationale for the current design. Detailed source-code research that led
to them belongs in commit history or focused notes; this section records the durable conclusion.

### Use Buck2 as the graph and cache

Buck2 was chosen because package builds benefit from content-addressed artifacts, lazy action execution,
sub-target providers for a build's binary outputs, and a test protocol that can later host the Barrage
executor. Its lack of an implicit local sandbox also lets tine use the same boundary locally and on
future remote workers. Bazel's broader language-rule ecosystem mattered less than these properties
for a package-heavy repository.

Buck cannot add ordinary target dependencies discovered from an action output. Dynamic actions may select
among declared inputs but cannot turn newly discovered BuildRequires into a new static graph. Therefore
dependency discovery/import must produce committed or analysis-time lock data before normal builds.

### Commit repository snapshots and selectively freeze boxes

Repository snapshots are committed so normal resolution remains independent of live repository state and
reviewable. Boxes with a predecessor resolve a cacheable transaction from those pins during their build.
Committing a box transaction is an explicit freeze operation for bootstrap roots, releases, or other
boxes that must remain stable across repository snapshot updates. Unlike an ordinary language lockfile, a
frozen box transaction also retains the transport for packages needed after a rolling repository
advances.

### Let repositories own upstream packages

Putting downloads in each closure duplicated ownership and left derived operations without a stable home.
The authoritative named pool instead gives every upstream package one action owner per repository. The
current snapshot defines available packages, while optional committed box locks retain older packages
required by frozen transactions. Closures select artifacts; they do not fetch or transform them.

### Separate package system, OS release, package manager, and buildroot

The former distribution object bundled repository membership, box tooling, and buildroot policy. That
made optional repositories awkward, implied that a box had to match every target release it operated
on, and provided no clean place for request-specific local repositories.

The current vocabulary follows the actual responsibilities:

- the package system defines operations;
- the repository universe defines membership and normal enablement policy;
- the OS release defines identity, selects a repository universe, and may provide a box's base;
- the package manager defines one exact solve universe; derived managers compose additional repositories and
  priority overrides without changing their inherited release;
- the buildroot materializes the shared base packages.

This is also why a release is not called a distribution target: two releases of one family are separate
release identities, while repositories and boxes can be reused across those identities when compatible.

### Keep native package managers homogeneous

Every repository universe and package manager belongs to one native package system. Two native systems must
not participate in one dependency solve, and neither must a third. Adding the second one bore this
out: the neutral rules needed no notion of which system they were driving, and the two never meet in
a solve because a universe, a release, and a manager each belong to exactly one. Supplemental content
systems such as Flatpak may eventually coexist with a native one in an image, but compatibility rules
are deliberately deferred until that is a real requirement. `PackageSystemInfo` is for native binary
package ecosystems, not every possible image content type.

### Separate a box's base release from its target releases

A box is a tools root with a concrete OS userspace, so its base release records where its packages and
identity came from. That does not make it part of a package manager's target OS identity. A package manager
names no box, so every image of a release shares the tools box of that release rather than one tooling root
per package manager.

### Use one sandbox boundary and let drivers mount target roots

The box userspace must be pinned, the host environment must not leak into builds, and package scriptlets
need unprivileged fakeroot semantics. A sandbox of tine's own supplies those properties without host
package tooling, a build-chroot manager, bwrap, or a second nested sandbox. Drivers mount their own
target roots because install, build, image, pack, and disk actions need different layouts.

### Pass drivers one JSON spec

A rule describes an action to its driver as a single JSON spec written with `write_json`, invoked as
`<driver> --spec <spec.json>`, rather than as a command line. Buck resolves artifact paths inside the spec,
so declared outputs are named there exactly like inputs and neither has to be flattened into repeated
options or positional triples. Structured configuration (layer operations, partition definitions, boot
profiles, repository selections, subpackage outputs) stays structured, and a driver reads the schema its
rule owns instead of revalidating an argument grammar. A driver that invokes another driver does the same:
the image layer driver writes an install spec for the package installer.

Two exceptions are deliberate. A planner keeps its verb on the command line, since each verb has its own
spec schema. Both it and the snapshot driver keep `--out` there too: the box lock and `[snapshot]` run
targets let a caller name the file to write, or `-` for stdout when it has nowhere to write one, and one
calling convention per driver beats splitting the destination by verb.

### Store image layers as deltas

Copying a complete root for every image step scales with total image size rather than change size. Ordered
overlay deltas let child layers and terminal outputs reuse their ancestors. Encoding whiteouts and opaque
directories as regular files keeps those deltas compatible with Buck's artifact/CAS model.

### Prefer exact transactions over package-manager network access

Resolution uses pinned local repository metadata; installation consumes an exact directory of already
selected packages.
This keeps network out of build actions, makes the transaction an inspectable early-cutoff boundary, and
separates “which packages?” from “apply these packages and scriptlets.” Weak dependencies are disabled to
match buildroot policy and avoid unreviewed closure growth.

### Build each source package once and expose subpackages

One build invocation naturally emits every binary subpackage and the source package. Running it once avoids
repeated work and inconsistent sibling outputs. Buck sub-targets give downstream packages addressable binary
outputs without pretending each subpackage is a separate build action.

### Keep static local repositories and closure-selected local packages separate

`local_repository` and a manager's `local_packages` both prefer locally built packages over upstream and
share the per-install `extra` materialization path, but they are deliberately not unified. A
`local_repository` publishes an explicit, hand-listed set and forces those packages to build; it suits a
project exposing a few of its own packages. `LocalPackageUniverseInfo` instead describes a whole imported
branch and builds only the packages an install's runtime closure actually pulls in, so attaching the
universe never forces the branch to build. Collapsing them would either force building an entire branch or
push closure computation into every static repository, so both remain until the planned per-package
source/prebuilt provider model (see Roadmap) subsumes them.

### Use bind mounts for out-of-tree content

The previous implementation declared the checkout as an external git cell, which Buck fetched like any
other pinned repository. As a result, Buck could only see committed changes. Bind mounting the checkout
at the cell root exposes the working copy, preserves file watching, and keeps Buck's declared paths
unchanged. This only works on Linux with unprivileged user namespaces and only when the build runs through
`bin/tine`; a build that bypasses it uses the checked-in path. Those constraints are acceptable for a local
development feature.

Deriving the Buck2 isolation directory from the mount set would keep those sets fully apart, but each
set would also get a separate `buck-out`. Keeping one isolation directory preserves build outputs when
switching between checkouts; with every command running its own daemon, nothing else tells the mount
sets apart, and the `TINE_MOUNTS` variable only names the one a nested invocation inherited.

### Require Linux 6.15 for sandbox and project mounts

The sandbox builds every filesystem as a detached mount, assembles its root as a detached tmpfs, and
attaches the result once. It uses `open_tree_attr()`, mounting onto a detached mount, and cloning a mount
out of a detached mount from the [Linux 6.15 mount API][mount-api-6.15]. The box's directory binds are
clones out of the detached tools tree, and `File` clones its file out of a private tmpfs. Overlay layer
file descriptors require [Linux 6.13 or newer][overlay-layer-fds].

An older kernel floor would require attaching the root first and keeping staging mount points for the
tools tree and `File`. Kernels without overlay layer file descriptors would also need escaped path
strings. The 6.15 floor keeps one mount mechanism without staging directories. Hosts with older kernels
need a newer kernel. `tine mount` uses the same mount operations and requires the same version.

The host root must also support `pivot_root()`. Kernels that [cannot pivot their initramfs
root][initramfs-pivot] require booting from another root filesystem.

[mount-api-6.15]: https://github.com/torvalds/linux/blob/v6.15/fs/namespace.c
[overlay-layer-fds]:
  https://docs.kernel.org/filesystems/overlayfs.html#specifying-layers-via-file-descriptors
[initramfs-pivot]: https://docs.kernel.org/6.15/filesystems/ramfs-rootfs-initramfs.html#what-is-initramfs

## Operating the current system

Host requirements, the wrapper commands, and representative smoke builds are documented in
[images.md](../user/images.md).

## Current limitations

These are properties of the implementation today, not merely ideas for future optimization. Limitations that
belong to one package system are listed in its own section instead:

- Buck preserves `buck-out` across mount changes. This is safe for hermetic actions because source
  changes produce new input digests. An action that reads an undeclared input can still reuse stale output
  after a mount change.
- Source builds and checkouts read their source directory as it is on disk, including the files that Buck
  ignores. An ignored file can change what a build produces without changing the action key.
- Two native package systems are implemented, and only one of them can build packages.
- A transaction describes packages to add. An install that would have to remove or replace something a
  lower layer carries is refused rather than expressed.
- Image installs select source-built packages through the imported-metadata runtime closure of
  `local_packages`. The walk follows local-to-local edges only, so a local package reachable only through
  an upstream intermediate silently resolves upstream, and there is still no per-package source/prebuilt
  choice under one shared version pin.
- Rust source builds cover crates.io and git sources; another registry is rejected. A git dependency's
  integrity rests on the commit hash the lock records: SHA-1 for ordinary repositories, which is weaker
  than the SHA-256 pinning everything else here uses.
- `cargo-auditable` is a pinned upstream release binary rather than a source-built one, so the Rust build
  path is not itself part of the source-trust chain.
- Four build steps reach the network instead of downloading content with a recorded byte hash: Rust Git
  dependencies use commit hashes from the lock file, project fetches use the commit passed to
  `git.fetch()`, the RPM packaging fetch uses the commit in the pin file of `in_place_packaging`, and Go
  verifies module downloads against the committed `go.sum`.
- Crate downloads carry no recorded size, so Buck learns it from an HTTP HEAD whenever a download action
  executes. A cold daemon therefore needs the network even when every crate is already cached.
- Repository metadata is trusted on first use: neither Fedora's GA trees nor the pinned mirrors serve a
  signed `repomd.xml`, so a substituted one at refresh time could select other validly signed packages.
  Reviewing the snapshot diff is the check for that.
- A root box verifies its packages with the `rpmkeys` of its own unverified stage1, so a tampered rpm
  package in the seed could defeat the check for that box; every other box and every image verifies with a
  predecessor. Arch packages are not verified at all.
- Archive ownership is intentionally normalized to uid/gid zero. Capabilities, xattrs, and SELinux labels do
  not survive as Buck directory metadata; deferred tmpfiles can restore xattrs at terminal assembly, and tar
  preserves them in PAX headers, but newc cpio cannot represent general xattrs.
- Directory image output cannot represent backslashes in names; archive outputs should be used instead.
- The default `/usr`-only disk has a volatile root, so the `/etc` a build writes is not what such an image
  boots with; first-boot defaults come from credentials instead. Package and authored state outside `/usr`
  is not yet translated into factory defaults or another persistent partition. How much `/etc` such an
  image boots with therefore depends on the distribution: Fedora's and Arch's systemd ship upstream's
  `etc.conf`, whose `L` lines recreate `/etc/os-release` and its neighbours, while Debian drops it and
  populates `/etc` from packages instead.
- The generators cover the databases under `/usr`. System users, volatile files and directories, and unit
  presets are left to the boot-time units systemd ships for them, so an image whose `/etc` is created at
  first boot gets them then and one that ships a populated `/etc` does not get them at all.
- Bootable images currently disable SELinux.
- Remote execution, Barrage integration, release publishing, and systematic reproducibility audits are not
  wired into CI.

## Roadmap

The roadmap is organized by architectural capability rather than old numbered phases. Ordering within a
section is approximate and should follow the next concrete product need.

### Package graph and self-hosting

1. Replace the current metadata intersection with a generated package lock that records precise direct
   BuildRequires and binary/runtime relationships. Use the package system's dynamic BuildRequires protocol
   for packages that generate requirements while preparing their sources.
2. Introduce the source/prebuilt package-provider model only when an image or buildroot needs to choose
   backing per package. Preserve one coherent version pin so upstream and source-built variants have the
   same dependency graph.
3. Model runtime closures at binary-subpackage granularity and make debuginfo/debugsource outputs explicit
   where consumers or publishing require them.
4. Extend bootstrap extraction to a newer package format when a pinned repository requires it.
5. Decide and implement build-time test policy. Because successful build scratch is discarded, checks most
   likely belong in the primary build action with per-package opt-outs for broken or prohibitively
   expensive suites.

The durable self-hosting rule remains: invoked build tools may come from the pinned seed, while libraries
linked into shipped outputs should come from source-built packages once their graph is available. Cycles
must be explicit; silently pretending a cyclic source graph is acyclic is not acceptable.

### Supply-chain authenticity and release output

Upstream authenticity for rpm follows the catalog pinning and package pool sections: a repository declares
its signing keys by fingerprint, refresh fetches the files once, and a consumer with a box verifies each
package against them as it is selected, a box using its predecessor. What remains:

1. A root box has no predecessor and verifies with its own stage1. Closing that needs a verifier that did
   not come out of the served packages: a host-side check of the header signature in `rpmfile.py` would
   do, since Fedora signs with one RSA/SHA-256 key per release.
2. Arch verifies through the box's `archlinux-keyring`, so a packager key the catalog's box predates is
   refused until the box lock is refreshed; a keyring taken from the pinned repository itself would need
   verifying first, by the previous one, which is the same bootstrap pacman has.
3. rpm and alpm repository metadata stays unsigned upstream; the snapshot diff is its review. Debian's
   is the one signed thing, and its verifier starts from it.

A later release pipeline needs repository composition, package-group metadata, source/debuginfo publication
policy, provenance/attestations, and signing. Secure Boot signing should use deterministic RSA PKCS#1 v1.5
without timestamps. Development keys are declared, cacheable inputs; production builds sign with a key held
outside the build, addressed by URI over a PKCS#11 socket, see
[signing-pkcs11.md](../user/signing-pkcs11.md).

### Image hardening and formats

Near-term image gaps are:

- offline SELinux labeling instead of `selinux=0`, as one more generator;
- build-time `systemd-sysusers`, `systemd-tmpfiles` and `systemctl preset-all`, once their single-UID/GID
  and volatile-`/etc` behavior is settled;
- deterministic ext4/FAT byte-level validation and any required normalization;
- OCI, ESP, and other terminal formats as real consumers require them;
- richer ordered operations for setting file metadata directly;
- deciding whether package installation and image tooling eventually need distinct compatible boxes.

The layer model should remain ordered operations captured as deltas. A provides/requires feature solver is
unnecessary unless real composition requirements appear.

### Scale, configuration, and testing

- Configure remote cache/execution only after the local action graph and host contract are stable. Box
  roots and ordinary filesystem artifacts are intended to be CAS inputs; relaxed VM actions remain local.
- Add build-twice reproducibility audits because early cutoff is useful only when rebuilt outputs are
  byte-identical. Track known exceptions explicitly rather than weakening all comparisons.
- Integrate Barrage through Buck2's external test executor so many image/integration tests can share one
  streamed process while still reporting per-test results.
- Replace the pinned `cargo-auditable` binary with a source-built one once that no longer depends on
  itself existing, and map commits to tarballs for whatever forge a dependency turns up on next. Add
  C/C++ equivalents only when in-repository builds need them.
- Define the upstream-update workflow: import upstream changes, rebase local patches, refresh snapshots and
  generated metadata, and verify that version skew has not invalidated source/upstream interchangeability.

## Reference points

Useful implementation entry points:

- `package/{system,repository,release,manager,solver,buildroot,install}.bzl` and
  `package/{href,installer,transaction}.py`
- each package system's `rules.bzl` and drivers under `package_system/`, listed in its own section above
- `box/{build,runtime}.bzl`, `box/sandbox.py`, and `rootfs/rootfs.py`
- `image/{image,compose,defs,sign,vm,archive,boot,ddi,extension}.bzl`
- `cargo/{defs,rules,vendor}.bzl` and `cargo/{lock,vendor,build}.py`
- `go/rules.bzl` and `go/{fetch,build}.py`
- `tools/catalog.py` and `catalog/BUCK`
- the generated `packages/*/*/BUCK` and the importer-facing Starlark that validates it

External projects that informed the design:

- Buck2 for action/dynamic-dependency semantics, sub-targets, content-based paths, and test execution;
- the native package managers tine drives, for build, resolution, transaction, and signature behavior;
- mkosi/mkosi-sandbox for user-namespace isolation, root mounting, UKIs, and repart-based images;
- Barrage for the planned streamed integration-test executor;
- Siguldry for a possible production PKCS#11 signing boundary.
