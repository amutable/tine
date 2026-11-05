# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Run pinned source checks and formatters against the active tine cell."""

import argparse
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from util import buck2_argv, buck_output, fail, write_if_changed


def _bold(label: str) -> None:
    print(f"\033[1m{label}\033[0m", flush=True)


def _run(cmd: Sequence[str | Path], *, stderr: int | None = None) -> None:
    # Tools print their own diagnostics; propagate failures without a traceback.
    proc = subprocess.run(cmd, stderr=stderr)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _cell_root(cell: str) -> Path:
    return Path(buck_output("audit", "cell", cell, "--paths-only"))


def _starlark_srcs() -> list[Path]:
    # Check every loaded in-tree Starlark file; ignore dead files, external cells, and JSON.
    cells = cast(dict[str, str], json.loads(buck_output("audit", "cell", "--json")))
    aliases = cast(dict[str, str], json.loads(buck_output("audit", "cell", "--json", "--aliases")))
    roots = {path: name for name, path in sorted(cells.items()) if name not in ("none", "prelude")}
    universe = " + ".join(sorted(f"{name}//..." for name in roots.values()))
    project = Path(buck_output("root", "--kind", "project"))
    files = sorted(
        project / f for f in buck_output("uquery", f"allbuildfiles({universe})").splitlines() if f
    )
    # Keep the tine cell's own files, plus those of any cell nested inside it. Reject by owning cell
    # rather than by path prefix, because nested `none`/`prelude` are not on disk, so a prefix test
    # would hand the formatter paths that do not exist.
    cell_roots = sorted(
        ((Path(path), name) for name, path in cells.items()),
        key=lambda item: len(item[0].parts),
        reverse=True,
    )
    tine = Path(aliases["tine"])

    def owner(path: Path) -> str | None:
        return next((name for root, name in cell_roots if path.is_relative_to(root)), None)

    return [
        f
        for f in files
        # Data loads are parse inputs too, but not Starlark, and the format override means their
        # names promise nothing, so keep what is known to be Starlark rather than reject data.
        if f.is_relative_to(tine) and owner(f) not in (None, "none", "prelude")
        if f.suffix == ".bzl" or f.name in ("BUCK", "PACKAGE")
    ] + sorted(
        # A .bxl is Starlark nothing loads, so no build file names it; check it all the same.
        f
        for f in tine.rglob("*.bxl")
        if "buck-out" not in f.parts
    )


def _orphan_tests(cell: Path) -> list[Path]:
    """Find test files omitted from Python test suites."""
    targets = json.loads(
        buck_output("uquery", "kind('python_test', tine//...)", "--output-attribute", "srcs")
    )
    # A label in the root package renders as `cell///file`, which would resolve to an absolute path.
    claimed = {
        cell / src.split("//", 1)[1].lstrip("/") for target in targets.values() for src in target["srcs"]
    }
    return sorted(p for p in cell.rglob("*_test.py") if "buck-out" not in p.parts and p not in claimed)


def _universe() -> str:
    """The query for everything a whole-cell run asks for."""
    return "tine//... + catalog//..."


def _targets() -> list[str]:
    """What `_universe()` matches, to hand to a command that takes no query.

    Named explicitly, a target incompatible by design (an image base without a chosen distribution, a
    signing token without a configured token, an Arch box on an arm64 host) is an error rather than a
    skip, so a command given these needs `--skip-incompatible-targets`.
    """
    return buck_output("uquery", _universe()).split()


def _starlark_fmt(args: argparse.Namespace, *arguments: str | Path) -> list[str | Path]:
    return [args.starlark_fmt, "--config", args.starlark_fmt_config, *arguments]


def _fmt_diff(args: argparse.Namespace, src: Path) -> str:
    """The rewrite starlark_fmt would apply to one file, empty when it is already formatted."""
    proc = subprocess.run(_starlark_fmt(args, "diff", src), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr, end="", file=sys.stderr, flush=True)
        raise SystemExit(proc.returncode)
    return proc.stdout


def _lint(args: argparse.Namespace) -> None:
    cell = _cell_root("tine")
    srcs = _starlark_srcs()
    _bold("test targets")
    if orphans := _orphan_tests(cell):
        listing = "\n".join(f"  {p.relative_to(cell)}" for p in orphans)
        fail(f"no python.test suite lists these, so they never run:\n{listing}")
    _bold("ruff")
    _run([args.ruff, "format", "--check", "--no-cache", cell])
    _run([args.ruff, "check", "--no-cache", cell])
    _bold("reuse")
    # Scoped by --root rather than by the cwd Buck runs this in, so a consuming project checks the
    # cell it declares and not whatever else sits beside it.
    _run([*buck2_argv("-v", "0", "run", "tine//tools:reuse"), "--", "--root", cell, "lint", "--lines"])
    _bold("ty")
    targets = buck_output("uquery", f"attrfilter(labels, 'python-typecheck', {_universe()})").split()
    if not targets:
        fail("ty: no generated type-check targets found")
    _require_manifests(cell, _ty_packages(targets))
    _run(buck2_argv("build", *targets))
    _bold("starlark_fmt")
    # starlark_fmt has no check mode, so diff each file and fail on the first rewrite it would make.
    if diffs := [diff for src in srcs if (diff := _fmt_diff(args, src))]:
        print("".join(diffs), end="")
        raise SystemExit(1)
    # Pass the files rather than the cell directory: standalone, the tine cell is the project root,
    # which Buck normalizes to an empty path and rejects.
    _bold("starlark lint")
    _run([*buck2_argv("-v", "0", "starlark", "lint", "--console", "none"), *srcs])
    _bold("starlark typecheck")
    # Typecheck errors use stdout; stderr is only the per-file event log. Unlike lint, typecheck
    # follows load() into data files and parses them as Starlark, which no TOML survives (a JSON
    # object happens to be a valid Starlark expression), so files with a TOML load stay out; the
    # probe also matches the `?format=toml` spelling.
    checkable = [f for f in srcs if 'toml"' not in f.read_text(encoding="utf-8")]
    _run([*buck2_argv("-v", "0", "starlark", "typecheck"), *checkable], stderr=subprocess.DEVNULL)
    _bold("target graph")
    # Analysis, not a build: it reaches every rule a build would run, without producing anything.
    # Scoped to this cell, whose platform the parser knows how to detect; what a consuming project
    # declares is its own to check, with its own pattern.
    _run(
        buck2_argv(
            "-v",
            "0",
            "bxl",
            "--console",
            "none",
            "tine//tools/graph.bxl:analyze",
            "--",
            "--pattern",
            _universe(),
        )
    )


def _check(args: argparse.Namespace) -> None:
    _lint(args)
    _bold("unit tests")
    # Building an example image is minutes where these are seconds. Everything that needs one is
    # labelled `image` and covered by `buck test tine//... --include image`, which is what CI runs.
    _run(buck2_argv("test", "--skip-incompatible-targets", *_targets(), "--exclude", "image"))


def _fmt(args: argparse.Namespace) -> None:
    cell = _cell_root("tine")
    srcs = _starlark_srcs()
    _bold("ruff")
    _run([args.ruff, "format", "--no-cache", cell])
    _run([args.ruff, "check", "--fix", "--no-cache", cell])
    _bold("starlark_fmt")
    _run(_starlark_fmt(args, "fmt", *srcs))


def _write_dot(path: Path, intra: dict[str, list[str]], rev: dict[str, list[str]]) -> None:
    """The cycle subgraph as graphviz; node labels carry out/in degree within the cycle."""
    lines = ["digraph scc {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    lines += [f'  "{n}" [label="{n}\\n->{len(intra[n])} <-{len(rev[n])}"];' for n in sorted(intra)]
    lines += [f'  "{n}" -> "{p}";' for n in sorted(intra) for p in intra[n]]
    path.write_text("\n".join(lines) + "\n}\n")


def _scc(args: argparse.Namespace) -> None:
    # Graph derivation and cycle detection live in buck (rpm_branch); this only formats its output.
    graph = buck_output("build", f"{args.branch}:_buildrequires_graph", "--out", "-")
    data = cast(dict[str, Any], json.loads(graph))
    edge_caps = cast(dict[str, dict[str, list[str]]], data["edges"])
    components: dict[int, set[str]] = {}
    for name, cid in data["sccs"].items():
        components.setdefault(cid, set()).add(name)
    cycles = sorted((c for c in components.values() if len(c) > 1), key=len, reverse=True)

    print(f"{len(edge_caps)} packages; cycle sizes: {[len(c) for c in cycles] or 'none, all acyclic'}")
    acyclic = sorted(n for c in components.values() if len(c) == 1 for n in c)
    print(f"outside any cycle: {', '.join(acyclic)}\n")
    if not cycles:
        return

    incycle = {n: c for c in cycles for n in c}  # cycle member -> its cycle
    intra = {n: sorted(p for p in edge_caps[n] if incycle.get(p) is incycle[n]) for n in incycle}
    rev: dict[str, list[str]] = {n: [] for n in incycle}
    for n in sorted(intra):
        for p in intra[n]:
            rev[p].append(n)

    if args.why:
        assert args.why in incycle, f"{args.why} is not a member of any cycle"
        _bold(f"{args.why}: cycle edges and their reasons")
        for p in intra[args.why]:
            print(f"  {args.why} -> {p}: {', '.join(edge_caps[args.why][p])}")
        for n in rev[args.why]:
            print(f"  {n} -> {args.why}: {', '.join(edge_caps[n][args.why])}")
        return

    for cycle in cycles:
        _bold(f"the {len(cycle)}-member cycle, ascending peer-provider count")
        print(f"{'package':24} {'out':>3} {'in':>3}  cycle-internal BR providers")
        for n in sorted(cycle, key=lambda n: (len(intra[n]), n)):
            print(f"{n:24} {len(intra[n]):3} {len(rev[n]):3}  {', '.join(intra[n])}")
        print()

    if args.dot:
        _write_dot(args.dot, intra, rev)
        print(f"\nwrote {args.dot}")


def _toml_value(value: object) -> str:
    """Encode the tables, arrays and scalars used by ty's configuration."""
    if isinstance(value, dict):
        table = cast(dict[str, object], value)
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(v)}" for k, v in table.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in cast(list[object], value)) + "]"
    return json.dumps(value, ensure_ascii=False)


def _merge_ty_config(base: dict[str, Any], package: dict[str, Any]) -> None:
    """Merge like ty's user and project settings: override scalars and concatenate arrays."""
    for key, value in package.items():
        previous = base.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            _merge_ty_config(previous, value)
        elif isinstance(previous, list) and isinstance(value, list):
            base[key] = previous + value
        else:
            base[key] = value


def _read_ty_config(base: Path, pyproject: Path | None = None) -> dict[str, Any]:
    config = tomllib.loads(base.read_text(encoding="utf-8"))
    if pyproject is not None:
        package = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        _merge_ty_config(config, package.get("tool", {}).get("ty", {}))
    return config


def _write_ty_config(path: Path, config: dict[str, Any]) -> None:
    write_if_changed(
        path, "\n".join(f"{json.dumps(k)} = {_toml_value(v)}" for k, v in config.items()) + "\n"
    )


def _ty_config(args: argparse.Namespace) -> None:
    _write_ty_config(args.output, _read_ty_config(args.base, args.pyproject))


def _ty_import_roots(cell: Path, package: Path, targets: dict[str, dict[str, list[str]]]) -> list[Path]:
    """The package's sources and its Python libraries' transitive source directories."""
    pending = [
        label
        for label, target in targets.items()
        if Path(label.split("//", 1)[1].split(":", 1)[0]) == package
        and "python-typecheck" in target.get("labels", [])
    ]
    if not pending:
        fail(f"no Python type-check targets in tine//{package}")
    roots = {cell / package}
    visited: set[str] = set()
    while pending:
        label = pending.pop()
        if label in visited:
            continue
        visited.add(label)
        target = targets[label]
        pending.extend(target.get("deps", []))
        for source in target.get("srcs", []):
            owner, path = source.split("//", 1)
            # Label-valued resources are not importable Python sources in the checkout.
            if ":" in path or Path(path).suffix not in (".py", ".pyi"):
                continue
            if owner != "tine":
                fail(f"ty's checkout configuration cannot resolve {source}")
            roots.add(cell / Path(path.lstrip("/")).parent)

    # Directory-based editor contexts expose siblings too. Refuse an ambiguous namespace rather
    # than silently choosing a different module from the one Buck would put in the flat tree.
    modules: dict[str, Path] = {}
    ordered = [cell / package, *sorted(roots - {cell / package})]
    for root in ordered:
        for source in sorted(root.glob("*.py*")):
            if source.suffix not in (".py", ".pyi"):
                continue
            previous = modules.setdefault(source.stem, source)
            if previous.parent != source.parent:
                fail(f"ambiguous ty import {source.stem!r}: {previous} and {source}")
    return ordered


def _ty_packages(labels: Iterable[str]) -> list[Path]:
    """The packages below the cell root that declare a type check."""
    return sorted({Path(label.split("//", 1)[1].split(":", 1)[0]) for label in labels} - {Path(".")})


def _require_manifests(cell: Path, packages: list[Path]) -> None:
    # Without a manifest, Zed would apply the parent's server settings to this package.
    manifests = [cell / package / "pyproject.toml" for package in packages]
    if missing := [manifest for manifest in manifests if not manifest.is_file()]:
        listing = "\n".join(f"  {manifest}" for manifest in missing)
        fail(f"missing Python project marker: add these to version control:\n{listing}")


def _ty_zed_files(cell: Path, targets: dict[str, dict[str, list[str]]]) -> None:
    """Give Zed distinct server arguments for each Python package."""
    packages = _ty_packages(
        label for label, target in targets.items() if "python-typecheck" in target.get("labels", [])
    )
    _require_manifests(cell, packages)
    marker = "// Generated by tools/ty.\n"
    for package in packages:
        settings = cell / package / ".zed/settings.json"
        if settings.exists() and not settings.read_text(encoding="utf-8").startswith(marker):
            fail(f"refusing to overwrite non-generated Zed settings: {settings}")
        configuration = {"lsp": {"ty": {"binary": {"arguments": ["--package", str(package), "server"]}}}}
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_if_changed(settings, marker + json.dumps(configuration, indent=2) + "\n")


def _box_search_paths(layers: Sequence[Path]) -> list[Path]:
    """The directories of a box's layers that contain third-party Python modules, top layer first.

    ty reads one Python environment, and a box splits its packages over several layers. ty takes the
    standard library from its own stubs, so the box only has to provide these search paths.
    """
    return [
        directory
        for layer in reversed(layers)
        # Debian installs modules into dist-packages, and Fedora installs compiled ones under lib64.
        for pattern in ("usr/lib*/python3*/site-packages", "usr/lib/python3/dist-packages")
        for directory in sorted(layer.glob(pattern))
    ]


def _ty_environment(cell: Path, package: Path, python: Path) -> dict[str, object]:
    """Select pinned Python and the package's explicitly requested box-provided imports."""
    manifest = cell / package / "pyproject.toml"
    config = tomllib.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    # The editor reads this from .buck, not the working directory of the Buck command.
    environment: dict[str, object] = {"python": str(python.absolute())}
    box = config.get("tool", {}).get("tine", {}).get("ty", {}).get("box")
    if box is not None:
        if not isinstance(box, str) or not box.endswith(".box"):
            fail(f"{manifest}: tool.tine.ty.box must name a Buck box target")
        # Tests use boxes even for stdlib-only sources, so their runtime is not an editor requirement.
        # Build the explicitly chosen box only for the package whose server is starting.
        listing = Path(buck_output("build", "--show-full-simple-output", f"{box}[layers]"))
        project = Path(buck_output("root", "--kind", "project"))
        layers = [project / layer for layer in json.loads(listing.read_text(encoding="utf-8"))]
        environment["extra-paths"] = [str(path) for path in _box_search_paths(layers)]
    return environment


def _ty_check(args: argparse.Namespace) -> None:
    command = [args.ty, "check", "--project", args.project, "--python", args.python]
    for path in _box_search_paths(args.layer):
        command += ["--extra-search-path", str(path)]
    if returncode := subprocess.run([*command, *args.files]).returncode:
        sys.exit(returncode)
    # ty reports through its exit status alone, so the output Buck tracks only says that it passed.
    args.output.touch()


def _ty(args: argparse.Namespace) -> None:
    package = Path(args.package)
    if package.is_absolute() or ".." in package.parts:
        fail("--package must be a directory relative to the tine cell")
    cell = _cell_root("tine")
    targets = json.loads(
        buck_output(
            "uquery",
            "attrfilter(labels, 'python-typecheck', tine//...)"
            " + kind('python_bootstrap_library', tine//...)",
            "--output-attribute",
            "^(srcs|deps|labels)$",
        )
    )
    roots = _ty_import_roots(cell, package, targets)
    environment = _ty_environment(cell, package, Path(args.python))
    _ty_zed_files(cell, targets)
    manifest = cell / package / "pyproject.toml"
    config = _read_ty_config(cell / "ty.toml", manifest if manifest.is_file() else None)
    config.setdefault("environment", {}).update(
        environment,
        root=[str(root) for root in roots],
    )
    config.setdefault("src", {})["include"] = [str(cell / package / "*.py")]
    path = cell / ".buck/ty" / package / "ty.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_ty_config(path, config)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["TY_CONFIG_FILE"] = str(path)
    # Zed sends the same dynamic settings to every instance; ty expands this in its own process.
    env["TINE_TY_CONFIG"] = str(path)
    executable = Path(args.ty).absolute()
    os.execve(executable, [str(executable), *args.arguments], env)


def main(argv: list[str] | None = None) -> None:
    starlark = argparse.ArgumentParser(add_help=False)
    starlark.add_argument("--starlark-fmt", required=True)
    starlark.add_argument("--starlark-fmt-config", required=True, help="starlark_fmt --config tables")
    p = argparse.ArgumentParser(prog="dev")
    sub = p.add_subparsers(dest="command", required=True)

    for name, func, help_text in (
        ("lint", _lint, "run the source lints (fmt fixes)"),
        ("check", _check, "run the source lints, then every unit-test suite"),
    ):
        verb = sub.add_parser(name, parents=[starlark], help=help_text)
        verb.add_argument("--ruff", required=True)
        verb.set_defaults(func=func)

    universe = sub.add_parser("universe", help="print the query for what a whole-cell run asks for here")
    universe.set_defaults(func=lambda _args: print(_universe()))

    fmt = sub.add_parser("fmt", parents=[starlark], help="auto-format and auto-fix lints")
    fmt.add_argument("--ruff", required=True)
    fmt.set_defaults(func=_fmt)

    scc = sub.add_parser("scc", help="analyze a branch's BuildRequires cycles")
    scc.add_argument("branch", help="branch label, e.g. //packages/fedora/rawhide")
    scc.add_argument("--why", metavar="PKG", help="show one cycle member's edges and their reasons")
    scc.add_argument("--dot", type=Path, help="write the cycle subgraph as graphviz")
    scc.set_defaults(func=_scc)

    ty_config = sub.add_parser("ty-config", help="merge shared ty settings with a package's pyproject")
    ty_config.add_argument("--base", type=Path, required=True)
    ty_config.add_argument("--pyproject", type=Path, required=True)
    ty_config.add_argument("--output", type=Path, required=True)
    ty_config.set_defaults(func=_ty_config)

    ty_check = sub.add_parser("ty-check", help="type-check against the pinned Python and a box's layers")
    ty_check.add_argument("--python", required=True, help="pinned standalone Python")
    ty_check.add_argument("--ty", required=True)
    ty_check.add_argument("--layer", type=Path, action="append", default=[], help="box layer, bottom first")
    ty_check.add_argument("--output", type=Path, required=True)
    ty_check.add_argument("--project", required=True, help="the isolated tree with its ty.toml")
    ty_check.add_argument("files", nargs="+")
    ty_check.set_defaults(func=_ty_check)

    ty = sub.add_parser("ty", help="run pinned ty in a package's Python environment")
    ty.add_argument("--python", required=True, help="pinned standalone Python for stdlib-only packages")
    ty.add_argument("--ty", required=True)
    ty.add_argument("--package", default=".", help="Python source package relative to the tine cell")
    ty.add_argument("arguments", nargs=argparse.REMAINDER)
    ty.set_defaults(func=_ty)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
