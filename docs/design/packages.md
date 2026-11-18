<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# rpm package import machinery

## Summary

`tools/importer.py` is a tool to maintain a (partial) downstream derivative distribution in a monorepo. It mirrors upstream distribution (currently Fedora or CentOS are supported) dist-gits, and maintains local package deltas.

The importer itself is part of `tine`. The actual packages are maintained in a target distribution/product monorepo which consumes `tine` as a cell; this is called `OS.git` in this document. (The importer therefore always runs from inside the consuming tree: `repo_root()` walks up to the `OS.git` root that holds `packages/`.)

`aos` is this document's placeholder for the downstream OS's own short identifier. It appears in the `packages/aos/` namespace and as the `%dist` suffix; a downstream project substitutes its own (`NATIVE_DIST` in `tools/importer.py`, and the `dist` attribute in `package_system/rpm/rules.bzl`).

This document records the design: branch layout, metadata, operation internals, and rebuild strategy. The user guide is [importer.md](../user/importer.md). It covers running the tool, the verb reference, the local-modification workflow, release conventions, changing import source, and `_properties.json` curation.

## Branch/Directory Layout

 - `OS.git`'s `upstream-rpm` branch contains the pristine imports and any future updates to these. I.e. this is a "dist-git mirror" from which we can then do efficient and local operations.
    * This is a standalone branch, no shared git history with `main`.
    * It contains a `packages/` directory with _distro_`/`_branch_`/`_srcpkg_ subdirectories, for example `packages/fedora/f44/glibc` or `packages/centos/c10s/openssl`.
    * It also contains corresponding `packages/`_distro_`/`_branch_`/`_srcpkg_`.json` metadata files (referred to as `srcpkg.json` throughout this doc) that map `srcpkg`'s binary packages to their relationships (BuildRequires, Requires, Recommends, Provides) and file lists. rpm computes these during package build (in particular, SONAME and pathname dependencies); they cannot be derived from the spec, but we need them for computing the downstream build system's dependencies.
    * Each commit corresponds *exactly* to one upstream dist-git commit, i.e. for one package. It has the upstream commit message plus an `X-Upstream-Commit:` git trailer which refers to the imported upstream commit SHA. This is necessary to translate per-package dist git repos to the `OS.git` monorepo structure. It contains the exact upstream commit plus the corresponding metadata (`srcpkg.json`) changes. An upstream commit that changes nothing downstream (like an unbuilt no-op mass rebuild, or a merge with no net change) is skipped entirely: nothing consumes it; see the `%autorelease` notes below.
    * There are *only* mechanical/automated commits on this, no human ones.

 - The "main" branch contains the packages from which the downstream OS is actually built.
   * It mirrors the per-distro-release `packages/` structure from the `upstream-rpm` branch.
   * This allows building downstream images from multiple distributions, e.g. a CentOS 10 based image next to a Fedora based one.
   * This allows shipping a "production" configuration which e.g. builds against fedora stable's `glibc` and `kernel`, and a "future" configuration which pulls in all rawhide packages (mostly for CI and learning about breakage as soon as possible).
   * Packages can be in sync, include local modifications, or even get removed (then the `upstream-rpm` branch should also be cleaned up, but that needs to happen async in e.g. a nightly workflow)
   * Might contain downstream specific packages, like a management CLI, with no upstream dist-git. I.e. `upstream-rpm` branch does *not* have that package. These live in `packages/aos/{latest,lts,...}/`_srcpkg_, one directory per downstream release stream.
   * A `packages/`_distro_`/`_branch_`/` directory may carry a `_properties.json` alongside the per-package `srcpkg.json` files. It holds hand-authored, branch-level build curation, see "Buildroot-only packages" below.

That layout allows efficient diff computation and hence determination whether a package is modified or not, and keeps the pristine `upstream-rpm` mirror cleanly separated from `main`. If a separate branch is considered too unwieldy, we could also have an `upstream-packages/` directory in `main` itself, but that will cause a lot of extra noise on `main`.

(Throughout this doc, `packages/…/pkgname/` is shorthand for a package's full `packages/`_distro_`/`_branch_`/`_pkgname_`/` path, which is identical on `upstream-rpm` and `main`.)

## Build configuration

To actually build an image, the selection needs happens in buck rules. E.g. production could define "take packages from fedora/f44 by default, but take a downstream management CLI and a forked systemd from aos/latest". A second "future" configuration defines "take everything from rawhide, plus the downstream management CLI from aos/latest".

The importer generates a `BUCK` file per `packages/`_distro_`/`_branch_`/` which declares its packages. A `package.manager` uses them with `local_packages`, see [images documentation](../../user/images.md).

## Operations

A single CLI tool (`tools/importer.py`) performs all operations via CLI verbs; how to run it and the
verb reference are documented in [importer.md](../user/importer.md).

## Design principles

 - Use `git` repository mechanics as much as possible, e. g. for computing diffs or queries for when which package changed
 - Avoid redundant state like duplicating version/release numbers or modification status in separate JSON metadata. This should only be done if performance would otherwise be too slow. `srcpkg.json` is a deliberate exception: on `upstream-rpm` it is *primary* data, not redundant — it can only be (re)computed by a full package build, so we cannot derive it from anything cheaper in the tree. On `main` it requires a package build, so it's expensive enough to record it statically. Its consistency is verified by the post-build recompute check (see below), per the next principle.
 - If we have to introduce any redundant metadata (e.g. the copied `srcpkg.json` on `main`), there must be a check happening on each commit/PR that validates its consistency.
 - Applying a local modification happens naturally: the developer updates the spec file, release, etc. There does not need to be any tool invocation for that, and future upstream updates then get merged with the local modifications.
 - Local modifications don't modify `%changelog` and bump `Release:` in a downstream sub-namespace; the concrete conventions developers follow are spelled out in [importer.md](../user/importer.md)
 - Every commit on `main` is a build. We don't "stage" modifications, as that just creates time bombs and makes it difficult to do integration testing. Devel branches can of course deviate from this (in particular, draft PRs which need conflict resolution, see below)
 - The downstream build configures its own `%dist` tag by appending `aos` to the upstream dist tag, e.g. `.fc44aos`, `.el10aos`, or `.hum1aos`. This keeps NEVRs unique across distros/branches (the same package may be built from several), and makes downstream rpms look different from Fedora etc. as they build against different library versions/toolchains; security scanners have to know about that, and NEVR in the VEX feed has to be accurate.
 - Rebuilding a package which uses `%autorelease` happens through a commit with subject "pkgname: Rebuild <reason>" (for humans) and an `X-Rebuild: pkgname` trailer (load-bearing). It contains *only* the resulting `srcpkg.json` changes. This avoids touching an arbitrary file in `packages/…/pkgname/`, as we want to avoid unnecessary diff noise.
 - tool code should deliberately be brittle: there should ideally be *no* `except:`. If there is any unforeseen situation in a newly imported or updated package, or a missing file or inconsistent state in the `OS.git`, the code crashes with a traceback, and developers need to fix it. Don't try to recover and apply heuristics/warnings.
 - We keep using the lookaside cache principle for sources. Storing them in git directly is out of the question, it will make `git clone` take way too long and become brittle. The downstream lookaside cache might even be a part of buck2's shared cache.

## Operation details

 * `update-upstreams` (temporarily) switches to the `upstream-rpm` branch, and parses the most recently imported upstream SHA from the `X-Upstream-Commit:` trailer in the most recent commit that changed `packages/…/PKGNAME/`. From there it can determine if there are newer upstream commits, and also do an efficient single-HTTP-query (without cloning, just `git ls-remote`) if upstream has any changes in the first place. It will only import upstream commits up to the most recent one that was actually built upstream, in order to avoid possibly broken staged changes. For Fedora we additionally ask bodhi whether a build was actually "published"; CentOS auto-publishes every koji build, we'll query its koji directly; and Hummingbird builds/publishes every commit.
 * `import-upstream`, `update-upstreams` fetch the binary RPMs, call `rpm -q --requires/--recommends/--provides` and `rpm -ql` (file list) on them, and update `srcpkg.json` accordingly. The `binaries` map is keyed by the build arch that *produces* each rpm: a noarch rpm is recorded under every build arch whose spec evaluation (`rpmspec --target <arch> -q`) produces its name. That correctly handles arch-specific noarch packages like glibc's per-arch `sysroot-<arch>-fcNN-glibc` cross-compilation sysroots, which belong only to their own arch's build. `BuildRequires` detection:
   - Static BuildRequires may be `%ifarch` (or similarly) conditional, so `srcpkg.json` records a per-arch `build_requires` map: the common set under the synthetic `_all` key, plus each build arch's conditional extras, evaluated from the dist-git spec with `rpmspec --target <arch>` in the build's distro context (`%dist`, `%fedora` or `%rhel`+`%centos` — a downstream `.elN` build is CentOS Stream — parsed off the srpm's dist tag).
   - Koji resolves dynamic BuildRequires (`%generate_buildrequires`) and writes them into the generated srpm's `Requires:`; but that got evaluated on whatever arch the srpm task happened to run on (often `s390x`), so the static part of that header can't be trusted per-arch. The dynamic part is recovered as the header minus the best-matching build arch's static set and folded into `_all`; a nonempty remainder without `%generate_buildrequires` in the spec crashes ("brittle by design" principle).
   - A locally built (`mockbuild`) srpm only has the static ones; mock's `buildreqs.nosrc.rpm` (which carries the dynamic ones) is transient and not retrievable post-build, and the buck rules don't reproduce it yet.
 * `import-upstream`, `update-upstreams` mirror the dist-git `sources` file with the rest of the tree, and record no archives in `srcpkg.json`. Each package build downloads the archives that its `sources` file lists and checks their SHA512 digests. A `sources` line with an MD5 digest fails the build. `rpm.branch()` downloads from the lookaside cache of the upstream distro, or from the `lookaside` property of `_properties.json`. A package with a `sources` file in a branch of another distro without a `lookaside` property fails to load.
 * A downstream lookaside cache can serve the sources through the `lookaside` property.
 * `import-upstream` defaults to importing the latest commit. This matches the assumption that downstream srpms auto-trail their upstreams anyway. However, in some corner cases (Fedora rawhide being in the middle of a library or Python transition, etc.) downstream may have to import an older version. Hence this needs an option to specify a SHA. But this is only a temporary workaround, as hours later, the machinery will propose an update.
 * `import-upstream` imports only the target commit, no history. This is true even for `%autorelease`: the version and release live in the imported `srcpkg.json`, ground truth off the koji build's NEVR, so no commit-count context is needed and deeper history stays upstream. (We previously tried to do commit counting, but rpmautospec's number simply cannot be reproduced from imported history: it counts dist-git commits back to the last *evaluated* `Version:` change, past any reasonable import boundary, even past the switch to `%autorelease` — and its `-b`/`-e`/`-p` offsets defeat counting altogether.) The imported commit must have a published build, which is what generates the metadata; an explicit `--sha` without one is refused.
 * `import`/`update`: Since the package's path is identical on `upstream-rpm` and `main`, `git cherry-pick` applies upstream commits directly (preserving their message and `X-Upstream-Commit:` trailer). A literal `git merge` does not work, as the two branches share no common history. If `cherry-pick` ever fails for some corner case, we can fall back to applying the diff and copying the commit message in code.
 * `update` uses the same `X-Upstream-Commit:` git trailer anchor to spot new commits in `upstream-rpm` as `update-upstreams` does for upstream dist-git repos. The trailers (just like the entire commit messages) are preserved on main.
 * `update`: If there are conflicts, keep the markers, and create a commit starting with `CONFLICT:` and a list of conflicted paths. The posted PR will then be draft, and of course fail to build. This will provide enough visibility to human developers which updates need manual work.
 * `import`/`update` keep the 1:1 commit correspondence, i.e. will import new commits from `upstream-rpm` individually, keeping their `X-Upstream-Commit:` link.
 * `import`/`update` handling of `srcpkg.json`:
   - start with copying the `upstream-rpm`'s srcpackage metadata, with the assumption that in the vast majority of cases the relevant bits like `BuildRequires` will be identical in a downstream build.
   - predict the `%dist` tag change (appending `aos` as above) and update dist tags in the relations lists before build. This will help the buck build system to resolve dependencies correctly and should already cover most of the "diff noise". This is a heuristic, but the result will be validated by the next step.
   - After building the packages (in the update PR), a post-build check re-computes the relationships/file lists, amends the corresponding commits, and re-pushes the temporary import branch for the PR.
 * `import`/`update` can further transform the per-srcpackage metadata into downstream build system specific form/rules in the future, once it has been designed/built. These rules will be included into the import commit, so that all corresponding changes are tied together.
 * `srpm`: Download the `sources` into `packages/…/pkgname/`, uncommitted. Like Fedora's koji plugin, compute the `%autorelease` value and write it into the SRPM by prepending a static spec definition (`%global autorelease <pkgrel>[.<minorbump>]%{?dist}` plus an empty `%global autochangelog %{nil}`). With that, the buildroot needs no git access and the SRPM rebuilds reproducibly anywhere. `<pkgrel>` comes straight from the imported `srcpkg.json`. The optional `.<minorbump>` is the downstream modification bump (the `.1`/`.2` *before* the dist tag, per the release rules in [importer.md](../user/importer.md)) and equals the count of local commits since the most recent import: commits without `X-Upstream-Commit:` which either touch `packages/…/pkgname/` or have an `X-Rebuild: pkgname`. A fresh upstream release restarts the bump, so the next modification is `.1` again, and so does a local commit that discards the local delta: if a commit leaves `packages/…/pkgname{/,.json}` byte-identical to the import it is based on, the package is unmodified again, so the count starts over at it. That is derived from the trees, not tracked in a trailer, so a hand-written revert counts the same as a `sync`. This is the only place that counts commits, and it is fully contained in the `OS.git` history. We are not interested in changelogs, hence an empty `%autochangelog`. The buck rules freeze `%autorelease` the same way.
 * `update`/`list`: If the *only* dist-git difference between upstream and downstream is in the `Release:` line, then disregard that delta (revert before cherry-pick) and consider the package to be unmodified. Downstream commonly has to do such bumps for rebuilds against newer dependencies.
 * `sync`: replays the same new commits, taking the package directory from upstream instead of merging the local one into it: that discards the local delta exactly (including locally added files, which have no upstream counterpart to conflict with), and it cannot conflict. The discard therefore rides along in the first replayed upstream commit; when there is no new upstream commit to fold it into, it becomes a local commit of its own. Either way the package ends up byte-identical to upstream's, which is what makes it unmodified again for the release bump rules above.
 * `rebuild`: If the package uses `%autorelease`, generate a commit with `X-Rebuild: pkgname` and no actual changes to the package directory; otherwise, bump `Release:` per the release rules in [importer.md](../user/importer.md). In both cases, update `srcpkg.json` for the expected Release: bump result, similar to what `import`/`update` do.
 * `check`: Walk through all commits (or all since the given ref, `origin/main` in PRs) of the current branch (usually `main` or a temporary developer or package update PR branch). For each commit that touches `packages/`:
   - check that `srcpkg.json` is valid JSON
   - check that `Version:` and `Release:` numbers agree between the architectures in the `binaries` map.
   - if it has `X-Upstream-Commit:`, check that it resolves to a corresponding commit on the `upstream-rpm` branch at the identical `packages/` path
   - if it does *not* have an `X-Upstream-Commit:`, check that the package either uses `%autorelease` or `Release:` was bumped according to the release rules in [importer.md](../user/importer.md). This includes downstream specific packages.
   - if it does *not* have an `X-Upstream-Commit:`, check that it changes `srcpkg.json`; release numbers are part of `Provides:`; guards against accidentally forgetting to update (or `git add`) metadata. Imported commits legitimately lack the metadata change when upstream never built that commit on its own (batched pushes, built once at the end with `%autorelease` counting them all)
   - if it does *not* have an `X-Upstream-Commit:` and *only* changes `srcpkg.json` metadata, check that the package uses `%autorelease` and commit has `X-Rebuild:`. Every other case of local metadata modification has to come from a sourceful modification or no-change rebuild which bumps `Release:`. Two legitimate exemptions:
     * imported commits: upstream's rpmautospec mass rebuilds are empty dist-git commits, and their build's recomputed json is all the mirrored commit contains
     * commits that also change the branch's `_properties.json`: its `rpmbuild_options` are a build input, so the recomputed json is the *result* of a real change
     * commits that discard the local delta (see the bump rules above): they take a modification away instead of making one, so they equally have no `Release:` bump and no `X-Rebuild:` to show — and need none, as their release is the import's, established by their tree
   - if it has `X-Rebuild:`, check that package uses `%autorelease` and the commit only changes `srcpkg.json` and nothing else.
   - if it does *not* have `X-Upstream-Commit:`, check that it does not touch `%changelog`
   - if package uses `%autorelease`, check the recorded release against the metadata chain: an imported commit that changes the recorded version-release must advance the release unless the version changed with it (upstream's counter restarted); a local commit's release must be the last import's release plus `.<count of local commits since>`.

   This does *not* check the integrity of the metadata -- it trusts developers not to mess around with this, and it can only be validated through a rebuild of the rpms. There might be more checks in the future.

## Rebuilds

### Distribution status quo

When a `libfoo` shared library changes without a new SONAME, distributions generally don't rebuild reverse dependencies. While they *could* change behaviour/compilation due to changes in include files, it is generally expected that this doesn't happen.

Distributions *do* mass rebuilds of reverse dependencies (often no-change, sometimes with source changes to adjust to API changes) for SONAME changes and static linking, e.g. updated Go modules or Rust crates.

The mass rebuilds generally happen via a source package change/commit that bumps the "Release" number. Packages using `%autorelease` just get no-change commits. This is because rpm (same for deb) repositories are designed with the expectation that the contents of a particular NEVRA .rpm *never changes*. I.e. the NEVRA is a global fixed synonym for the bits in the .rpm/.deb file.

### Our strategy

For our buck based image builds we want/need to be stricter: The "no rebuilds for shared library updates" model does not work with buck's strict "changed input triggers rebuild" model, and for both correctness (include files!) and reproducibility reasons we will just do the full transitive reverse dependency rebuilds. This assumes that the images are small enough for that to be viable. We can look into optimizations (such as moving to "header files + symbol table dump" as input instead of the full library rpm) if and when rebuild times become too much of a burden.

Our primary exported build artifact is an image, not a set of rpms. For as long as we can get away with it, we treat the built RPMs as an implementation detail and buck cache entries, and do not publish them to an official package mirror. The only reasons why we deal with NEVRAs at all is because we (1) inherit them from Fedora/CentOS imports, and (2) they are the language of SBOMs, security scanners, PURL and VEX feeds. This means that we can *accept* implicit rebuilds within buck, so that a `myapp-1.2-3` NEVRA that depends on `libfoo` will *change its contents* after a libfoo update. In that model, the `OS.git` will only get the actual libfoo change commit; all rebuilds of reverse dependencies and the image itself will happen automatically via buck. This includes SONAME changes and static linking cases, as they are included in buck's reverse dependency calculation rules (see below). Exception: when the rebuild incorporates the actual fix (like a bundled Go library with a CVE fix), we need the rebuild/Release bump to mark a CVE fix (in the VEX feed).

This changes if/when a downstream decides to actually *publish* the built rpms. Then we need to play by the "NEVRA is a global fixed synonym for the bits" rule. Then a branch/PR that updates `libfoo` will automatically call `rebuild` for the affected reverse dependencies. This will have to be designed carefully: a naïve implementation that builds all transitive revdeps easily overbuilds and quickly reaches the entire package set for low-level packages like glibc, openssl, or libsystemd. Instead, we can ask buck to compute the rebuilds and which rpms *actually* change their content (for shared library updates we expect they don't, aside from -debuginfo which we filter out), and then commit the No-change rebuilds for only them, so that their Release: bumps for correct publication. We will need to design the integration with buck and automation, but only if and when we actually need it.

### Reverse dependency calculation

This is implemented with the usual buck mechanics, based on `srcpkg.json` metadata the tool produces.

 * Collect all src and binary package names and their `BuildRequires:` and _provides_ = `Provides:` ∪ `Files:` from the `srcpkg.json` metadata of all packages a build configuration selects, into a reverse map. (This treats path names as a special case of `Provides:`, they effectively work the same way). [Per policy](https://docs.fedoraproject.org/en-US/packaging-guidelines/#_file_and_directory_dependencies), dependencies may only refer to `/usr/bin/` and `/etc`, so filter the calculation down to these paths for efficiency.
 * A package _X_ is a direct revdep of `pkgname` iff _X's_ `BuildRequires:` set intersects `pkgname`'s _provides_.

This happens per build configuration on `main`, i.e. with downstream specific metadata. Scoping to the selected package set keeps a rebuild from crossing into a different distro/configuration's packages.

This *overbuilds*: shared libraries which don't change SONAME generally *don't* require their revdeps to be rebuilt. They might change behaviour due to changed C header files and the like, but the C world is generally very disciplined with this. The main reason to do it is (1) be absolutely sure, and (2) keep package builds reproducible. If the overbuilding becomes a burden, we can optimize by checking the `Provides:` diff in the originating `srcpkg.json` and only triggering rebuilds on more specific changes like SONAME bumps. Or e.g. just avoid rebuilds for `glibc`.

## Buildroot-only packages

`BuildRequires:` (BR) cycles are unavoidable at the bottom of the stack (gcc needs glibc, glibc needs gcc). There is a strongly connected cycle of about 70 packages in Fedora's build root. buck rules cannot have cyclic deps, so the intra-cycle edges are broken by ignoring the cycle, and falling back to installing these BuildRequires from the "seed" (upstream distro packages). This is exactly what koji does, by building against its own previous build round.

That fallback assumes the seed can actually provide the dropped edge (i.e. BR). `buildroot_only_packages` is the list of packages for which that assumption is false: i.e. packages that exist *only* in the distribution's build ecosystem (koji's buildroot repo) and are *never* shipped in the compose (i.e. seed).

The only current case is `gcc`, which build-requires `(glibc32 or glibc-devel(x86-32))`. `glibc32` is a `glibc` subpackage that lives only in koji's buildroot, never in the compose; the only compose-side alternative is the 32-bit multilib `glibc-devel.i686`, but as tine does not supports/tracks that architecture, the build system cannot use it. Also, the alternative declaration (and koji) prefer `glibc32` anyway. The consuming OS.git can import `glibc` and then *does* build `glibc32`, but with the naïve "ignore the cycle" approach from above, tine cannot use it, and thus `gcc` cannot build.

The `buildroot_only_packages` branch property lists these packages: a listed edge survives the cycle-breaking, so it introduces a gcc → glibc dependency edge and hence gcc builds against the OS.git's glibc build. The reverse glibc → gcc edge stays dropped as per the general rule above. When and how to add an entry, and the `_properties.json` format, are documented in [importer.md](../user/importer.md).

## Seed-only packages

The opposite curation: the `seed_only_packages` branch property (defined and exemplified in [importer.md](../user/importer.md)) resolves everything a listed source package provides from the seed, because its role in other package builds is tool use, not linkage. Since such edges are inside the BuildRequires cycle today, listing a package changes nothing about which rpms end up in a buildroot -- it turns the cycle fallback into policy, and thereby shrinks the cycle itself (fewer intra-cycle edges), which is what makes the future self-hosting approaches cheaper. The analyzed candidates (gcc, gnupg2, kernel, tzdata -- cycle 69 → 38) and their trade-offs are documented in [self-host-approaches.md](self-host-approaches.md).

## Multiple architectures

The tool tracks a fixed `BUILD_ARCHES` (currently `x86_64` and `aarch64`) for imports. The buck rules build a package for the configured architecture, from that architecture's `binaries` bucket.

 - The `upstream-rpm` branch always has full arch coverage. `import-upstream`/`update-upstreams` fetch all tracked arch rpms from upstream koji.
 - The per-arch `build_requires` and the noarch producing-arch attribution are evaluated from the spec (`rpmspec --target <arch>`) and thus never require an actual build of that arch. This field has full coverage on `main` as well.
 - `rpm-metadata` expects the caller to provide rpms for *all* tracked arches; it wholesale-replaces the `binaries` map with what it is given. Collecting the rpms of the per-arch builds is up to the downstream CI. `check` validates that the recorded arches agree on Version/Release.
 - A recompute from a single-arch build drops the other arches' buckets, or degrades them to the noarch subpackages whose attribution is spec-derived. Without a bucket, the package fails to configure for that arch; with a degraded one, its build fails the check that it produces exactly the recorded subpackages. So a downstream which builds only some arches can only build those.
 - The buckets repopulate on the next build + `rpm-metadata` of each package with all arches; no repair step is needed.
