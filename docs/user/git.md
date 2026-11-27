<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Fetching project sources

tine wraps Buck's `git_fetch()` as `git.fetch()` so local development can use a checkout without changing
the pinned source. The wrapper normally fetches the requested commit. It uses a directory in the current Buck
package instead when that directory is non-empty and its name matches the target after removing a
trailing `.git`.

## Declaring a git fetch

For example, a `packages/hello/BUCK` can declare:

```starlark
load("@tine//cargo:defs.bzl", "cargo")
load("@tine//git:defs.bzl", "git")

git.fetch(
    name = "hello.git",
    repo = "https://github.com/example/hello",
    rev = "1c9e0f4b0f7d2a5e8b3c6d1a4f7b2e5c8d0a3f6b",
)

cargo.package(
    name = "hello",
    binaries = ["hello-cli"],
    src = ":hello.git",
)
```

To build an RPM package from a fetched dist-git repository, see
[Building RPM packages](rpm.md#building-a-package-from-a-dist-git-repository).

## Building from a local checkout instead

The target above looks for a checkout at `packages/hello/hello/`: the package path followed by the target
name without `.git`. Use `tine mount` to place a working checkout there without copying it into the
project:

```sh
tine mount list                                        # includes packages/hello/hello
tine mount add packages/hello/hello ~/Projects/hello   # use the working checkout
tine mount remove packages/hello/hello                 # use the pinned commit again
```

A mount is also what makes a `cargo.package()`, `go.package()` or `rpm.package()` build incremental: then
its build directory survives between runs, and an edit recompiles just what changed. See
[Rust source builds](../design/architecture.md#rust-source-builds).

To build a checkout that is committed to the project incrementally, list its project path under `[tine]
dev` in `.buckconfig`, or as `[buckconfig.tine] dev` in `tine.toml` or `tine.local.toml`. Separate several
paths with commas. tine adds every mounted target to that list. While mounts are declared, tine
rejects a `[tine] dev` entry in `.buckconfig.local`, because it would replace the mounted targets.

```toml
[buckconfig.tine]
dev = "packages/hello/hello"
```

The override has a few constraints:

- Only a `git.fetch()` or `git.checkout()` target can be mounted, anything else is rejected.
- A directory in the project must contain at least one file. An empty directory, such as an uninitialized
  submodule, falls back to the remote fetch. A mounted directory counts as a checkout even when it is empty.
- Every file not excluded by `.gitignore` or `[project] ignore` is a build input. tine merges both, plus
  VCS metadata directories, into the generated ignore list; put build outputs in `.gitignore` and
  anything Git tracks but Buck should not see in `[project] ignore`.
- A build reads the checkout as it is on disk, including the files that Buck ignores, such as build
  output, `.git` and the files listed in `[project] ignore`. Buck does not hash an ignored file, so an
  ignored file can change what a build produces without changing the cache key. Keep the files a build
  reads out of `[project] ignore`.
- The local checkout is not checked against `rev`. Remove the mount before a release build to restore the
  pinned source, and build releases from a fresh clone of the project.
- A `BUCK` file in the checkout creates a package boundary that the source glob cannot cross. Projects
  containing Buck packages must use the remote fetch.

## Checkout slots without a fetch

`git.checkout()` declares the directory named after the target as a source tree that may be empty: a slot
to commit sources into or to `tine mount` a checkout over. `rpm.package()` declares one per package,
`<name>.source`, and builds from it in place of the source archives once it holds anything; see
[Building from a source checkout](importer.md#building-from-a-source-checkout).

A consumer reads a populated slot as it is on disk, like a mounted checkout. An `image.ops.copy()` of a
path in the slot therefore also copies the files that Buck ignores below that path into the image.
