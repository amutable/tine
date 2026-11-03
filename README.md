<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# tine

tine composes operating-system images out of packages and pinned third-party repositories with
[Buck2](https://buck2.build/). Every build action runs in an unprivileged sandbox without network access.
Filesystem images are stacks of overlay deltas. tine's primary output targets are [unified kernel
images](https://uapi-group.org/specifications/specs/unified_kernel_image/) and dm-verity protected GPT
disks in various formats, and also additional artifacts like
[SBOM](https://en.wikipedia.org/wiki/Software_supply_chain).

## Requirements

The host needs only:

 * Linux ≥ 6.15 with unprivileged user namespaces. On Ubuntu you need to allow them with a
 [sysctl](https://www.freedesktop.org/software/systemd/man/latest/sysctl.d.html):
   `kernel.apparmor_restrict_unprivileged_userns=0`.
 * A host root that supports `pivot_root()`; some kernels cannot pivot an initramfs root
 * glibc ≥ 2.17, which the pinned Python that starts every sandbox links against
 * `/usr/bin/python3` ≥ 3.14, or ≥ 3.12 together with `zstd`
 * `git`
 * `/bin/sh`, which runs the fetch of a Rust project's git dependencies
 * `/dev/kvm` to run VMs for testing; without it, you can still build everything

tine uses the host's `python` and `git` to bootstrap itself and to read the project's git state, such as the
version an image derives from it. A build also runs the host's `git` to fetch git checkouts, and the host's
`git` and `/bin/sh` to fetch the git dependencies of Rust projects. Every other tool a build runs is pinned.
The first [`tine`](bin/tine) invocation that runs Buck fetches and verifies the pinned Buck2 binary into
`${XDG_CACHE_HOME:-~/.cache}/tine/buck2` (one directory per pin, safe to delete); everything else is fetched
and cached by Buck itself.

### HOME directory

Buck2 refuses to start without `$HOME` (some CI environments don't set it), as it runs its daemon from
there. In that case, the `bin/tine` command sets `$HOME` to a gitignored `.buck/` in the project root
before running anything: the Buck2 download from above then lands there, too.

Kill the daemon if you change the home directory between runs.

## Quick start

The commands below spell the entry point `tine`; it is [`bin/tine`](bin/tine) in a checkout, so run it
from there, or put it on `PATH`.

Build a minimal tar image with a few packages and custom files:

```sh
tine buck build //examples/image:demo.fedora
```

Build a bootable GPT disk with a unified kernel image and dm-verity protected `/usr`:

```sh
tine buck build //examples/image:boot-demo.fedora
```

Boot it in an ephemeral VM:

```sh
tine buck run //examples/image:boot-demo-vm.fedora
```

Images are declared in ordinary `BUCK` files ([examples/image/BUCK](examples/image/BUCK) has the complete
demos):

```Starlark
load("@tine//image:defs.bzl", "image")

image.archive(
    name = "demo",
    packages = ["bash", "coreutils"],
    ops = [
        image.ops.copy("marker.txt", "/etc/tine/marker"),
        image.ops.run(["/usr/bin/bash", "-c", "echo built-by-tine >> /etc/tine/marker"], chroot = True),
    ],
    format = "tar",
)
```

## Using tine from your project

### Initialization

Check out this repository inside the project, as a pinned git clone, worktree, or submodule. Register
that directory as the `tine` cell with `init`, then build:

```sh
git submodule add https://github.com/amutable-systems/tine tine
tine/bin/tine init          # write tine.toml, .buckconfig, and .gitignore
tine/bin/tine buck build //packages/...
```

`init` records the cell in `tine.toml` and generates the project's `.buckconfig` from the checkout's
defaults. Each ordinary `tine buck` command regenerates that file from the selected checkout, including
a mounted override. Put persistent project settings in `[buckconfig.*]` tables in `tine.toml`; direct
edits to the generated `.buckconfig` are overwritten.

### Configuration

Keep `tine.toml` and the generated `.buckconfig` committed in the consuming project. The latter is a
bootstrap file: tine needs it to locate the project before it can refresh the defaults. Ignoring it
would leave a fresh clone unable to run `tine buck` without recreating that file first.

Use `tine.local.toml` or `.buckconfig.local` for machine-local overrides, and keep both untracked. tine
updates only its generated block in `.buckconfig.local`, preserving text outside that block.

Image versions can be derived from the project's Git history; see
[images.md](docs/user/images.md#image-versioning).

### Entry point

The entry point is [`bin/tine`](bin/tine). For Buck commands and completion, an existing `tine` on `PATH`
automatically hands over to the configured tine cell's `bin/tine`, even without a mount. There is no need
to change `PATH` when selecting another checkout. Move to a newer tine with `git submodule update` or the
equivalent change in your pinned `git clone`.

Buck reads the checkout in place, so a branch or an uncommitted edit is active immediately.

`tine` has several top-level verbs; see `tine --help`:

```sh
tine buck <arguments>    # run the pinned Buck2, passing <arguments> verbatim
tine box                 # run the project's root box target
tine mount <verb>        # manage external directories mounted over project paths
tine cache-status        # report on the shim serving the shared build cache
tine init [<path>]       # write the configuration a project needs, for the checkout this command is in
tine bump <arguments>    # update the pins in the project's tine.lock.json
tine completion <shell>  # print the completion script for bash, fish or zsh
```

Use `tine buck` instead of running Buck2 directly so that builds use your selected checkouts.

## Shell completion

Install shell completion from inside a project:

```sh
tine completion fish > ~/.config/fish/completions/tine.fish
tine completion bash > ~/.local/share/bash-completion/completions/tine
tine completion zsh > ~/.local/share/zsh/site-functions/_tine   # any directory on $fpath
```

Target completion neither downloads Buck2 nor refreshes shared configuration. Run an ordinary
`tine buck` command first to fetch the binary if target completion has no results.

### Fast local development

To test a development checkout of tine in a consuming project, mount it over the one the project
registered:

```sh
tine mount add tine ~/Projects/tine    # use this checkout as tine/
tine mount list                        # list targets and their active sources
tine mount remove tine                 # use the project's checkout again
```

Mounts work the same for other parts of an image that come from a git repository, such as Go or Rust
projects; see [fetching project sources](docs/user/git.md). `tine mount list` shows the active local
source, or `default` when a target is not overridden. `add` creates a missing directory for a declared
checkout slot. Builds use the selected checkout, including uncommitted edits; selecting a different tine
checkout also selects its command, rules, and pinned Buck2.

Mount changes take effect on the next `tine buck`. Switching mounts can interrupt builds already using a
different checkout. Mounting requires unprivileged user namespaces, which some distributions disable.

A consuming OS monorepo ("OS.git" in these docs) additionally holds package sources under
`packages/<distro>/<branch>/<package>`, imported and updated from upstream dist-gits (Fedora, or CentOS
Stream) by the [importer](docs/user/importer.md). Those build as ordinary Buck targets and feed images,
so an image build rebuilds exactly the affected packages.

## Pinned dependencies

[`tine.lock.json`](tine.lock.json) declares the supported Buck2 and every other tool tine uses
internally. `tine bump` updates those pins. tine depends on fixes in its Buck2 fork, so running
another Buck2 is unsupported.

## Developing tine

```sh
tine buck run tine//tools:check           # lint, type-check, unit tests
tine buck run tine//tools:fmt             # auto-format and auto-fix
tine buck run tine//tools:verify-catalog  # assert the committed catalog lock matches
tine buck build tine//... catalog//...    # build everything: the catalog, examples, tooling
tools/ci.sh                               # the whole CI pipeline: checks, builds, image tests
```

The full command list is in [AGENTS.md](AGENTS.md).

## Documentation

User guides:

- [Building images](docs/user/images.md): host requirements, declaring images and layers, output formats,
  the VM runner
- [Publishing images to AWS EC2](docs/user/aws.md): importing a disk as an AMI, and booting a test
  instance
- [Publishing quarry releases](docs/user/quarry.md): a release folder with the
  [TUF](https://theupdateframework.io/) targets.
  [quarry](https://github.com/amutable/quarry) updates machines from that.
- [Building Rust projects](docs/user/cargo.md): building a checked-out Rust project offline, with its
  crate graph in the executables and image SBOM
- [Building Go projects](docs/user/go.md): the same for a checked-out Go project, whose module list go
  itself embeds in the executables
- [Fetching project sources](docs/user/git.md): pinning an external project, or use a local checkout
  for development
- [Maintaining packages](docs/user/importer.md): importing and updating packages from upstream
  distributions, local modifications, branch curation
- [Development boxes](docs/user/box.md): pinned interactive development environments
- [Pinning prebuilt tools](docs/user/http.md): downloading a tool release pinned in `tine.lock.json`
- [Signing with external keys](docs/user/signing-pkcs11.md): Secure Boot, PCR policy and verity signing
  over PKCS#11, with the keys outside the build
- [Shared build cache](docs/user/remote-cache.md): configuring it, the keys, the bucket, and reading the
  shim's counters

Design:

- [Architecture](docs/design/architecture.md): component model, decision record, current limitations,
  roadmap
- [Package import machinery](docs/design/packages.md): branch layout, metadata, consistency checks,
  rebuild strategy
- [Self-hosting approaches](docs/design/self-host-approaches.md): future design for the BuildRequires
  cycle
- [Shared build cache design](docs/design/remote-cache.md): threat model, bucket layout, signatures and
  keys

## License

tine is distributed under the [Mozilla Public License 2.0](./COPYING) (SPDX `MPL-2.0`).
