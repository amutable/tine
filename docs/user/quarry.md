<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# Publishing quarry releases

[quarry](https://github.com/amutable/quarry) delivers updates over
[TUF](https://theupdateframework.io/): machines run systemd-sysupdate against a local quarry proxy,
which serves only files that the repository's signed targets list. `quarry.release` turns an
[`image.artifacts`](images.md#imagedisk) release folder into one that carries those targets. Whoever
publishes the release imports them into its repository and signs them with its own keys, so the
build holds no signing key.

`examples/image-secureboot` publishes its `release` this way.

## Declaring a release

```Starlark
load("@tine//quarry:defs.bzl", "quarry")

quarry.release(
    name = "release",
    targets = [":image", ":image[qcow2]", ":image[sbom]", ":demo-ext"],
    transfers = {"sysupdate.d": "sysupdate.d"},
)
```

- `targets` are what `image.artifacts` gathers into the release folder.
- `transfers` maps a component dir, `sysupdate.d` or `sysupdate.<component>.d`, to the directory of its
  sysupdate definitions. hardhat inlines them in the targets as `.quarry/<component dir>/<file>`,
  without a version. A component's `.component` and `.feature` files decide on which machines it is
  enabled. A transfer's `MatchPattern=` starts with `**/`, because the publisher decides in which
  directory of the repository the files of a release end up.
- `exclude` selects files that the targets leave out. The folder still holds them. A selector names a
  kind of the [release's description](images.md#imagedisk), which picks every subkind of it, or
  `<kind>/<subkind>` with an fnmatch glob as the subkind: `partition/usr*` picks the usr partition and
  its verity partitions, `extension` every extension. It defaults to `["disk/raw"]`, as the sparse raw
  disk is not meant to be published to HTTP (only a disk's `[qcow2]` and `[raw.zst]` subtargets are).
- `hardhat` is the hardhat binary buck target that writes the TUF targets. Defaults to
  `tine//quarry:hardhat` from the quarry commit that `tine.lock.json` pins. A project that also
  builds quarry for its images can pass its own, so that one quarry version writes and reads its
  releases.

The rule adds `targets_<arch>.json` to the folder, next to `artifacts_<arch>.json`. hardhat links to
libpathrs, which Debian does not package, so a release is incompatible with Debian. A target that
depends on a release needs to be declared `target_compatible_with = quarry.compatible_with()`.

## The marks

Every target carries quarry's marks under `custom.quarry`. They come from the release's
description:

| Mark | On |
| --- | --- |
| `version` | every file: the version of the release's images, which must all have the same one |
| `update` | what systemd-sysupdate installs: UKIs, usr partitions and their verity partitions, extensions of any class, portable images |
| `supplemented-by` | what a client fetches alongside: a UKI brings its usr partition and the initrd's SBOMs and package database, a usr partition its verity partitions and the image's SBOMs and package database, an extension or a portable image its own SBOMs and package database. Each names its media type where it has one |
| `verity-root-hash` | the usr partition, extensions and portable images |

Only `buck build` of a release builds hardhat and the boxes that build and run it. A project without a
release never builds them.
