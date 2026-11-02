<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Maintaining packages with the importer

The importer maintains a (partial) downstream derivative distribution in a monorepo: it mirrors upstream
distribution dist-gits (currently Fedora or CentOS) and maintains local package deltas. Package sources live
in the OS.git monorepo that vendors this `tine` cell, under `packages/`_distro_`/`_branch_`/`_srcpkg_ (for
example `packages/fedora/f44/glibc`), with pristine imports on the separate `upstream-rpm` branch. The branch
and metadata layout, the design principles, and the rebuild strategy behind all of this are documented in
[packages.md](../design/packages.md).

Run the tool as `tine buck run tine//tools:importer -- <verb> …`: that executes it in the
`tine//tools:dev.box` environment (host identity, network, and cwd; the box's pinned rpm/git
toolchain), so the host needs no rpm tooling installed. Verbs that only use git also work by executing the
script directly.

## Operations

Operations on the `upstream-rpm` branch to import/fetch upstream dist-git changes into `OS.git`:

 - `import-upstream pkgname distro branch`: new package from `fedora` or `centos`
 - `update-upstreams`: check for any new upstream dist-git commits for all currently imported packages and
   pull them in

Operations on the `main` branch to maintain the downstream packages:

 - `import pkgname [distro branch]`: copies `packages/…`_pkgname_`{/,.json}` from `upstream-rpm` branch
   into main, as a single commit. If there are multiple imports, you have to specify distro and branch to
   disambiguate.
 - `update pkgname`: applies all new upstream commits on top of current state, keeping our local
   modifications.
     * for unmodified local package this always works, file content remains identical between up- and
       downstream
     * for modified local package this may result in conflicts (see `sync` below)
 - `update-all`: Run `update pkgname` for all currently imported packages; i.e. keeps individual
   per-package commits, but will just result in one branch/PR with the whole update batch. That (1) groups
   together updates that were published to Fedora in a single batch, (2) avoids unnecessarily many CI
   runs, and (3) retains bisectability of package updates.
 - `rpm-metadata [--distro … --branch …] pkgname rpm [rpm...]`: Recompute `pkgname.json` from locally
   built rpms. Meant to be run by the build system for a package import/update branch/PR.
 - `sync pkgname`: `update pkgname` for a local delta we no longer want to carry: upstream adopted
   it, or we dropped the requirement. Same replay, except that the package directory is taken from
   upstream wholesale instead of merged into ours, so our changes are gone and nothing can conflict. The
   discard rides along in the upstream commit; if we are already on the latest upstream commit, it
   becomes a commit of its own.
 - `diff pkgname`: show diff between the `upstream-rpm` and `main` versions of `packages/…/pkgname/`
   (ignores metadata differences)
 - `srpm pkgname`: assemble the `.src.rpm`: freeze the `%autorelease`/`%autochangelog` macros, fetch the
   sources from the lookaside cache, and run `rpmbuild -bs`. Can then be locally built with `mock`, and
   later consumed by the production build system.
 - `mockbuild pkgname`: Build `pkgname` using `mock`, in the chroot config that matches its import source
   (e.g. `fedora-rawhide-x86_64`). Developer tool for validating changes sent to Fedora (production builds
   happen with buck).
 - `rebuild pkgname reasonpkg-version-release`: Generate an automated "pkgname: Rebuild against
   reasonpkg-version-release" commit
 - `list`: table with all rpms, local and upstream version/release (a package with no upstream is shown as
   `native`), and modification status
 - `check [start-ref]`: Validate consistency of all commits (optionally, starting from given ref); will
   run in all PRs

## Initialization

The importer names a package branch by its upstream distro (`fedora`, `centos`) and branch (`f44`,
`c10s`): the generated `packages/`_distro_`/`_branch_`/BUCK` builds against abstract
`//buildroots/`_distro_`:`_branch_ targets. These names need to be mapped to actual catalogs (see
"Concepts" in [images.md](images.md)). In your OS.git, create these aliases in
`buildroots/`_distro_`/BUCK`:

```Starlark
alias(
    name = "rawhide",
    actual = "catalog//:fedora.rawhide.buildroot",
    visibility = ["PUBLIC"],
)
```

We recommend that the OS declares its own `catalog` cell. The OS then advances its repository pins in
its own commits, instead of whenever it updates tine. Without its own cell, `catalog//` is tine's catalog.
See "Concepts" in [images.md](images.md) for how to declare one.

To use locally built packages in an image build, its package manager has to attach the branch's
`//packages/`_distro_`/`_branch_`:_local_packages` universe. See `local_packages` in
[images.md](images.md).

## Release and changelog conventions

 - Local modifications don't modify `%changelog`: We document changes in git, this avoids unnecessary
   merge conflicts
 - Local commits (i.e. not from imports) increase `Release:` by 0.1 to avoid colliding with
   Fedora/CentOS/other upstream's namespaces. Note: Hummingbird already does that, so if we modify a
   Hummingbird import, it will have to be bumped by 0.0.1. Consistency checks enforce this.
     * E.g. `glibc-2.43-6.fc44.x86_64` in Fedora → unmodified import builds as
       `glibc-2.43-6.fc44aos.x86_64`, next modifications are `*-2.43-6.1.fc44aos.*`, `-6.2.fc44aos.*`,
       etc.
     * Hummingbird import of `openssl-3.5.6-0.3.hum1.x86_64` gets imported as `*-3.5.6-0.3.hum1aos.*`,
       and next modification is `*-0.3.1.hum1aos.*`
     * The common rule is: "append `.1` for a modification of a previously unmodified package" and
       "increase last component for a modified one"
 - Packages using `%autorelease` need no manual bump; the release value is derived from the imported
   metadata and the local commit count (see the `srpm` operation details in
   [packages.md](../design/packages.md)).
 - Builds use a downstream-specific `%dist` tag: `aos` (this documentation's placeholder, see
   [packages.md](../design/packages.md)) appended to the upstream dist tag, e.g. `.fc44aos`; the rationale is
   recorded in the design principles in [packages.md](../design/packages.md).

## Making local package modifications

The workflow for changing a package downstream, like adding a patch or tweaking the spec:

 1. Edit the package in `packages/…/pkgname/`: modify the spec, add patch files, etc. There is no tool
    verb for this, it is a plain git change. Don't touch `%changelog` (git documents the change), and bump
    `Release:` per the rules above unless the package uses `%autorelease`.
 2. If the change introduces new `BuildRequires:`, add them to `srcpkg.json`'s `build_requires` map
    manually. This is unavoidable: the buck build system installs the buildroot from the metadata, it does
    not dynamically resolve `BuildRequires:` from the spec.
 3. Build the package locally with the buck build system, or possibly `mockbuild pkgname` (see below).
 4. Recompute the metadata from the built rpms with `rpm-metadata pkgname _build/*.rpm`. This picks up all
    unpredictable metadata changes to relationships, added/removed files, or binary rpm structure.
 5. Commit the package change together with the recomputed `srcpkg.json` in a single commit; `check`
    enforces this and the Release/`%changelog` conventions.

The canonical `srcpkg.json` comes from the buck build (the post-build recompute in the PR, see "Operation
details" in [packages.md](../design/packages.md)). A `mockbuild`-based recompute of the same change
likely has changes in the `/usr/lib/.build-id/…` file lists: the GNU build-id is a content hash of each
built binary, and mock's buildroot resolves the live distro repos instead of buck's pinned snapshot, so
the binaries are not bit-identical between the two.

## Changing import source

This might happen sometimes if we e.g. decide to move a package from Fedora to CentOS, or more
commonly to move from Fedora 44 to 45 or rawhide. As each distro/branch is its own path, this means
importing the package at the new coordinate (`import-upstream` + `import`) and pointing the build
configuration at it; the old coordinate can be dropped once nothing selects it. We may eventually
introduce a proper tool verb for that cleanup, but that can wait until the need actually arises.

## Branch build curation (`_properties.json`)

A `packages/`_distro_`/`_branch_`/` directory may carry a `_properties.json` alongside the per-package
`srcpkg.json` files. It holds hand-authored, branch-level build curation. The generated branch `BUCK`
loads it and passes its properties to `rpm.branch()`. Never edit the `@generated` `BUCK` by hand.

### Buildroot-only packages

The `buildroot_only_packages` property lists packages that exist *only* in the distribution's build
ecosystem (koji's buildroot repo) and are *never* shipped in the compose, so the seed cannot provide them
when the BuildRequires cycle-breaking drops an edge (background in [packages.md](../design/packages.md)):

```json
{ "buildroot_only_packages": ["glibc32"] }
```

Add a package when another package fails to build because a `BuildRequires` it needs is all three of:
(a) produced by one of our own packages, (b) never present in the seed/compose, and (c) inside a
BuildRequires cycle, so the lock drops the edge to it. `rpm.branch()` checks the list every time Buck loads
the branch. Loading fails when an entry has no local provider, because then the entry is wrong. Loading
also fails when the kept edges form a cycle, because then that cycle needs a staged bootstrap instead of a
kept edge.

### Seed-only packages

The opposite curation: the `seed_only_packages` property lists *source* packages whose builds we ship but
never want to build *against*. Their role in other package builds is tool use (a compiler, a signature
verifier, test data), not linkage. A `BuildRequires:` on anything a listed source package provides (`gcc`
covers gcc-c++ and libstdc++-devel, `kernel` covers kernel-devel) never becomes a dependency edge between
our packages; it is always resolved from the seed instead:

```json
{ "seed_only_packages": ["gcc"] }
```

The list is intentionally empty for now. See [packages.md](../design/packages.md) for what listing a
package changes, and [self-host-approaches.md](../design/self-host-approaches.md) for the analyzed
candidates and their trade-offs.

### Per-package rpmbuild options

Some specs offer build-trimming or other configuration through build conditionals (`%bcond`) or macros.
The `rpmbuild_options` property maps a package to the extra rpmbuild CLI options
(`--with`/`--without`/`--define`) its build is invoked with:

```json
{ "rpmbuild_options": { "gcc": ["--with=basic"] } }
```

`rpm-metadata` applies the same options when recomputing the package's srcpkg.json, so the recorded
subpackages and BuildRequires match what the build actually produces. An upstream import re-records the
package as koji built it (all subpackages); the next local build + `rpm-metadata` recompute converges it
back.

### Building from a source checkout

Each imported package has a `<package>.source` checkout slot beside its imported `<package>/` directory.
When populated, this tree replaces the source archives: rpmbuild skips `%prep`, so the checkout must
already contain any patches or generated files that step normally supplies. The build runs on an overlay
of the tree, where `rename(2)` on one of its directories fails with `EXDEV`, as it does in a container;
`mv` works, since it copies instead. The build reads the tree as it is on disk, including the files that
Buck ignores, and `%install` can copy them into the RPMs. See
[Building from a local checkout instead](git.md#building-from-a-local-checkout-instead).

Three per-package maps in `_properties.json` can adapt the build to that checkout. `in_place_specs` selects
an RPM spec by a path relative to the checkout root. The spec's directory also supplies its Source/Patch
files and relative includes. `in_place_packaging` takes the spec and its Source/Patch files from a dist-git
commit that the checkout pins; see [Taking the packaging from a pin in the
checkout](#taking-the-packaging-from-a-pin-in-the-checkout). A package cannot set both `in_place_specs` and
`in_place_packaging`. `in_place_rpmbuild_options` adds CLI options after the common `rpmbuild_options` and
any dev-mode defaults.

For example, suppose an imported `hello` package has `rpm/hello.spec` in its developer checkout and
supports `--with=upstream` for building from that tree. Its branch's `_properties.json` can contain:

```json
{
  "in_place_specs": { "hello": "rpm/hello.spec" },
  "in_place_rpmbuild_options": { "hello": ["--with=upstream"] }
}
```

Mount the checkout over the package's source slot:

```sh
tine mount add packages/fedora/rawhide/hello.source ~/Projects/hello
```

Without an `in_place_specs` or `in_place_packaging` entry, a source-tree build keeps the imported spec and
its declared sources. Without a source-tree override, the normal archive build ignores all three maps.
These overrides do not change the imported BuildRequires or subpackage list; `rpm-metadata` uses only the
common `rpmbuild_options`.

#### Taking the packaging from a pin in the checkout

A project such as systemd tests its main branch against a fixed commit of the distribution's dist-git
repository. To build with the spec and Source/Patch files of that commit instead of the imported ones, map
the package to the path of a pin file in its checkout:

```json
{ "in_place_packaging": { "systemd": "packaging.json" } }
```

The pin file is a JSON object that maps each distro to its pin. A branch under `packages/<distro>/` uses
the entry of its distro, so the branches of every distro can share one pin file. An entry is a JSON object
with the string keys `url`, `branch` and `commit`, and no other keys:

```json
{
  "fedora": {
    "url": "https://src.fedoraproject.org/rpms/systemd.git",
    "branch": "rawhide",
    "commit": "57cbcf979ce2dd872a871c59e87d2c65dfa996e6"
  },
  "centos": {
    "url": "https://src.fedoraproject.org/rpms/systemd.git",
    "branch": "rawhide",
    "commit": "57cbcf979ce2dd872a871c59e87d2c65dfa996e6"
  }
}
```

The build fails if the pin file has no entry for the branch's distro. It checks only that entry, so an entry
for another distro can have other keys. `commit` is a full commit hash in lowercase. The build fetches
`branch` from `url` over the network and fails unless `commit` is on that branch. It then builds
`<package>.spec` from that commit and copies every file at the top of the commit's tree into rpmbuild's
`SOURCES`. The imported spec and its declared sources are not used. The fetch reads only the pin file: an
edit elsewhere in the checkout does not fetch again, and a change to the pin file does, also when the change
is in the entry of another distro. A missing pin file fails the build with Buck's message that the path does
not exist in the checkout.

The pinned spec can declare other subpackages than the imported metadata. A build with `in_place_packaging`
prints the difference instead of failing on it, and emits the declared subpackages that it built. A build of
a declared sub-target that the pinned spec does not produce, such as `:systemd[systemd-imds]`, fails. An
image that installs a subpackage that the pinned spec does not produce gets the package of that name from
the distribution's repositories, if the package exists there and its dependencies allow it. Archive builds
and `in_place_specs` builds fail on any difference.

The buildroot still contains the imported BuildRequires. If the pinned spec requires a package that the
imported spec does not, rpmbuild fails with `Failed build dependencies:` until the package is imported again
at a commit with that BuildRequires.

The branch's generated `BUCK` passes `in_place_packaging` to the build only if its `rpm.branch()` call has
an `in_place_packaging` line and a `distro` line. `import`, and an `update` that takes new commits,
regenerate the `BUCK` with both lines.
