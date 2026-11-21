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

## Building the project's own source tree

A project can build an RPM from its own files. Declare a source tree in the root `BUCK` file and pass it as
`source_tree`:

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

Every `BUCK` file in a subdirectory of the tree must call `project.source_listing()`:

```starlark
load("@tine//project:defs.bzl", "project")

project.source_listing()
```

Without that call, the build fails with an ``Unknown target `source_tree_listing` `` error that names the
package of that `BUCK` file.

The build reads the directory of the `BUCK` file that declares the tree as it is on disk. Its inputs are the
files of that directory and its subdirectories, except the files that Buck ignores and `.buckconfig.local`.
tine makes Buck ignore the files that Git ignores. `tine buck build --show-output :source` prints the path
of a file that lists the inputs.

- The build also sees the files that are not inputs, such as `.git` and the files that Git ignores. An edit
  to one of them doesn't start a new build, and the shared cache can return a result that was built with a
  different version of them. Keep the files that a build reads out of `.gitignore`.
- Buck tracks a file only if at most four directories and file names start with a dot in its path relative
  to the nearest `BUCK` file.
- Buck tracks no file in the directory of another cell, such as a cell that tine adds below the project root.
- A build of a tree at the project root sees an empty `buck-out` directory.

A dev build keeps its build directory between builds, and an edit recompiles only what it changes. Pass
`dev = True` to `rpm.package()` for a dev build, or list the project path of the package that declares the
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
