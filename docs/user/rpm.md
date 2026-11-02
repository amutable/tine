<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Building RPM packages

`rpm.package()` builds RPM packages from a spec and its Source and Patch files. The importer declares the
packages that it brings in from a distribution; see [Maintaining packages](importer.md).

## Building a package from a spec

Put the spec and the files that it names in the directory of the `BUCK` file or below it, and declare the
package:

```starlark
load("@tine//package_system/rpm:defs.bzl", "rpm")

rpm.package(
    name = "hello",
    package = "hello",
    spec = "hello/hello.spec",
    srcs = ["hello/hello-2.12.3.tar.gz"],
    buildroot = "catalog//:fedora.rawhide.buildroot",
    release = "1",
    source_date_epoch = 1754006400,
    subpackages = ["hello"],
)
```

`package` is the `Name:` of the spec, and `subpackages` lists the binary packages that the build produces.
Each subpackage is a sub-target, such as `:hello[hello]`.

## Building a project from its own checkout

An upstream project, such as systemd, can build RPMs of itself from the checkout that contains its `BUCK`
files. If your project has no `BUCK` file in a subdirectory, pass the directory of the root `BUCK` file as
`source_tree`:

```starlark
load("@tine//package_system/rpm:defs.bzl", "rpm")

rpm.package(
    name = "systemd.rpm",
    package = "systemd",
    spec = "systemd.spec",
    source_tree = ".",
    buildroot = "catalog//:fedora.rawhide.buildroot",
    release = "1",
    source_date_epoch = 1744207869,
    subpackages = [],
)
```

Buck rejects `.` as soon as a subdirectory contains a `BUCK` file, because a directory source cannot include
another Buck package. In that case, declare the tree with `project.source_tree()` in the root `BUCK` file and
pass its label instead:

```starlark
load("@tine//package_system/rpm:defs.bzl", "rpm")
load("@tine//project:defs.bzl", "project")

project.source_tree(name = "source")

rpm.package(
    name = "systemd.rpm",
    package = "systemd",
    spec = "systemd.spec",
    source_tree = ":source",
    buildroot = "catalog//:fedora.rawhide.buildroot",
    release = "1",
    source_date_epoch = 1744207869,
    subpackages = [],
)
```

`project.source_tree()` lists the files of its own directory. It cannot list the files below another `BUCK`
file, so every other `BUCK` file in the project lists its own files with `project.source_listing()`:

```starlark
load("@tine//project:defs.bzl", "project")

project.source_listing()
```

If a `BUCK` file lacks the call, the build fails with an ``Unknown target `source_tree_listing` `` error that
names the directory of that `BUCK` file.

The build reads the tree as it is on disk, like a local checkout; see [Building from a local checkout
instead](git.md#building-from-a-local-checkout-instead) for which files are inputs. `tine buck build
--show-output :source` prints the path of a file that lists the inputs. [Project source
trees](../design/architecture.md#project-source-trees) describes the few files that the list leaves out.

A dev build keeps its build directory between builds, and an edit recompiles only what it changes. Pass
`dev = True` to `rpm.package()` for a dev build, or list the directory of the `BUCK` file that declares the
tree under `[tine] dev`, with `.` for the project root:

```toml
[buckconfig.tine]
dev = "."
```

## Build dependencies

The build installs the BuildRequires of the spec over the shared base buildroot. `build_requires` lists
packages that the build installs before it reads the spec, such as tools or RPM macros that rpm needs to
parse the spec. Before the build, `rpmbuild -bd` checks the spec in that buildroot with the build's rpmbuild
options, so the spec's conditionals select the same BuildRequires as in the build. The build then installs
the BuildRequires that the buildroot lacks.

`rpmbuild -bd` runs `%generate_buildrequires` only when the buildroot contains every static BuildRequires
of the spec, and runs it once. For a spec with the script, list its static BuildRequires in
`build_requires`. If the script reports more packages once its first ones are installed, list those as
well. Otherwise the build fails with rpm's `Failed build dependencies:`.

Neither the check nor the build looks for the Source and Patch files of an in-place spec. The build only
fails if the spec reads a missing file, for example a Source that `%install` copies.

A package of an imported branch skips the check. It installs the BuildRequires that the importer recorded
in `build_requires`.

## Building a package from a dist-git repository

Declare the spec of a dist-git repository in the `sub_targets` of its [`git.fetch()`](git.md), and pass that
sub-target as `spec`. Pass the fetch itself in `srcs`. The build takes every file at the top of a directory
in `srcs`, and installs the BuildRequires of the spec:

```starlark
load("@tine//git:defs.bzl", "git")
load("@tine//package_system/rpm:defs.bzl", "rpm")

git.fetch(
    name = "hello.git",
    repo = "https://src.fedoraproject.org/rpms/hello.git",
    rev = "2621a97179dd5182d127a98a50759a637a4fa334",
    sub_targets = ["hello.spec"],
)

rpm.package(
    name = "hello",
    package = "hello",
    spec = ":hello.git[hello.spec]",
    srcs = [":hello.git"],
    buildroot = "catalog//:fedora.rawhide.buildroot",
    release = "1",
    source_date_epoch = 1754006400,
    subpackages = ["hello"],
)
```

Fedora and CentOS keep the source archives out of the repository, in a lookaside cache. Add them to `srcs`
as well.

The build fails if two files of `srcs` have the same name and different contents, or if the top of a
directory in `srcs` contains a symlink. For a spec in a subdirectory of the repository, such as `rpm/`,
declare `rpm/hello.spec` and `rpm` in `sub_targets`, and pass `":hello.git[rpm/hello.spec]"` as `spec` and
`":hello.git[rpm]"` in `srcs`.
