<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Pinning prebuilt tools

`http.tool()` downloads a prebuilt tool for each CPU architecture that tine builds for. The project's
`tine.lock.json` pins every download by sha256 and size, and Buck checks the download against both. `bump`
updates the pins to the newest upstream release.

## Declaring a tool

Add an entry for the tool to `tine.lock.json` at the root of the project. The `ruff` entry in tine's own
[`tine.lock.json`](../../tine.lock.json) pins a GitHub release.

Then load the entry in a `BUCK` file and declare the tool:

```starlark
load("//:tine.lock.json", pins = "value")
load("@tine//http:defs.bzl", "http")

http.tool(
    name = "ruff",
    path = "ruff",
    spec = pins["ruff"],
)
```

`buck run :ruff -- check` runs ruff, and a rule takes `:ruff` as a dependency like any other executable.

## The pin

- `repository` and `release` name a GitHub release, and each `artifact` is a file attached to it.
- `url` replaces `repository` for a tool that is not published on GitHub. Each artifact is downloaded from
  `<url>/<artifact>`, and `release` is the version.
- `releases` names the index that `bump` reads for a pin with a `url`. Its `url` is a page that lists the
  releases, and every match of its `pattern`, a Python regular expression, is a release. `bump` compares
  the numbers in each match and takes the highest. A prerelease such as `go1.28rc1` ranks by its numbers
  too, so write a pattern that matches only releases. A pin without `releases` stays on its release. See
  [Building with your own Go toolchain](go.md#building-with-your-own-go-toolchain) for an example.
- `platforms` has one entry per architecture, named `arm64` and `x86_64`. Buck downloads the entry of the
  architecture that the target is configured for. A rule attribute declared with `attrs.exec_dep()`, such
  as the `toolchain` of `go.package()`, configures the tool for the machine that runs the build.
- `sha256` and `size` are those of the downloaded file.
- `strip_prefix` is a directory in the archive whose contents become the root of the unpacked tree.

## The arguments

- `spec` is the pin, as loaded from `tine.lock.json`.
- Without `path` or `directory`, the download is the program itself.
- `path` names the program inside an archive. The program is the target's output.
- `directory = True` makes the whole unpacked archive the target's output, for a tool that needs the files
  beside its program. `path` then names the program that `buck run` runs. Without `path`, the target has
  nothing to run.
- `compressed = True` decompresses a download that is a single zstd-compressed program.
- `args` come before the arguments given to `buck run`.

## Updating the pins

Run `tine bump` with the tools to update. It updates the `tine.lock.json` of the project that it runs in:

```sh
tine bump --tool ruff
```

`bump` writes the newest release, artifacts, digests and sizes back into the file. `--all` updates every
pin in the file, and `--commit` commits the result.

`bump` also fills in a new pin. Write the entry with `repository` or `url`, `release` and each platform's
`artifact`, leave out `sha256` and `size`, and run `bump` for that tool. An artifact name that contains the
release version, such as `syft_1.54.1_linux_amd64.tar.gz` in release `v1.54.1`, is matched to the
same file in the newest release.

`bump` replaces the old release with the new one in each `strip_prefix`, and for a pin with a `url` also in
each artifact name. A release with a leading `v`, such as `v1.2.3`, also replaces `1.2.3` in a name that
doesn't contain `v1.2.3`. When the release of a pin with a `url` changes, `bump` downloads every artifact
again to compute its digest and size, and fails if a download ends early.
