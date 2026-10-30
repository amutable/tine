<!--
SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
SPDX-License-Identifier: MPL-2.0
-->

# AGENTS.md

## General

- **Never touch `buck-out/` directly**: no `rm`, `mv`, or edits. To inspect an output, get its path from
  `buck build --show-output`/`--out` and read it read-only.
- **Never call `buck2` directly**: `tine buck` is the only entry point; every `buck` here and in the docs
  is shorthand for that.
- **Never `buck build | tail`**: redirect the full output to a log file, print the path so the user can
  follow along, and check the exit status separately. `tine buck log what-ran --failed --show-std-err`
  prints the full stderr of the actions that failed in the last build, which the build output truncates.
- Keep `buck run tine//tools:check` green.
- Start every new file with the SPDX header, in the file's comment syntax, after a shebang if there is
  one: `SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>` and `SPDX-License-Identifier:
  MPL-2.0`. A file that cannot carry one, such as data, is listed in `REUSE.toml` instead; `check` runs
  `reuse` and fails on a file with neither.
- Line break documents and plans at 109 columns.
- Comments explain *why*, not *what*; say each thing once.

## Layout

Cell layout and architecture: [the design plan](docs/design/architecture.md). User guides are under
`docs/user/`, design documents under `docs/design/`.

- A unit test is `<driver>_test.py` beside its driver, run by a `box_python_test` in the same package.
- An assertion about what a build produced is a `box_sh_test` beside the target that produced it, taking
  artifacts as `$(location)` arguments so it runs against pinned tools rather than the host's.
- Python sources reach a suite in another package through `deps` on a `tine_python_library`, never
  `export_file`.
- Python drivers use `tine_python_library`/`tine_python_binary`. Never declare the prelude's bootstrap
  target or a `-ty` check by hand; the macros do both.
- A driver importing third-party modules lists the `boxes` its check resolves against, one check per box.
  `typecheck = False` is for vendored sources only.

## Commands

`mise.toml` puts `./bin` and `./tools` on `PATH`; every pinned tool is declared in `tine.lock.json`.
`tine buck` runs Buck with the mounts declared by `tine mount` in `.buck/tine-mounts.toml`.

- **Build everything:** `buck build tine//... catalog//...`, which runs no tests. The catalog is a cell of
  its own, so `tine//...` leaves it out.
- **Lint (format, lint, type-check, whole-graph analysis):** `buck run tine//tools:lint`
- **Check (lint plus the fast test suites):** `buck run tine//tools:check`
- **Auto-format + auto-fix:** `buck run tine//tools:fmt`
- **Analyze part of the graph without building it:** `buck bxl tine//tools/graph.bxl:analyze -- --pattern
  <pattern>`; `lint` already runs it over everything.
- **Tests:** `buck test tine//...`, or one suite: `buck test tine//image:test`. Anything needing an example
  image carries the `image` label, the boot smokes additionally `vm`; `--exclude image` is the
  seconds-long half that `check` runs, `--include image` the rest, which CI runs as its own step.
- **Catalog lock:** `buck run tine//tools:refresh-catalog`; `tools:verify-catalog` asserts the committed
  lock matches.
- **Example image digests:** `examples/image:reproducibility-test` asserts the digests committed in
  `examples/image/expected.json`, and on a failure writes `actual.<arch>.json` beside it; `buck run
  tine//examples/image:refresh-expected` accepts them (usually with `--amend`). Anything that can alter
  what the example images build needs this: a package they install, an image or UKI rule, a driver that
  writes into a root. The test carries the `image` label, so `check` does not run it and only CI's
  image step catches a stale file.
- **Full CI pipeline:** `tools/ci.sh`, fail-fast. Run individual groups by stating their names, e.g.
  `tools/ci.sh check build`. `TINE_BOX=arch.rolling tools/ci.sh` runs it with the boxes of another
  release of the catalog, as one of CI's jobs does.
- **rpm importer:** `buck run tine//tools:importer -- <verb>`; see [importer.md](docs/user/importer.md).
- **BuildRequires cycle analysis:** `buck run tine//tools:scc -- <branch label>`; background in
  [self-host-approaches.md](docs/design/self-host-approaches.md).

## Starlark

`starlark_fmt` owns the layout of every `.bzl` and `BUCK` file and wraps at 160 columns.

- Leave a trailing comma after the last argument to keep a signature or call broken across lines.
- It sorts dict keys unconditionally; mark a dict whose order is a contract (`ddi.bzl:partition` feeds
  partition UUIDs) `# @unsorted-dict-items`.
- It drops load symbols it cannot see used; annotate type-only imports `# @unused`.

## Macros and rules

A macro forwards arguments it does not read as `**kwargs`; one it *reads* is a typed parameter, never a
`kwargs` lookup, and its helpers take those values as parameters rather than the whole dict. Such a
parameter defaults to `None`, which Buck reads as the attribute's own default, and is annotated
`X | Select` if a caller may reach it with a `select()`.

`ctx.attrs` belongs to the rule implementation that owns those attributes. `_impl` reads whatever it likes
from it; every other function takes what it needs as typed parameters.

## Workspace

- Use cell-relative `tine//...` labels for same-cell targets, never `root//...`.
- Never add a `.buckroot` here. Buck takes the *furthest* ancestor `.buckconfig`, and `.buckroot` would
  stop that search, keeping this checkout out of a workspace it supplies the `tine` cell to.
- The flip side: a stale `.buckconfig` in a parent directory captures the project, and `//...` then matches
  nothing while still exiting 0. If a command mysteriously finds nothing, check
  `tine buck root --kind project` before anything else.

## Python

- Use PEP 257 docstring style: Short single-line summary, blank line, multi-line body.
- Don't use `TypeVar`; use the 3.12+ generics syntax. Never `from __future__ import annotations`. Use
  `Self` for a class's own type, not a string type.
- Use context managers (`ExitStack`/`AsyncExitStack` where needed) for anything that needs cleanup.
- Use `pathlib.Path` and prefer its methods over `os.*`. The exceptions are `box/sandbox.py` and
  `box/isolation.py`: use string paths and `os.path` there to keep startup imports to the absolute minimum.
- Never import `.bzl` files into Python tests, including through `runpy`.

### Paths in action scripts

Buck hands drivers project-relative paths and runs them at the project root, which a build sandbox mounts
at `/tine/project`. Keep them relative; making one absolute is a decision that needs a reason.

- **`.absolute()`** only when something downstream changes the frame of reference: a chroot, a subprocess
  given its own `cwd=`, a path written into a spec another process consumes, or a library documented as
  needing it (libdnf5). Say which in a comment.
- **`.resolve()`** follows symlinks on top of that, so reach for it only when following them is the
  point: `source_overlay()` in `rootfs.py` resolves the source tree to compare it with the mount points
  that statmount() returns, which have their symlinks resolved.

## Commits

- Commits are signed off, but the trailer is appended automatically: never add it by hand.
- Never mention an AI or agent in a commit message or PR body, and never add a `Co-Authored-By: Claude`
  or "Generated with Claude Code" trailer.
- A subject under 60 characters, `area: what changed`, imperative. Then at most one short paragraph
  wrapped at 80 columns: what was wrong or missing, then what this does about it, and nothing else.
- Write it like you would say it to a colleague at their desk. Short words, plain verbs, no bullet lists.
- Never restate the diff: the reader has it, what they lack is the reason. Cut every sentence that is not
  the problem or the fix.

A whole commit body, in full:

```
The daemon kept a stale mount digest, so a second build reused a namespace
that no longer matched. Compare the digest on startup and kill the daemon
when it differs.
```
