<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Building images

How to declare, build, and run OS images. This is the user guide. The architecture behind it (the
provider vocabulary, the delta layer model, and disk composition internals) is documented in
[architecture.md](../design/architecture.md).

## Restrictions

File permissions in built images are normalized: 0755 for directories and executables, 0644 for files.
This deliberately breaks suid/sgid executables and the sticky bit: these need to be replaced with
socket-activated services and other safer alternatives. `/tmp` and `/var/tmp` keep their `1777`, which is
restored when the image is finalized. Nothing on a `/usr` partition ought to be a secret, and
[`tmpfiles.d`](https://man7.org/linux/man-pages/man5/tmpfiles.d.5.html) handles permissions of files in
writable directories such as `/etc` and `/var`.

If you must have files with different permissions in your image, the escape hatch is the `tmpfiles`
attribute of an image or composition.

A layer that adds a program or shared library which would run on an executable stack fails to build: one
whose `PT_GNU_STACK` program header is executable, or which has none on x86, where glibc then falls back
to an executable stack.

## Concepts

A **catalog** is the Buck cell named `catalog`, which declares which OS releases are available to build
against. For each release it bundles the repository definitions, the release identity and its package
sets, a package manager (the pinned solve environment that images start from), a buildroot for package
builds, and a box. tine's own catalog is [`catalog//`](../catalog/BUCK) and declares these releases:

- `fedora.rawhide`: pinned to an rpmrepo compose snapshot, so packages never vanish underneath the pins
- `arch.rolling`: pinned to a day in the Arch Linux Archive, whose dated trees serve databases that never
  change under a committed pin
- `debian.testing`: pinned to a timestamp in the Debian archive, whose dated trees serve indexes that
  never change under a committed pin

Every one of them is pinned to a mirror that publishes immutable snapshots, which is what a release has to
have to be buildable from a committed pin at all.

Each release consists of targets named `<family>.<release>.<role>`, for example
`catalog//:fedora.rawhide.package-manager` or `catalog//:arch.rolling.release`. The pins live
as committed snapshots under [`catalog/snapshot/`](../catalog/snapshot/): repository metadata in
`snapshot/repo/*.<architecture>.json` and frozen box transactions in
`snapshot/box/*.<architecture>.json`. Normal builds therefore never touch the network;
`refresh-catalog --advance` (below) advances the pins. The naming scheme and the pinning mechanism are
described in [architecture.md](../design/architecture.md).

A project can declare its own catalog with the same macros. Point the `catalog` cell at its directory in
`tine.toml`:

```toml
[buckconfig.cells]
root = "."
tine = "tine"
catalog = "catalog"
```

Every `catalog//` label then resolves to the project's catalog, including the labels in tine's own
targets, and the build does not use tine's catalog. If `tine.toml` declares no `catalog` cell, `bin/tine`
points the cell at tine's catalog.

tine's own boxes, such as its tools and the box that generates signing keys, build from the catalog's default
release. A release has boxes only for the architectures it serves, so its images build only on a host of one
of those architectures. Arch serves x86_64 only, so Arch images don't build on an arm64 host. Name the
default release in the catalog's `BUCK` file, with the `<family>.<release>` prefix of any release the catalog
declares:

```Starlark
load("@tine//distribution:defs.bzl", "distribution")

distribution.default_release("debian.testing")
```

If the catalog's `releases.bzl` lists one release, that release is the default release. A catalog with
one release in `releases.bzl` doesn't call `default_release()`. The build fails if it does.

To build with the boxes of another release of the catalog, set `box` in the `tine` section of the
project's `.buckconfig`, or pass it for one build:

```sh
tine buck build -c tine.box=arch.rolling //...
```

A tine box installs the packages it lists for the default release's family. A box that lists no packages
for that family fails to build, and so does a box that lists a package the release does not contain.

The catalog also lists the releases that images build for in `releases.bzl`, beside its `BUCK` file. See
"Choosing a distribution" below.

A **box** is a pinned, reproducible execution environment that runs every build action. Each release has a
root box, which its release macro declares. `<family>.<release>.box`, such as `catalog//:fedora.rawhide.box`,
contains the package system's own tools, Python and core utilities. It resolves and installs every other box
of the release. `tine//distribution:tools.box` adds the image assembly tools and the VM stack on top of the
root box of the release that the build chooses, and images build in it. The tools stay in the box and out of
the built images. How a box bootstraps itself is described in [architecture.md](../design/architecture.md).

## Commands

The wrapper commands work from anywhere in the root project:

```sh
tine buck run tine//tools:refresh-catalog
tine buck run tine//tools:verify-catalog
tine buck run tine//tools:fmt
tine buck run tine//tools:check
```

`refresh-catalog` refreshes the `catalog//` cell, which is the project's catalog if it declares one. A
project declares its releases in the root package of that cell, because tine fails to load a release declared
anywhere else. To refresh the repositories and boxes of another package, pass it after `--`, for example
`tine buck run tine//tools:refresh-catalog -- my_project//boxes`. `verify-catalog` performs the same
generation and fails when the committed JSON differs. The pinning and refresh mechanism is described in
[architecture.md](../design/architecture.md).

## Declaring an image

Everything below is re-exported from one facade, so a `BUCK` file needs a single load:

```Starlark
load("@tine//image:defs.bzl", "image")
```

The modules behind the facade, such as `image.bzl` and `compose.bzl`, are implementation structure and
may be rearranged; load them directly only from inside the cell.

An initial image fixes one package manager for its whole lifetime, and every derived image and terminal
output inherits it. An image runs in the tools box of the release that the build chooses, or in the tools box
of the catalog's default release when the build chooses none. An image that names the package manager of one
release therefore also names that release's distribution. Take a catalog package manager, optionally extend
it with project repositories, and create the initial image. For example, a project can expose locally built
packages without adding them to its OS release:

```Starlark
local_repository(
    name = "project.repository",
    packages = ["//packages:project"],
)

package.manager(
    name = "project.package-manager",
    base = "catalog//:fedora.rawhide.package-manager",
    additional_repositories = [":project.repository"],
)

image.layer(
    name = "project.image",
    distro = "catalog//:fedora.rawhide.distribution",
    package_manager = ":project.package-manager",
    packages = ["project"],
)

image.layer(
    name = "project-configured.image",
    parent = ":project.image",
    ops = [image.ops.run(["/usr/bin/project", "configure"], chroot = True)],
)
```

A package manager may instead attach a branch's generated local-packages universe:

```Starlark
package.manager(
    name = "image.package-manager",
    base = "catalog//:fedora.rawhide.package-manager",
    local_packages = "//packages/fedora/rawhide:_local_packages",
)
```

Each install then builds exactly the locally built packages in its runtime closure and offers them to the
solver ahead of the upstream repositories; requested capabilities without a local provider continue to
resolve upstream (details in [architecture.md](../design/architecture.md)).

## Images and operations

Ordinary absolute symlinks in chrooted file operations resolve inside the image. Images and scripts must
be trusted because procfs magic links can access paths outside the image.

`image.layer` has two construction modes. An initial image takes `package_manager`, which defaults to the
package manager of the distribution that the build chooses. A derived image takes `parent` and inherits the
package manager of that image. A call with operations applies one ordered operation sequence in one action
and persists exactly one delta:

- `packages` and `package_sets` are rule attributes rather than operations: a layer installs them as one
  request before any of its operations run, so every operation sees what the layer adds. `package_sets`
  names symbolic sets the image's OS release supplies and `packages` names concrete ones; a layer may use
  both, and duplicates between them collapse.
- `image.ops.run([...])` executes a command against the image. By default the box supplies the userspace and
  the image is mounted at `/buildroot`; with `chroot = True` the command runs inside the image with its
  own binaries instead. Either takes an `env` argument that overlays variables on that command's
  environment. An argument names a build artifact by being one, or by spelling `$(location //target)` in
  a BUCK file, and it resolves the same in both modes: a chrooted command gets the project bind-mounted
  read-only at a fixed path under `/run`, which is also its working directory. Write generated files
  to paths in the image rather than to the working directory. Running a script the repository
  owns is therefore just `image.ops.run(["/usr/bin/bash", "$(location :setup.sh)"], chroot = True)`. Buck's
  macro parser claims `$(...)`, so a shell substitution has to be written `\$(...)`; an unescaped one fails
  to parse rather than silently reaching the shell.
- `image.ops.python([...])` runs a python script against the image without the image needing python: it is an
  `image.ops.run` of the relocatable interpreter Buck already pins for its own bootstrap, named through the
  project like any other artifact. It takes the same `env` and `chroot` arguments, so the script either
  sees the image
  at `/buildroot` or has it as its own root; in the chrooted case nothing of the interpreter reaches the
  delta, because the project bind carrying it lives under `/run`.
- `image.ops.copy` introduces a declared Buck artifact at an absolute image path; `image.ops.mkdir` and
  `image.ops.symlink` mutate the same root. `image.ops.remove` treats its absolute path as a glob pattern,
  including recursive `**`, and removes every matching file, symlink, or directory tree. A pattern that
  matches nothing does nothing.
- `image.ops.depmod()`, `image.ops.hwdb()` and `image.ops.locale_gen()` build the state installed packages
  only describe (below).
- `image.ops.install_from` installs what another target needs and applies the operations it attaches (see
  below).

`image.layer()` recursively flattens operation lists, allowing reusable helpers to return ordered groups of
operations; `image.archive`, `image.sysext`, `image.confext`, `image.extension`, `image.portable`
and `image.disk` accept the same nested groups. Materialize a complete logical image explicitly with
`image.directory`.

### Generating what packages only describe

Installing a package leaves state described but not built: modprobe reads depmod's binary indexes,
udev reads a compiled hardware database and never the sources beside it, and some distributions
generate their locale archive from a list. On a running system scriptlets and boot-time units produce
that; an image being assembled has neither, so each generator is an operation of its own, placed after
whatever it acts on. The compositions run all three for you (below); an `image.layer()` places them itself:

```Starlark
image.layer(
    name = "appliance",
    package_manager = ":image.package-manager",
    package_sets = ["bootable"],
    ops = [
        image.ops.install_from(":project.install"),
        image.ops.depmod(),
        image.ops.hwdb(),
    ],
)
```

- `image.ops.depmod()` rebuilds `modules.dep` and its `.bin` indexes for every kernel the image installs, and
  does nothing for an image that installs none. It runs the box's `depmod` with the image's `depmod.d`
  configuration, so the image's search order applies. The box's `depmod` would look for an `external`
  module directory, and for the target of an absolute symlink, in the box. A `depmod.d` file that lists an
  `external` directory, or a module that is an absolute symlink, therefore fails the build.
- `image.ops.hwdb(usr = True, strict = True)` compiles `hwdb.d` into the binary database udev actually reads.
  It writes `/usr/lib/udev/hwdb.bin`, where the image ships it and nothing writable shadows it, and drops
  the `/etc` copy; `usr = False` writes that copy instead. `strict = False` accepts a source file the
  image cannot parse. An image with no `hwdb.d` is left alone.
- `image.ops.locale_gen()` generates the locales that `/etc/locale.gen` lists into
  `/usr/lib/locale/locale-archive`, which is how Debian and Arch generate locales. It does the work of the
  distribution's `locale-gen` with the box's `localedef`, the image's locale sources and the image's
  charmaps. A charset can be a charmap's alias, such as `LATIN1`. An entry without a charset, a locale or
  charset that the image has no definition for, or a warning from `localedef` fails the build, where
  `locale-gen` would skip the entry. A non-empty `/etc/locale.gen` replaces the archive, even when every
  line in it is a comment. A distribution that installs locales as packages has no such file, and the
  operation does nothing.

The generators run box tools against the mounted image, exactly as `image.ops.run()` does by default, so an
image that installs no kmod or systemd of its own still gets its indexes and database. The box is the tools
box of the image's release. It installs its packages from the release's repositories, so its tools match an
image that installs kmod, systemd and glibc from those repositories. The `additional_repositories` and
`local_packages` of a package manager do not reach the box: an image that installs kmod, systemd or glibc
from them still gets the release's `depmod`, `systemd-hwdb` and `localedef`. The layer that runs a generator
captures what it writes, so it is built once and cached rather than repeated by every terminal output.

`image.archive`, `image.portable`, `image.disk`'s root filesystem, and `image.initrd` end their operations
with `image.ops.depmod()`, `image.ops.hwdb()` and `image.ops.locale_gen()` at their defaults, because a
composition declares a whole product rather than one layer: the three cover everything a package can leave
described, and each does nothing on an image carrying none of it. They land on the layer the composition
declares, so a composition handed a `parent` and given no operations or packages of its own declares no layer
and places none; that parent is where they belong, after the operations whose packages they read. An initrd
generates before it prunes, so it ships the compiled database rather than the sources it was compiled from.
Naming a generator in `ops` overrides the composition's copy instead of adding a second, so
`image.ops.hwdb(usr = False)`, or an `image.ops.depmod()` placed before the operations that strip modules, is
taken exactly as written. The extension rules (`image.sysext`, `image.confext`, `image.extension`) run none
of them. An extension merges onto a system it does not own. A database generated in the extension would
replace the database of that system in the merged tree, and would go stale when that system changes.

### Declaring the initrd

A bootable `image.disk` declares a conventional initrd for itself, so nothing is required to get one.
Declare an `image.initrd()` and pass it when you want to change what is in it or how it is packed:

```Starlark
image.initrd(
    name = "os.initrd",
    package_manager = ":image.package-manager",
    packages = ["cryptsetup"],
    ops = [image.ops.copy(":modprobe.conf", "/usr/lib/modprobe.d/local.conf")],
    compression = "none",
)

image.disk(
    name = "os",
    initrd = ":os.initrd",
    ...
)
```

`packages`, `package_sets`, and `ops` are added to the defaults, never replace them: the release's `initrd`
package set and the operations every initrd needs (`/init`, `/etc/initrd-release`, and the databases an
initrd never reads) apply either way. `compression` picks how the cpio the UKI carries is packed, default
`zstd`, and `strip_pkgdb` (default true) keeps the package database out of it while `[pkgdb]` still reports
it from the tree. `install_docs` and `install_langs` default the other way round from an OS image, since
documentation and translations only cost an initrd boot memory.

Every initrd can import system credentials, such as an SSH key, from a cloud's instance metadata service
(IMDS). systemd's IMDS generator enables that (together with networkd to reach the service) on a cloud
instance which it recognizes from the hardware database; `systemd.imds=` on the kernel command line
overrides it.

The target is an ordinary image: it builds on its own, publishes `[pkgdb]` and `[sbom]`, and one initrd can
be shared by several disk images.

### Attaching install operations to a target

How a project installs is a property of the project, not of each image carrying it. `image.install()`
attaches packages and operations to a target, and an image applies both with one `image.ops.install_from()`:

```Starlark
image.install(
    name = "project.install",
    packages = ["glibc"],
    ops = [
        image.ops.copy(":project[project-cli]", "/usr/bin/project-cli"),
        image.ops.copy(":project.checkout[tmpfiles.d]", "/usr/lib/tmpfiles.d"),
    ],
)

image.disk(
    name = "os",
    package_sets = ["bootable"],
    ops = [image.ops.install_from(":project.install")],
    ...
)
```

A target declaring its own `packages` is what keeps every image carrying it from having to know: they
join the installing layer's own request, so two targets that both need packages compose without the
caller merging anything. Any operation may be attached, not only copies, and the operations are spliced
in place, so the image still decides where in its own order they land. An `image.install` target may
itself `image.ops.install_from()` another, which composes; a cycle is rejected by Buck as a target cycle.

### Installing a file a project ships as a template

A project that expects its build system to fill in a prefix or a port commits the file with markers and a
`sed` in its install recipe. `image.substitute()` runs that expansion as a build action, so the values live
in the declaration that installs the file and the result is an artifact like any other:

```Starlark
image.substitute(
    name = "project-http.service",
    src = ":project.checkout[contrib/project-http.service.in]",
    replacements = {"@bindir@": "/usr/bin", "@port@": "555"},
)

image.ops.copy(":project-http.service", "/usr/lib/systemd/system/project-http.service")
```

The output is named after the target. Each placeholder has to appear in the template, so a project
renaming one fails the build rather than leaving a marker in an installed file, and the template's mode
carries over, so a substituted script stays executable.

Every `image.ImageInfo` carries its canonical lazy SBOM artifacts and its file manifest, and a package
database whenever the image has a package manager. The same artifacts are exposed as subtargets, and
`image.ImageSbomInfo` remains available for consumers that need only the SBOM formats:

```text
//examples/image:chained-base
├── [manifest]
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]
```

The `version` attribute sets the SBOM source version and defaults to `"0"`. Merely building the image still
produces only its latest delta. Selecting `[pkgdb]` runs the image's package system's capture driver;
selecting either nested SBOM format runs one shared scan of the completed stack.

`[manifest]` is a [UAPI.16 File Manifest](https://uapi-group.org/specifications/): an RFC7464 JSON-SEQ
listing of every path the assembled tree holds, with the type, mode, ownership and clamped mtime of each,
a SHA256 over every regular file's contents, symlink targets carried inline, and one `inodeToken` per
inode two names share. It answers what an image ships rather than what it was built from, so a consumer
can compare the paths of two images against each other (a path in both a system extension and the /usr it
merges onto shadows an OS file) or account for size per file. The artifact is named `Uapi16Manifest`,
which is the name the format reserves, so it can be dropped beside a tree of the image it describes.

`image` also takes `install_langs`: keep translated files only for these languages, instead of all of them.
Nothing matches a value that is not a language, so `install_langs = ["C.UTF-8"]` installs no translations
at all. `install_docs = False` likewise installs no documentation, keeping the licenses that packages ship.
`image.initrd()` defaults to both. Both configure an install, so a layer that installs nothing ignores them.

## Terminal outputs

Logical images and terminal outputs are separate rule families. Terminal rules merge the layer stack only
when needed:

- `image.directory` materializes a Buck directory artifact and provides `image.ImageDirectoryInfo`;
- `image.uki` builds the unified kernel image for the image's single installed kernel from one or more cpio
  `image.ImageArchiveInfo` dependencies and provides `image.UkiInfo`, named `<image_id>_<version>_<arch>.efi`
  (defaults: target name and `0`; systemd architecture spelling, e.g. `x86-64`), the shape
  systemd-sysupdate UKI transfers match. After those dependencies it appends one further initrd of its
  own, holding the kernel modules `initrd_modules` selects, so that none of the initrds handed to it has
  to carry modules for the kernel in question (see "Kernel modules in the UKI"). Alternative kernel
  command lines are `image.uki_profile()` descriptors, which add boot profiles as separate sd-boot menu
  entries, each appending its arguments to the base kernel command line. `splash` names the BMP image to
  embed for the EFI stub to display while booting. With a `secure_boot_key`, the UKI and its embedded
  kernel are signed
  for Secure Boot. With a `sign_expected_pcr_key`, a signed expected-PCR 11 policy covers each profile;
  set its `sign_expected_pcr` to false to opt out;
- `image.bootable` selects a kernel and matching initrd from a logical image, exposed as `[uki]`, `[kernel]`,
  and `[initrd]` subtargets;
- `image.vm` runs the raw image ephemerally with the `systemd-vmspawn`, QEMU and OVMF stack of the tools box
  of the image's release, and binds the DDIs in `extensions` into the guest, where systemd merges them at
  boot: a sysext from `/var/lib/extensions`, a confext from `/var/lib/confexts`, and a DDI of both classes
  from both. Each must have the `system` scope. It binds the DDIs in `portables`, portable images and
  extensions with the `portable` scope, at `/var/lib/portables`, where `portablectl attach` finds them by
  name. Each DDI is named `<name>.raw` in the guest. With `secure_boot`, vmspawn picks Secure Boot capable
  firmware without pre-enrolled keys, so an image carrying `loader/keys/auto` enrollment files enrolls them
  on first boot and then boots with Secure Boot enforced, and attaches a software TPM so the UKI's signed
  expected-PCR policy is measured.

`image.archive` builds one logical image from operations and writes it as a deterministic tar or newc cpio
archive, optionally zstd-compressed (`compression = "zstd"`). `image.sysext`, `image.confext` and
`image.extension` are the compositions for extension DDIs (see "Extensions" below), and `image.portable` is
the composition for portable service images (see "image.portable" below). `image.archive`, the extension
rules, `image.portable` and `image.disk` start their image from `package_manager`, or add their
operations on top of the image in `parent`. Without either, they start their image from the package manager
of the distribution that the build chooses. Every composition takes `install_docs` and passes it to the image
it builds.

Each composition publishes one product target that also provides its logical filesystem as `image.ImageInfo`.
`image.archive` defaults to its archive and provides `image.ImageArchiveInfo`, which names the format of the
archive. An extension defaults to its DDI and provides `image.DdiInfo`. `image.archive` and the extension
rules expose their supply-chain artifacts as subtargets:

```text
//examples/image:demo
├── [manifest]
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]

//examples/image:demo-ext
├── [manifest]
├── [pkgdb]
└── [sbom]
    ├── [spdx]
    └── [cyclonedx]
```

The product targets provide `image.ImageSbomInfo` for typed consumers. Buck builds an optional artifact
only when it is requested, so declaring these views costs nothing on a default build.

### Extensions

`image.sysext()` builds a system extension, which contains `/usr` and `/opt`. `image.confext()` builds a
configuration extension, which contains `/etc`. `image.extension()` builds a DDI of either class or of
both, named in `classes`:

```Starlark
image.sysext(
    name = "debug-tools",
    parent = ":appliance",
    packages = ["strace", "gdb"],
)

image.confext(
    name = "site-config",
    parent = ":appliance",
    ops = [image.ops.copy("chrony.conf", "/etc/chrony.conf")],
)

image.extension(
    name = "webapp-cni",
    parent = ":webapp",
    classes = ["sysext", "confext"],
    scope = ["portable"],
    ops = [image.ops.copy("cni", "/")],
)
```

With `parent`, the DDI contains only what the target adds on top of `parent`. Only the `tmpfiles` lines that
the target adds apply to the DDI, so a line of `parent` creates nothing in it. `parent` can be any image
except another extension. Without `parent`, the DDI contains the whole tree of the image. The DDI is an
erofs root partition, its verity hash partition and, with `verity_key`, a signature over the verity root
hash. A host validates the signature against the certificates in its `/usr/lib/verity.d/`, and enforces it
only where its image policy says so (see "Example targets" below). The DDI contains no os-release and no
package database.

The rule writes one extension-release file per class:
`/usr/lib/extension-release.d/extension-release.<name>` for a sysext and
`/etc/extension-release.d/extension-release.<name>` for a confext. Each file contains:

- `ID` and `VERSION_ID` from the os-release of `parent`. A parent without `VERSION_ID`, such as Arch
  Linux, gives only `ID`. Without `parent`, `ID=_any`, which matches any host.
- `SYSEXT_SCOPE` or `CONFEXT_SCOPE`, from `scope`. The scope lists where systemd applies the extension.
  `system` merges it on the host and `initrd` in the initrd. `portable` lets
  `portablectl attach --extension` or `ExtensionImages=` add it on top of a portable service image. The
  default is `["system"]`.
- `SYSEXT_ID` or `CONFEXT_ID` with the target name, `SYSEXT_VERSION_ID` or `CONFEXT_VERSION_ID` and
  `IMAGE_VERSION` with `version`, and `ARCHITECTURE` from `arch`.
- `EXTENSION_RELOAD_MANAGER=1`.

`release` overrides any of these fields in every file, except the scope: set it with `scope`, which
`image.vm` checks.

The DDI is named `<name>_<version>_<arch>.sysext.raw`, `<name>_<version>_<arch>.confext.raw`, or
`<name>_<version>_<arch>.raw` for both classes. A systemd-sysupdate transfer matches these names.
systemd takes the image name from the file name up to the first `_` and looks up `extension-release.<name>`
by it, so a target name containing `_` or `+` fails the build. A deployed DDI can drop the rest of its
name, such as `debug-tools.raw`. With `arch = "_any"`, the DDI is named `<name>_<version>_all` and
`ARCHITECTURE=_any` matches any host architecture.

The target defaults to its DDI and provides `image.DdiInfo`, like `image.disk`, and
`image.ExtensionInfo` with its classes and scope. `[roothash]` is the verity root hash, and
`[disk-manifest]` lists the files the DDI contains. The rule publishes both with the DDI.

### image.portable

`image.portable()` builds a portable service image for `portablectl attach` and `RootImage=`. The DDI
contains the whole tree of the image, including its os-release, in an erofs root partition with its
verity hash partition and, with `verity_key`, a signature over the verity root hash. Extensions for it
are `image.extension()` targets with `parent` set to the portable image and `scope = ["portable"]`:

```Starlark
image.portable(
    name = "webapp",
    package_manager = ":image.package-manager",
    packages = ["bash", "coreutils", "python3"],
    ops = [
        image.ops.copy("webapp.service", "/usr/lib/systemd/system/webapp.service"),
        # webapp.service sets StateDirectory=webapp.
        image.ops.mkdir("/var/lib/webapp"),
    ],
)

image.extension(
    name = "webapp-config",
    parent = ":webapp",
    classes = ["confext"],
    scope = ["portable"],
    ops = [image.ops.copy("webapp.conf", "/etc/webapp/webapp.conf")],
)
```

systemd cannot create a mount point on the read-only root partition, so every path that systemd binds
into the service must exist in the image. The portable profiles bind `/etc/machine-id` and
`/etc/resolv.conf` from the host, and the rule adds an empty file at each path. An operation in `ops` that
writes `/etc/machine-id` or `/etc/resolv.conf` replaces the empty file. `StateDirectory=`, `CacheDirectory=`
and `LogsDirectory=` bind directories under `/var` from the host. Create the directories for the service's
settings with `image.ops.mkdir()`, as in the example above. A tmpfiles.d snippet in the image cannot create
the directories, because systemd-tmpfiles cannot write to the read-only root partition at run time. [Portable
Services](https://systemd.io/PORTABLE_SERVICES/) lists what else an image needs, such as `/proc` and
`/tmp`, which the base package of a distribution installs. Like `image.archive`, the rule runs the
generators (see "Generating what packages only describe").

The DDI is named `<name>_<version>_<arch>.raw`. portablectl takes the image name from the file name up to the
first `_`, and attaches the units whose names start with that image name: `webapp.service`,
`webapp-worker.service` or `webapp@.service`. The target defaults to its DDI and provides `image.DdiInfo`,
with the same `[roothash]` and `[disk-manifest]` subtargets as an extension. It takes an optional
`package_manager` or `parent`, the operations and packages of any composition, and `version`, `arch`,
`seed` and `verity_key`.

### image.disk

`image.disk()` composes the partitions in `definitions` into a GPT disk. A bootable disk also contains
systemd-boot, the initrd and versioned UKIs in its ESP. Most attributes parameterize the terminal rules
described above.

Required attributes:

- `ops` (operation list) and `tmpfiles` (list of tmpfiles.d lines): Build the root filesystem layer;
  passed on to `image.layer`.
- `definitions` (list of `image.partition()` descriptors): Partition layout; `image.DEFAULT_ROOT_PARTITIONS`,
  `image.DEFAULT_USR_VERITY_PARTITIONS`, and `image.DEFAULT_SIGNED_USR_VERITY_PARTITIONS` are reusable
  conventional layouts; a bootable disk needs system and ESP partitions.

Optional attributes:

- `bootable` (bool): Makes the disk boot. It defaults to whether `definitions` contain an ESP partition, and
  `True` without one fails the build. A disk that does not boot refuses `initrd`, `profiles`, `splash`,
  `cmdline`, `initrd_modules`, `secure_boot_key` and `sign_expected_pcr_key`.
- `disk_seed` (string): Seeds stable partition UUIDs.
- `verity_key` (target providing `image.SigningKeyInfo`): Signs the verity signature partition.
- `secure_boot_key` (target providing `image.SigningKeyInfo`): Signs the UKIs and systemd-boot; see "Secure
  Boot signing" below.
- `virtual_size` (size string such as `"20G"`): The size of a virtual disk (`[qcow2]` or `[vmdk]`). A
  system booted from it can claim the extra space during first boot, e.g. for a root partition. The raw
  image stays at the size of its partitions. As with `image.vm`'s `grow`, the extra space sits past the GPT
  backup header until something rewrites the table. A size the partitions do not fit in fails the build.
- `mkfs_options` (dict of filesystem to option list, default `{}`): Options `mkfs` is given when
  creating a partition of that filesystem. The disk hands each list to
  `systemd-repart` as `SYSTEMD_REPART_MKFS_OPTIONS_<FSTYPE>`. Naming a filesystem the disk never formats
  fails the build rather than going nowhere, and an option holding whitespace is refused because repart
  splits the variable on it. This is where a disk's compression is really decided: an `image.partition()`'s
  `compression` only picks the algorithm, so an erofs partition left at the defaults comes out
  substantially larger than one built the way the extension rules build their own, which is
  `["-zzstd,level=3", "-C524288", "-Efragments,ztailpacking,dedupe"]`. `["--invariant"]` for vfat makes an
  ESP byte-stable across rebuilds, which `mkfs.fat` otherwise is not, because it stamps the volume label
  entry with the wall clock even under `SOURCE_DATE_EPOCH`.
- `strip_pkgdb` (bool, default `False`): Leaves the package database out of the system partitions, for a
  system that ships without its package manager and never resolves a package again. The logical image keeps
  it, so `[pkgdb]` still captures the database and `[sbom]` still reports every installed package rather than
  what a binary scan can guess. The ESP carries no database either way.
- `sign_expected_pcr_key` (target providing `image.SigningKeyInfo`): Seals the expected-PCR policy;
  without one the policy is not sealed. See "Secure Boot signing" below.
- `package_manager` (target label): The package manager the disk starts its image from. See "Declaring an
  image" above. Without `package_manager` and `parent`, the disk uses the package manager of the
  distribution that the build chooses.
- `parent` (target label providing `image.ImageInfo`): The logical image the disk extends, instead of the
  `package_manager` it would otherwise start one from. A disk can't have both. This is what
  lets something else build on the same content the disk boots: an `image.sysext()` whose `parent` is
  that image extends the content of the disk, and the disk can then copy the resulting DDI to `/boot`
  with `image.ops.copy()` in `ops`, where making the extension's parent the disk itself would be a
  dependency cycle. A disk extending a parent
  declares its own `initrd`, because the conventional one is declared from a package manager it no longer
  names, and the parent places the generators below itself, because only a composition places them for a
  caller.
- `initrd` (target label providing `image.InitrdInfo`): The initrd to boot, normally an `image.initrd()` (see
  below). Given none, `<name>.initrd` is declared for you with the conventional defaults, inheriting this
  target's package manager, version, and distribution. The rule republishes the package database and SBOM
  the initrd image already carries. The cpio omits the package database, since nothing in an initrd reads
  it; `[initrd][pkgdb]` still captures it from the image's own tree. It needs no kernel modules:
  `initrd_modules` selects those and the UKI carries them in an initrd of its own.
- `cmdline` (string list): Kernel command line arguments, default
  `["root=tmpfs", "mount.usr=dissect", "rw"]`; passed on to `image.uki()`.
- `initrd_modules` (glob pattern list): The kernel modules the UKI carries, default
  `image.DEFAULT_INITRD_MODULES`; see "Kernel modules in the UKI"; passed on to `image.uki()`.
- `profiles` (`image.uki_profile()` descriptor list): Alternative sd-boot menu entries, passed on to
  `image.uki()`.
- `splash` (source target or absolute image path): BMP image embedded in the UKI and displayed by the EFI
  stub while booting; passed on to `image.uki()`. An absolute path names a file the image ships, so one
  declaration can pick each distribution's own logo:

  ```starlark
  splash = distribution.select({
      "DEFAULT": None,
      "arch": "/usr/share/systemd/bootctl/splash-arch.bmp",
      "debian": "splash.bmp",
  }),
  ```

  Either way the file must be a BMP; the build fails on any other format, and on an image path the
  image does not ship.
- `arch` (string): Architecture; defaults to the one the image is built for; passed on to `image.uki()`.
- `install_docs` (boolean): Passed to the root filesystem layer only; an `image.initrd()` carries its
  own, defaulting to no documentation.
- `image_id` (string): The image identity, stamped into the image's os-release as `IMAGE_ID`.
  Defaults to the target name; a product should set it explicitly so that renaming a Buck target
  cannot re-identify the installed OS (systemd-sysupdate matches partitions and UKIs by this
  identity at run time).
- `version` (string): Declared image version. Default `"0"`; stamped into the image's os-release as
  `IMAGE_VERSION` and used as the SBOM source version. Together with `image_id`
  it also names the UKI (`<image_id>_<version>_<arch>.efi`) and renders partition label placeholders.

The identity stamp is applied in its own thin layer between the root filesystem layer and everything
derived from it, so building the same target with a different `version` re-runs only the artifacts
that embed the version (`/usr` partition and verity, UKI, ESP, disk) while package installation and
the caller's operations stay cached.

The version carries a contract: systemd-sysupdate identifies an update *purely* by the version in the
partition labels and the UKI filename, so every published build must carry a new, higher version. A
rebuild under an unchanged version puts different content behind identical names, which sysupdate
cannot distinguish from the release a device already installed, and so never applies. The release pipeline
that publishes update artifacts must enforce version immutability by rejecting an already-published version.

The requested target name is one bootable-image result whose default output is the raw disk. Other terminal
views and supply-chain artifacts are lazy subtargets:

```text
//examples/image:boot-demo
├── [uki]
│   └── [modules]
├── [directory]
├── [qcow2]
├── [raw.zst]
├── [vmdk]
├── [ami]
├── [manifest]
├── [pkgdb]
├── [sbom]
│   ├── [spdx]
│   └── [cyclonedx]
├── [initrd]
│   ├── [manifest]
│   ├── [pkgdb]
│   └── [sbom]
│       ├── [spdx]
│       └── [cyclonedx]
├── [boot]
│   ├── [kernel]
│   ├── [initrd]
│   └── [uki]
├── [roothash]
└── [partitions]
    ├── [usr]
    └── [usr-verity]
```

The disk and its re-encodings are files named `<image_id>_<version>_<arch>.<ext>`, so they keep the image
identity when copied out of the build, exactly matching the UKI's `<image_id>_<version>_<arch>.efi`.
The `[qcow2]`, `[raw.zst]` and `[vmdk]` subtargets re-encode the raw disk into a compact qcow2, a compressed
raw, or a compressed (streamOptimized) VMDK for VMware and cloud importers on demand, each publishing one
`image.DiskConversionInfo`; the encodings are reachable only through those
subtargets, because one result cannot carry the same provider type twice. `[ami]` publishes the parameters
an Amazon Machine Image of the raw disk is registered with, see [aws.md](aws.md). The target has no aggregate
bootable-image provider: it returns `image.ImageInfo`, `image.DdiInfo`, `image.InitrdInfo`,
`image.ImageDirectoryInfo`, `image.UkiInfo`, and `image.PublishedInfo` independently. A disk that does not
boot has no `[uki]`, `[initrd]`, `[boot]` or `[ami]` subtargets, returns no `image.InitrdInfo` or
`image.UkiInfo`, and publishes no UKI, kernel or initrd.

`image.PublishedInfo` is what a target contributes to a release, keyed by the name it is published under:
for a disk the raw image, the UKI, the kernel and the initrd, plus the partitions, and for an
extension its DDI. Each rule names what it builds, so anything gathering a release reads those
names instead of composing names of its own. Each name maps to an `image.PublishedFile`, which holds the
artifact, its kind and its optional subkind. The provider's required `image` field names the image they
belong to, and its optional `version` field gives the version of that image. The release's description
below lists the kind, subkind, image and version of every file. A partition is the exception and travels
as a typed `PartitionInfo`: its name contains the type and UUID repart assigned, so it exists only after
the build, and a consumer reads it back out of the metadata written beside the partition. The ESP is not
published: what it carries is transferred by other means, and no update writes the partition back. The format
subtargets publish themselves rather than travelling in the disk's own set, so gathering a release does not
build every encoding of it. So do the metadata views: `[sbom]` publishes both formats and `[pkgdb]` one file
named after the format its package system keeps the database in, each named after the image they describe,
and for a disk `[initrd][sbom]` covers what only early boot has.
A scanner then reads a release without unpacking anything in it, and a release that lists no view builds
none.

`image.artifacts` gathers those contributions into one directory of symlinks, which is a release as a
consumer sees it:

```Starlark
image.artifacts(
    name = "release",
    targets = [":image", ":image[qcow2]", ":image[sbom]", ":image[pkgdb]", ":demo-ext"],
)
```

```text
artifacts_x86-64.json
demo-ext_0_x86-64.sysext.Uapi16Manifest
demo-ext_0_x86-64.sysext.raw
demo-ext_0_x86-64.sysext.roothash
image_0_x86-64.Uapi16Manifest
image_0_x86-64.cdx.json
image_0_x86-64.efi
image_0_x86-64.esp.Uapi16Manifest
image_0_x86-64.initrd
image_0_x86-64.pkgdb.sqlite
image_0_x86-64.qcow2
image_0_x86-64.raw
image_0_x86-64.spdx.json
image_0_x86-64.usr-x86-64.0a72787674a137eaabf7aa97c82a72ee.raw
image_0_x86-64.usr-x86-64-verity.94dcef64374b73274b88fe1a9ff45b31.raw
image_0_x86-64.usr-x86-64-verity-sig.cb66a98640e5450aa028a8bf473ac0ea.raw
image_0_x86-64.usr.Uapi16Manifest
image_0_x86-64.vmlinuz
```

The rule decides no name of a built file: it unions what each target published and reads a partition's name
back from the metadata beside it, which is why it assembles the directory from a dynamic action. The only
name it adds is its description's, see below. Two targets publishing one name is an error rather than a
silent overwrite. The `usr` and `usr-verity` UUIDs above are the two halves of that build's verity root hash,
and `%M_@v_%a.usr-%a.@u.raw` in a transfer is how systemd-sysupdate reads one back to give the partition it
writes the UUID dissection pairs them by.

The release carries a description of its files, `artifacts_<arch>.json`. The `arch` in its name and in its
top-level `arch` key is the architecture the release is built for, spelled as systemd does, as in `x86-64`
or `arm64`. It is part of the name so that the releases of several architectures can share one directory.
The `files` key maps each published name to an entry with these fields:

- `kind`: This says what kind of file it is, and the optional `subkind` refines it. A consumer that does not
  care about the `subkind` matches on the `kind` alone. These are the kinds and their subkinds:
  - `disk` with the format as its subkind, `raw`, `qcow2`, `raw.zst` or `vmdk`.
  - `partition` with the type its definition gives as its subkind, such as `usr`, `usr-verity`,
    `usr-verity-sig` or `root`.
  - `uki`, `kernel` and `initrd`, which have no subkind.
  - `extension` with its classes as its subkind, `sysext`, `confext` or `sysext+confext`.
  - `portable`, which has no subkind.
  - `roothash` is the verity root hash file of an extension or a portable image and has no subkind.
  - `sbom` with the format as its subkind, `spdx` or `cyclonedx`.
  - `pkgdb` with the package system as its subkind, `rpm`, `deb` or `pacman`.
  - `listing` with the subkind `uapi16manifest`.
  - `ami` is the [AMI](aws.md) registration parameters and has no subkind.
- `image`: This names the image the file belongs to. All files of one image share this value, so a consumer
  can find the usr partition that goes with a UKI, or the SBOM that describes an extension, without parsing
  names. For a disk it is the prefix the disk's files are published under, `image_0_x86-64` above. For an
  extension or a portable image it is the DDI's file name without `.raw`, `demo-ext_0_x86-64.sysext`
  above, so that a sysext and a confext of one name are two images. The initrd's SBOM and package database
  describe the initrd rather than the disk, so they form an image of their own named `<prefix>.initrd`. The
  initrd file itself stays in the disk's image.
- `root_hash`: This is the verity root hash of the partition verity protects in a disk, or of an extension
  or a portable image. Other files have no `root_hash`.
- `version`: This is the version of the image the file belongs to, the same string as in the file's name:
  `0` above, or a version rendered from git such as `0.0.0^573-160c7ddc0-d`. All files of one image share
  it. Each image declares its own version, so two images in one release can have different versions. A
  file whose `image.PublishedInfo` gives no version has no `version`.

A consumer can match on these fields rather than file names. The excerpt below also gathers
`:image[initrd][sbom]`.

```json
{
  "arch": "x86-64",
  "files": {
    "demo-ext_0_x86-64.sysext.raw": {
      "image": "demo-ext_0_x86-64.sysext",
      "kind": "extension",
      "root_hash": "…",
      "subkind": "sysext",
      "version": "0"
    },
    "image_0_x86-64.cdx.json": {
      "image": "image_0_x86-64",
      "kind": "sbom",
      "subkind": "cyclonedx",
      "version": "0"
    },
    "image_0_x86-64.efi": {
      "image": "image_0_x86-64",
      "kind": "uki",
      "version": "0"
    },
    "image_0_x86-64.initrd.spdx.json": {
      "image": "image_0_x86-64.initrd",
      "kind": "sbom",
      "subkind": "spdx",
      "version": "0"
    },
    "image_0_x86-64.usr-x86-64.0a72787674a137eaabf7aa97c82a72ee.raw": {
      "image": "image_0_x86-64",
      "kind": "partition",
      "root_hash": "0a72787674a137eaabf7aa97c82a72ee94dcef64374b73274b88fe1a9ff45b31",
      "subkind": "usr",
      "version": "0"
    }
  }
}
```

`image.DdiInfo` contains the optional `image.RootHashInfo` when verity is enabled. The completed
`image.ImageInfo` includes the ESP layer, so another image can use the bootable image as its parent without
relying on a generated
helper label. The same `image.InitrdInfo`, containing its logical `image.ImageInfo` and derived
`image.ImageArchiveInfo`, is the sole typed provider published by `[initrd]`. Merely carrying an artifact
in a provider does not build it;
optional actions run only when a consumer uses the artifact or a user selects its subtarget.
`[directory]` has the same representability limit as `image.directory`: it fails when the completed tree
contains a path, such as a systemd-escaped unit name, that Buck directory artifacts cannot store.

`[boot]` is what the disk boots, as files: the `bootable` rule run against the composition's own ESP, so a
direct kernel boot or a publisher needs no second target pointing back at it. Its `[kernel]` and `[initrd]`
come out of the selected UKI's PE sections, which is why `[boot][initrd]` and `[initrd]` differ: the latter
is the initrd image this composition was given, while the former also carries the kernel modules the UKI
adds for the kernel it selected. `[boot][uki]` is the single selected UKI, where `[uki]` is the directory of
every UKI the image ships.

The nested metadata describes the initrd, which resolves its own package closure and may therefore contain
packages that the root filesystem does not install. Nothing scans the initrd once it is a cpio inside the
UKI's PE, so it carries its own artifacts rather than being folded into the root filesystem's. Keeping them
separate also preserves the distinction a vulnerability triage needs: a package reachable only during early
boot is not exposed the way the same package in the running system is. The union of the two accounts for
everything in the UKI, provided the image keeps the kernel package installed in its own tree, which is where
the UKI's kernel and modules come from.

### Kernel modules in the UKI

A UKI carries the kernel, the initrds it was built from, and one further initrd the `image.uki` rule
assembles for that kernel alone, holding kernel modules. `initrd_modules` selects those modules from the
image's own `/usr/lib/modules/<kver>`, and the build adds what they depend on and the firmware they ask for.
This
initrd is appended after the ones passed in, so no initrd has to be built for a particular kernel: an
image given to `image.disk` as `initrd` needs no kernel modules of its own, and gets the ones
selected here.

The default is `image.DEFAULT_INITRD_MODULES`, exported from `//image:defs.bzl`. It is a core set rather than
every module the kernel package ships, because only the path to `/usr` has to work from the UKI: `/usr`
keeps the complete set, the erofs partition compresses it, and the system loads any other module from
there once it has switched root. The core set covers the usual ways a machine reaches its root
filesystem, meaning AHCI, NVMe, SCSI and USB storage, the virtio devices a VM is given, device-mapper
including dm-verity, the filesystems these images use, and keyboard and console input so that a rescue
shell works. It does not cover enterprise storage controllers such as SAS and RAID adapters, a root
filesystem reached over the network, or any device whose driver needs firmware. An appliance that boots
through one of those has to name it:

```Starlark
image.disk(
    # The core set, plus a controller this appliance boots from, minus a filesystem it never mounts.
    initrd_modules = image.DEFAULT_INITRD_MODULES + ["mpt3sas", "-btrfs"],
)
```

Patterns written without `image.DEFAULT_INITRD_MODULES` replace it rather than extending it: `["*"]` carries
every module the image installs, and `[]` carries none.

A pattern that matches no module is reported rather than fatal, counted on stderr and named in the
manifest below, because one pattern list meets kernels that ship different sets of modules and an entry
naming something a given kernel does not have is ordinary. Naming a module the kernel builds in counts
as a match and packs nothing, since the kernel already holds it.

Each pattern is matched against a module's path below `/usr/lib/modules/<kver>`, with its `.ko`, `.ko.gz`,
`.ko.xz` or `.ko.zst` suffix removed and `_` and `-` treated as one character, as kmod treats them:

| pattern              | matches                                                            |
|----------------------|--------------------------------------------------------------------|
| `loop`               | the basename                                                       |
| `block/loop`         | a trailing run of path components                                  |
| `/kernel/block/loop` | the whole path, anchored at the root of the module directory       |
| `crypto/`            | everything below that directory                                    |
| `raid[0-9]*`         | shell globs (`*` crosses `/`)                                      |
| `-nouveau`           | excludes; patterns are evaluated in order and the last match wins  |

Firmware follows the modules: whatever a selected module declares is packed alongside it, symlinks
included, as is the firmware of modules the kernel has built in, since those load it from the initrd too.
An image that installs no firmware therefore ships none, and one that installs `linux-firmware` and asks
for every module gets all of it.

`[uki][modules]` is the record of that whole decision, as JSON, and it is where to start when a UKI boots
to no root. Alongside the kernel version and the patterns the target asked for, it names the ones that
matched nothing, the dependencies the image does not install and the firmware nothing satisfies, then
totals the modules, the firmware, the content bytes and the size of the archive itself. Every packed path
is listed with what it is (`module`, `firmware`, `index`, `vdso` or `directory`),
its size, and why it is in there: `selected` for one a pattern named, `needed_by` for one the closure
pulled in, and `declared_by` for firmware, each naming the modules responsible. The driver prints only
counts as it builds, since the names are all here.

### Image versioning

A version derived from the current commit or any other dynamic query cannot be computed inside the build
graph, so it arrives as build configuration and is rendered where the image is declared. It takes three
shapes against the latest `v*` tag (or 0.0.0 if there is no tag):

| build    | git state                     | version            |
|----------|-------------------------------|--------------------|
| release  | checkout of v1.4.2            | `1.4.2`            |
| snapshot | 3 commits past the tag        | `1.4.2^3-08f2c4`   |
| dirty    | uncommitted work on top       | `1.4.2^3-08f2c4-d` |

systemd version comparison orders these correctly (`tag` < `tag^count-hash` < `tag^count-hash-d` <
`nexttag`), which systemd-sysupdate needs to recognize an update. Right on a tag it reads the same way:
`1.4.2` < `1.4.2-d` < `1.4.2^1-08f2c4`. The hash names the commit for tracking; it shrinks (never below 4
characters) until the longest partition label still fits GPT's 36-character limit, and the marker is
inside what it shrinks against, which is why it is `-d` and not `-dirty`.

The marker is a bit, not a measure: it says the checkout held more than its commit, not how much of it or
for how long. That is what keeps it out of the inner loop, since it flips on the first uncommitted change
and then stands still however far the work goes. A version that measured the work instead, seconds since
the commit or a digest of it, would be a different string on every build, and every image carrying one
would be rebuilt for it whether or not anything that image is built from had changed. So two dirty trees
on one commit render one version; commit to tell them apart.

The split between the two halves runs along what Buck can ask for itself. [`bin/tine`](../bin/tine)
queries git, which the graph cannot, and writes what a version is made of into its own block in
`.buckconfig.local` once per command:

```ini
[tine]
version-base = 1.4.2
version-count = 3
version-height = 43
version-commit = 08f2c4d9ab7e...
version-dirty = 1
```

Rendering those into the version above stays inside the graph, in `image/version.bzl`, because the hash
length is a question only the image being versioned can answer: a project with several images has one git
state but one label budget per image, and a single rendered version would have to shrink every image's
hash to the tightest of them. `version-dirty` is written only for an uncommitted tree, so its absence is
what says a checkout held nothing but what its commit holds.

An image asks for that version with `version = "auto"`, or names its own base with
`version = "auto:<base>"` and takes only what tells one build of that base from the next:

```Starlark
image.disk(
    name = "demo",
    definitions = image.DEFAULT_SIGNED_USR_VERITY_PARTITIONS,
    version = "auto:1.4.2",
    ...
)
```

For an image whose labels carry no version, and which therefore keeps the whole 12-character hash:

| declaration       | at tag v2.0.0            | 3 commits past it        | dirty, 3 commits past it  |
|-------------------|--------------------------|--------------------------|---------------------------|
| `"auto"`          | `2.0.0`                  | `2.0.0^3-08f2c4d9ab7e`   | `2.0.0^3-08f2c4d9ab7e-d`  |
| `"auto:1.4.2"`    | `1.4.2^40-08f2c4d9ab7e`  | `1.4.2^43-08f2c4d9ab7e`  | `1.4.2^43-08f2c4d9ab7e-d` |
| `"1.4.2"`         | `1.4.2`                  | `1.4.2`                  | `1.4.2`                   |

The disk declared above renders `1.4.2^43-08f2c4d9ab7` instead: `demo_{version}_verity_sig` has to stay
inside 36 characters, which leaves 20 for the version and one fewer hex digit for the hash.

A declared base counts in `version-height`, the number of commits behind HEAD, where a derived one
counts its distance from the tag it came from: the next tag to be cut resets that distance, which would
order the build after it below the one before it, and no tag names the declared base to be counted from
anyway. For the same reason a declared base is never spelled bare, not even standing on a tag: only the
version a tag itself names can be, since two builds of a declared base have to stay distinguishable.
`":"` is not a character a version accepts, so neither spelling can collide with one meant literally.

Every image macro resolves it as it declares the target, so the components are read where the image is
declared and only the packages that declare one re-read them when the version moves. `image.disk`
renders the hash against its own longest label and hands the result to the initrd it declares, so a disk
and its initrd carry one version rather than each rendering its own; an image whose version reaches no
partition label (`image.archive`, the extensions, `image.portable`, `image.initrd`) has all 36
characters to itself and keeps the full 12.

Three things are errors rather than fallbacks, because an unversioned or stale build published under a
version that promises something else is worse than a build that stops:

- A budget that cannot hold even a 4-character hash. The message names the label, what it leaves, and
  what the image_id took out of it. The dirty marker spends two characters of that budget, so an image
  that builds on a clean checkout can still fail on a dirty one. Take characters back from the image_id,
  or declare a shorter base.
- Either `"auto"` spelling with no components in configuration. `bin/tine` writes the reason it had none
  into that block (see below), and the message points there.
- A sentinel that reaches the build unresolved: `image.layer()` and `image.uki()` render
  nothing, and a `select()` cannot be read where the rendering happens, so `"auto"` reaching either would
  otherwise name partitions and files `auto`.

`bin/tine` writes components only for a checkout git can answer for, and records why it could not
otherwise, since a version derived from a checkout that cannot say is worse than none:

```ini
# @generated by `tine`; rewritten on every command.
# no version components: shallow git checkout
# @end generated by `tine`; what follows is yours.
```

A shallow checkout is the one worth knowing about, because CI makes them by default: `git describe` and
`git rev-list --count` answer from truncated history as confidently as from whole history, and every
answer is wrong. The others are a checkout that is not a repository, one without commits, a tag that is
not a version, and a host with no `git` to ask. One caveat has no diagnostic: the base comes from the
*nearest* reachable `v[0-9]*` tag, so merging a branch tagged below the current release moves the base
back with it.

`image.DEFAULT_ROOT_PARTITIONS` is one of the layouts whose labels carry no version: they only serve
sysupdate's A/B slot matching, and a single writable root has no slots and mutates in place, so its
labels stay systemd-repart's type defaults and the version only lands in os-release and the UKI name.

An image that declares a version outright keeps it, the two `"auto"` spellings being the only ones
resolved from configuration. A whole version computed elsewhere is declared the same way, since the
attribute is an ordinary string and anything that answers before the graph is evaluated can produce it.

A version that changes rebuilds only the artifacts that embed it, never package installation.

## Secure Boot signing

`image.disk()` accepts a `secure_boot_key`, which names a target providing
`image.SigningKeyInfo` (the private key and its certificate). It signs the UKIs and the systemd-boot
binaries with `systemd-sbsign`, and `bootctl` places `loader/keys/auto/{PK,KEK,db}.auth` enrollment variables
on the ESP: firmware in setup mode
enrolls the certificate on first boot and then enforces Secure Boot. That key covers PE signing and
enrollment.

`sign_expected_pcr_key` seals the expected-PCR policy the UKIs carry (see the `image.uki` rule above). It
requires Secure Boot signing, which signs the UKI whose measurements it seals. Each role names its own key,
so sealing a policy with the Secure Boot key is something a caller spells out rather than gets by default.
The
two authorize different things: one says which boot binaries firmware may load, the other which measured
boot states may unseal TPM secrets. Keeping them apart bounds a compromise of either one, and means only the
policy key has to be reachable to re-seal a policy. For a key in the build graph its certificate goes
unused, because ukify derives the `.pcrpkey` section from the private key; a key behind a provider needs
one, as [signing-pkcs11.md](signing-pkcs11.md) describes.

The systemd-boot binary is signed inside the image tree, as a `.signed` sibling under
`/usr/lib/systemd/boot/efi`, sealed under the verity root hash where the booted system's `bootctl update`
finds it after an OS update; [architecture.md](../design/architecture.md) explains why it must live there.

Development images can source a key in two ways:

- `image.generate_signing_key()` mints a pair at build time with `ukify genkey`, into buck-out; see
  `//examples/image-secureboot`. Nothing is committed and no manual step is needed. Rebuilds reuse the pair,
  also after a change to the box, and the build never uploads it to a cache. Every workspace, and
  every build after `buck clean`, mints a different key, so all signed artifacts rebuild and each
  workspace's images enroll a different certificate.
- `image.pem_signing_key()` adopts PEM files committed in the consuming project. Stable inputs keep the whole
  signed image graph cacheable, and every build enrolls the same certificate. Such a key is public to
  everyone with repository access: use it for test images only, and never enroll it on real hardware.

A production build should sign with `image.pkcs11_signing_key()`, where the key is held by a PKCS#11 token
and the build reaches it over a socket without ever seeing the key material. See
[signing-pkcs11.md](signing-pkcs11.md).

## Running the image in a VM

Runtime and execution policy live on the `image.vm` target, not in the disk provider: the tools box of the
image's release supplies the VM stack, its `autologin` option provisions a locked root password and runtime
`login.noauth`, and arbitrary non-secret system credentials configure settings such as first-boot locale and
timezone. `cpus` sets the number of virtual CPUs, and `ram` sets the guest memory to a systemd size such as
`"4G"`; both are passed directly to vmspawn.

`cmdline_extra` appends kernel command line arguments to the ones the image already boots with, for
settings a run needs and the image should not carry: `["systemd.firstboot=headless"]` keeps a first boot
from stopping at an interactive enrollment prompt, and `["systemd.log_level=debug"]` makes one boot
verbose without rebuilding the UKI. They are handed to `systemd-vmspawn` as its own trailing arguments,
which is where it takes extra kernel command line arguments, so it picks how to deliver them: a booted
image takes its command line from its own UKI, and vmspawn writes them as the SMBIOS OEM strings the stub
and the boot loader read (`io.systemd.stub.kernel-cmdline-extra` and its `boot` counterpart).

`grow` enlarges the disk file to a given size before boot (vmspawn's `--grow-image`), which is how an
ephemeral guest is given room the built partitions do not occupy; the guest claims that room itself, for
example with `systemd-repart`. vmspawn grows the built artifact in place, and the added space sits past the
GPT backup header until something rewrites the table. Use it to give a guest more room than the shipped
disk carries; a qcow2 that should always carry that room comes from setting `virtual_size` on the image.

```sh
tine buck run //examples/image:boot-demo-vm.fedora
```

The example image uses a tmpfs root with `mount.usr=dissect`; SELinux is disabled because the build does not
yet produce filesystem labels. Ephemeral mode preserves the Buck disk artifact.

## Example targets

Representative builds are:

```sh
tine buck build //examples/image:demo.fedora
tine buck build //examples/image:layered-install.fedora
tine buck build //examples/image:boot-demo.fedora
tine buck build '//examples/image:boot-demo.fedora[uki]'
tine buck build '//examples/image:boot-demo.fedora[partitions][usr]'
tine buck build //examples/image:demo-ext.fedora
tine buck build //examples/image:demo-portable.fedora
```

These validate package installation and commands sharing one delta, incremental layering, archive packing,
versioned UKI creation, semantic boot-artifact extraction, ESP-layer assembly, and disk composition.
`demo-ext` is a system extension and `demo-confext` a configuration extension on top of `boot-demo`.
`demo-portable` is a portable service image, and `demo-portable-ext` an extension for it. Running
`boot-demo-vm` validates the interactive VM runner. `boot-demo-vm-smoke` boots `boot-demo`, merges
`demo-ext` and `demo-confext` at boot, and attaches the two portable DDIs with
`portablectl attach --extension demo-portable-ext demo-portable`.

A host that merges a confext or attaches a portable image needs two things, which `boot-demo` provides:

- systemd-confext mounts `/etc` read-only unless configured otherwise. A host that writes `/etc` at
  boot needs a `confext.conf.d` drop-in with `Mutable=`, or login fails with "PAM failure, aborting"
  once a confext is merged. `boot-demo` uses `Mutable=ephemeral`.
- Only Arch's systemd package contains portablectl. Debian and Fedora install it with
  `systemd-container`.

The signed variant lives in `//examples/image-secureboot`: its `demo-ext` signs the verity root hash with a
generated key, the image ships that key's certificate in `/usr/lib/verity.d/` and a
`/usr/lib/systemd/sysext.conf` whose `ImagePolicy` grants `signed` only, and its `vm-smoke` proves both
directions — the signed extension merges, and the unsigned `demo-ext-unsigned` DDI is refused. Without such
a policy the signature is decorative: systemd-sysext's default policy merges unsigned images, and falls back
to plain verity when a signature fails to validate.

The same targets build over Arch Linux through their `.arch` aliases, which is the second native package
system end to end:

```sh
tine buck build //examples/image:demo.arch
tine buck build //examples/image:layered-install.arch
tine buck build //examples/image:boot-demo.arch
tine buck run //examples/image:boot-demo-vm-smoke.arch
```

and `.debian` in place of `.arch` builds the same declarations over Debian. Nothing in them names
pacman or dpkg, and nothing is written twice.

### Choosing a distribution

An image's distribution is a configuration its target carries, so one declaration serves every
distribution the catalog offers. An image rule names itself once per distribution its package
serves, so declaring `boot-demo` also declares `boot-demo.fedora`, `boot-demo.arch` and
`boot-demo.debian` with nothing further to write. For any other rule, such as a prelude `command_alias`,
you declare the aliases yourself:

```Starlark
command_alias(
    name = "boot-demo-qemu",
    exe = ":run-qemu",
    args = ["$(location :boot-demo)"],
    target_compatible_with = distribution.compatibility(),
)

distribution.alias(
    name = "boot-demo-qemu.fedora",
    actual = ":boot-demo-qemu",
    distro = "catalog//:<family>.<release>.distribution",
)
```

If the target has constraints of its own, also pass them to the alias as `compatible_with`. Otherwise
building `//...` for an architecture the target doesn't support fails instead of skipping the alias. An
image rule passes its `target_compatible_with` to its aliases itself.

An image can also name its own distribution instead of being aliased into one:

```Starlark
image.disk(
    name = "appliance",
    distro = "catalog//:<family>.<release>.distribution",
    package_sets = ["bootable"],
    ...
)
```

Either way the image gets the package manager of that distribution, and the package names come
from the release's package sets, so moving an image between distributions changes neither its operations
nor the rules underneath. The mechanism is described in
[architecture.md](../design/architecture.md#selecting-a-distribution).

The repository sets no default. `//examples/image:boot-demo` is declared for no distribution
in particular, so building it by that name fails as incompatible and `//examples/image/...` skips it;
`:boot-demo.fedora`, `:boot-demo.arch` and `:boot-demo.debian` are what build. A default in the
repository would make whichever distribution it named the only one anybody builds, and the others would
rot. Your own checkout can set a default with the `distribution` key of the `tine` buckconfig section.

Every package gets that behaviour for the releases that the catalog lists in `catalog//:releases.bzl`:

```Starlark
RELEASES = {
    "arch": "arch.rolling",
    "debian": "debian.testing",
    "fedora": "fedora.rawhide",
}
```

Each key is the suffix of the alias that every image gets for that release. The key `fedora` declares
`:boot-demo.fedora` beside `:boot-demo`. Each value is the `<family>.<release>` prefix of a release in the
catalog. A project with its own catalog lists the releases of that catalog in its own `releases.bzl`.

If `releases.bzl` lists one release, images build for that release by their own names and get no aliases.
For a project that builds for Fedora rawhide only, `releases.bzl` lists that one release, and
`:boot-demo` builds a Fedora rawhide image:

```Starlark
RELEASES = {
    "fedora": "fedora.rawhide",
}
```

An image that names neither `package_manager` nor `parent` uses the package manager of the release that a
build chooses:

```Starlark
image.archive(
    name = "demo",
    packages = ["bash"],
    format = "tar",
)
```

`distribution.package_manager()` returns that package manager. To add repositories of your own, pass it as
the `base` of a `package.manager()`, and pass the result as the image's `package_manager`.

A package that builds for other releases, or needs more per release, declares its own table in its
`PACKAGE` file. Each entry's `release` names a release of the catalog. `package_manager` defaults to the
release's package manager. Everything else is whatever the package needs to know per distribution, read
back with `distribution.for_package()`:

```Starlark
load("@tine//distribution:defs.bzl", "distribution")

distribution.set_for_package({
    "fedora": {"release": "fedora.rawhide"},
    "fedora-43": {"release": "fedora.43", "package_manager": "//:fedora-43.package-manager"},
})
```

An empty table, `distribution.set_for_package({})`, declares no distribution. The package's images then
build by their own names, from whichever package manager they name. An image in such a package that names
neither `package_manager` nor `parent` has no package manager and can't install packages.

A key of `distribution.select()` is a family (`arch`, `debian` or `fedora`), a release of the catalog such as
`fedora.43`, or `DEFAULT`. A family key matches every release of that family. Buck picks a release key over
the key of its family, and `DEFAULT` matches where no other key does. Buck fails to configure a target that
matches no key. The names in a package's table name its aliases, such as `:boot-demo.fedora-43`, and no
select reads them. A release key names the distribution in the root package of the catalog cell, and tine
fails to load a distribution declared anywhere else.

To build an image for some of the package's distributions only, make it incompatible with the others. The
aliases for the others remain, and a build of the package skips them:

```Starlark
image.disk(
    name = "fedora-only",
    # ...
    target_compatible_with = distribution.select({
        "DEFAULT": select_incompatible("fedora-only builds for Fedora only"),
        "fedora": [],
    }),
)
```

Every image rule then defaults its `target_compatible_with` to them, so no target repeats it and none
can forget it: a target that is itself unconstrained while its image is incompatible is an error
rather than a skip, which is exactly the mistake the per-package declaration prevents. A rule tine
does not own, such as a prelude `command_alias` over an image, has no macro to inherit through and
asks with `distribution.compatibility()`.

Building one of those targets without choosing says so by name:

```text
tine//examples/image:boot-demo is incompatible with tine//platforms:default
    (tine//distribution:no-distribution-chosen unsatisfied)
```

If you build one distribution all day, set the `distribution` key of the `tine` section to the name of
its alias. Put it in `.buckconfig.local`, below the block that `bin/tine` generates there. git ignores
that file, so the setting applies to your checkout only:

```ini
[tine]
distribution = arch
```

`//examples/image:boot-demo` then builds what `:boot-demo.arch` builds, and the build shares its outputs
with the alias. The aliases still build their own distributions. A package whose table has no entry of
that name still fails to build a target by its own name. To set the key for one build, pass
`-c tine.distribution=arch`.

With the key set, `buck test` runs a test of that distribution by its own name only. The test's alias
for that distribution still builds, but `buck test` of the alias runs no test.

A consuming project adds `//packages/fedora/rawhide:zlib-ng`, which validates package import,
package-manager selection, buildroot assembly and RPM collection (packages with `buildroot_packages`
additionally exercise local-package preference), and `//examples/image-local-packages:image`, which builds
an image from those packages. Neither target exists in this repository.

## Updating pinned tools

`bump` refreshes the pinned tool releases against their upstream GitHub releases. A pin with a `url`
moves to the newest release in the index that its `releases` names.

Update one or more pinned tools by name (`buck2`, `starlark-fmt`, `python3`, `ruff`, `ty`, `syft`,
`cargo-auditable`, `seaweedfs`, `go`, `gh`):

```sh
tine bump --tool ruff --tool ty
```

Update all tools:

```sh
tine bump --all
```

Each tool is resolved to its latest upstream release, and its `release`, `artifact`, `sha256` and `size`
are rewritten in place. For a pin with a `url`, `bump` downloads each new artifact to compute its digest and
size. The pattern of the `go` pin matches only Go 1.27 releases. Change the pattern to move the pin to
another minor version. Add `--commit` to record the result as a git commit whose message itemizes each
update.

Buck2 is declared like the rest, and `starlark-fmt` ships from the same fork release, so one run keeps
the two on one tag. `bin/tine` fetches Buck2 itself, Buck2 then fetches the rest.

`bump` keeps `python3` on the minor version that `python-version` in `ty.toml` sets. Moving to a new minor
version is a deliberate change to `ty.toml`.
