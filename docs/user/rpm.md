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
