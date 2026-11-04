# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Regression tests for package-scoped editor import environments."""

import argparse
import contextlib
import json
import os
import select
import subprocess
import tempfile
import time
import tomllib
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast, override
from unittest import mock

import dev


class LanguageServer:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.buffer = b""
        self.request_id = 0

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps({"jsonrpc": "2.0", **message}).encode()
        assert self.process.stdin is not None
        self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + 15
        while True:
            header, separator, body = self.buffer.partition(b"\r\n\r\n")
            if separator:
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                if len(body) >= length:
                    self.buffer = body[length:]
                    return cast(dict[str, Any], json.loads(body[:length]))
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("ty did not respond within 15 seconds")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError("ty exited before responding")
            self.buffer += chunk

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        while True:
            response = self.receive()
            if "method" in response:
                if "id" in response:
                    result = None
                    if response["method"] == "workspace/configuration":
                        # Zed sends this same setting to every server, even for nested manifests.
                        result = [
                            {"configurationFile": "${TINE_TY_CONFIG}"} for _ in response["params"]["items"]
                        ]
                    self.send({"id": response["id"], "result": result})
            elif response.get("id") == self.request_id:
                if "error" in response:
                    raise AssertionError(response["error"])
                return response["result"]

    def check(self, path: Path, text: str) -> list[dict[str, Any]]:
        self.send(
            {
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": path.as_uri(),
                        "languageId": "python",
                        "version": 1,
                        "text": text,
                    }
                },
            }
        )
        result = self.request("textDocument/diagnostic", {"textDocument": {"uri": path.as_uri()}})
        assert isinstance(result, dict)
        return cast(list[dict[str, Any]], result["items"])


class TyPackages(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/var/tmp"))
        self.root = Path(directory)
        self.targets: dict[str, dict[str, list[str]]] = {
            "tine//cargo:test-ty": {
                "labels": ["python-typecheck"],
                "srcs": ["tine//cargo/build.py", "tine//cargo/use.py"],
                "deps": [],
            },
            "tine//go:test-ty": {
                "labels": ["python-typecheck"],
                "srcs": ["tine//go/build.py", "tine//go/use.py", "tine//:resource"],
                "deps": ["tine//shared:helper"],
            },
            "tine//shared:helper": {
                "srcs": ["tine//shared/helper.py"],
                "deps": ["tine//:util"],
            },
            "tine//:util": {"srcs": ["tine///util.py"], "deps": []},
        }
        for package in ("cargo", "go", "shared"):
            (self.root / package).mkdir()
        # The layers of a box, bottom first. Each module is installed where one of the distributions
        # installs it. shadowed.py is in both layers, and the upper layer's VALUE is an int.
        self.layers = [self.root / "box/lower", self.root / "box/upper"]
        for layer, module, text in (
            (self.layers[0], "usr/lib64/python3.14/site-packages/libdnf5.py", ""),
            (self.layers[0], "usr/lib/python3/dist-packages/createrepo_c.py", ""),
            (self.layers[0], "usr/lib/python3.14/site-packages/shadowed.py", 'VALUE: str = "lower"\n'),
            (self.layers[1], "usr/lib/python3.14/site-packages/pefile.py", ""),
            (self.layers[1], "usr/lib/python3.14/site-packages/shadowed.py", "VALUE: int = 42\n"),
        ):
            (layer / module).parent.mkdir(parents=True, exist_ok=True)
            (layer / module).write_text(text)
        # The box's [layers] subtarget lists the layers relative to the project.
        self.listing = self.root / "box/layers.json"
        self.listing.write_text(json.dumps([str(layer.relative_to(self.root)) for layer in self.layers]))
        for package in ("cargo", "go"):
            (self.root / package / "pyproject.toml").write_text("# Python project marker for Zed.\n")
        (self.root / "ty.toml").write_text(
            '[environment]\npython-version = "3.14"\n[rules]\nall = "error"\n'
        )
        (self.root / "cargo/build.py").write_text('VALUE: str = "cargo"\n')
        (self.root / "go/build.py").write_text("VALUE: int = 42\n")
        (self.root / "shared/helper.py").write_text("from util import VALUE\n")
        (self.root / "util.py").write_text("VALUE: int = 42\n")

    def buck_output(self, command: str, *arguments: str) -> str:
        """Answer the Buck commands the editor's ty runs for the fixture."""
        match (command, *arguments):
            case ("uquery", *_):
                return json.dumps(self.targets)
            case ("build", "--show-full-simple-output", "catalog//:fedora.rawhide.box[layers]"):
                return str(self.listing)
            case ("root", "--kind", "project"):
                return str(self.root)
        self.fail(f"unexpected Buck command: {command} {arguments}")

    def launch(self, package: str) -> tuple[list[str], dict[str, str]]:
        args = argparse.Namespace(
            package=package,
            python=os.environ["TY_TEST_PYTHON"],
            ty=os.environ["TY_TEST_BINARY"],
            arguments=["server"],
        )

        with (
            mock.patch.object(dev, "_cell_root", return_value=self.root),
            mock.patch.object(dev, "buck_output", side_effect=self.buck_output),
            mock.patch.object(dev.os, "execve") as execute,
        ):
            dev._ty(args)
        execute.assert_called_once()
        return cast(list[str], execute.call_args.args[1]), cast(dict[str, str], execute.call_args.args[2])

    def test_builds_a_box_only_for_opted_in_packages(self) -> None:
        # The generated config is read outside Buck's working directory.
        python = Path(os.environ["TY_TEST_PYTHON"]).absolute()
        with mock.patch.object(dev, "buck_output", side_effect=self.buck_output) as buck:
            for package in (".", "cargo", "go"):
                with self.subTest(package=package):
                    self.assertEqual(
                        dev._ty_environment(self.root, Path(package), python),
                        {"python": str(python)},
                    )
            buck.assert_not_called()
            (self.root / "go/pyproject.toml").write_text(
                '[tool.tine.ty]\nbox = "catalog//:fedora.rawhide.box"\n'
            )
            lower, upper = self.layers
            self.assertEqual(
                dev._ty_environment(self.root, Path("go"), python),
                {
                    "python": str(python),
                    "extra-paths": [
                        str(upper / "usr/lib/python3.14/site-packages"),
                        str(lower / "usr/lib/python3.14/site-packages"),
                        str(lower / "usr/lib64/python3.14/site-packages"),
                        str(lower / "usr/lib/python3/dist-packages"),
                    ],
                },
            )
            buck.assert_any_call(
                "build", "--show-full-simple-output", "catalog//:fedora.rawhide.box[layers]"
            )

    def test_rejects_invalid_packages(self) -> None:
        for package, error in (
            ("missing", "no Python type-check targets"),
            ("../go", "must be a directory relative to the tine cell"),
            ("/go", "must be a directory relative to the tine cell"),
        ):
            with self.subTest(package=package), self.assertRaisesRegex(SystemExit, error):
                self.launch(package)

    def test_refuses_shadowing_from_an_exposed_dependency_directory(self) -> None:
        (self.root / "shared/build.py").write_text("VALUE = False\n")
        with self.assertRaisesRegex(SystemExit, "ambiguous ty import 'build'"):
            dev._ty_import_roots(self.root, Path("go"), self.targets)

    def test_each_process_gets_its_own_configuration(self) -> None:
        (self.root / "go/pyproject.toml").write_text(
            '[tool.ty.environment]\npython-version = "3.12"\n'
            '[tool.ty.rules]\ninvalid-assignment = "ignore"\n'
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/unrelated"}):
            _, cargo = self.launch("cargo")
            _, go = self.launch("go")
        self.assertNotEqual(cargo["TINE_TY_CONFIG"], go["TINE_TY_CONFIG"])
        for package, env in (("cargo", cargo), ("go", go)):
            with self.subTest(package=package):
                self.assertEqual(env["TY_CONFIG_FILE"], env["TINE_TY_CONFIG"])
                self.assertNotIn("PYTHONPATH", env)
                path = Path(env["TINE_TY_CONFIG"])
                config = tomllib.loads(path.read_text())
                self.assertEqual(
                    config["environment"]["python-version"], "3.12" if package == "go" else "3.14"
                )
                self.assertEqual(
                    config["rules"],
                    {"all": "error", **({"invalid-assignment": "ignore"} if package == "go" else {})},
                )
                self.assertEqual(config["src"]["include"], [str(self.root / package / "*.py")])
                modified = path.stat().st_mtime_ns
                self.launch(package)
                self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_buck_config_merges_package_settings_and_enforces_the_python_floor(self) -> None:
        base = self.root / "ty.toml"
        base.write_text(base.read_text() + '[analysis]\nallowed-unresolved-imports = ["shared_optional"]\n')
        manifest = self.root / "go/pyproject.toml"
        manifest.write_text(
            '[project]\nname = "go"\n'
            '[tool.ty.environment]\npython-version = "3.12"\n'
            '[tool.ty.rules]\ninvalid-assignment = "ignore"\n'
            '[tool.ty.analysis]\nallowed-unresolved-imports = ["package_optional"]\n'
            '[tool.tine.ty]\nbox = "catalog//:fedora.rawhide.box"\n'
        )
        output = self.root / "merged.toml"
        dev.main(["ty-config", "--base", str(base), "--pyproject", str(manifest), "--output", str(output)])
        self.assertEqual(
            tomllib.loads(output.read_text()),
            {
                "environment": {"python-version": "3.12"},
                "rules": {"all": "error", "invalid-assignment": "ignore"},
                "analysis": {"allowed-unresolved-imports": ["shared_optional", "package_optional"]},
            },
        )
        source = self.root / "probe.py"
        source.write_text(
            "import compression.zstd\nimport shared_optional\nimport package_optional\n"
            'value: int = "wrong"\n'
        )
        result = subprocess.run(
            [
                os.environ["TY_TEST_BINARY"],
                "check",
                "--config-file",
                str(output),
                "--python",
                os.environ["TY_TEST_PYTHON"],
                "--output-format",
                "concise",
                str(source),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("error["), 1, result.stdout)
        self.assertIn("import-not-found", result.stdout)
        self.assertIn("compression.zstd", result.stdout)

    def test_launch_generates_settings_without_a_root_manifest(self) -> None:
        self.targets["tine//:util-ty"] = {"labels": ["python-typecheck"], "srcs": ["tine///util.py"]}
        (self.root / "cargo/pyproject.toml").write_text('[project]\nname = "cargo"\nversion = "1.0"\n')
        manifests = {
            self.root / p / "pyproject.toml": (self.root / p / "pyproject.toml").read_bytes()
            for p in ("cargo", "go")
        }
        settings = self.root / ".zed/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"languages": {"Python": {"language_servers": ["ty"]}}}\n')
        root_settings = settings.read_bytes()
        ty_config = (self.root / "ty.toml").read_bytes()

        self.launch(".")
        generated = []
        for package in ("cargo", "go"):
            manifest = self.root / package / "pyproject.toml"
            settings = self.root / package / ".zed/settings.json"
            self.assertEqual(manifest.read_bytes(), manifests[manifest])
            config = json.loads(settings.read_text().split("\n", 1)[1])
            self.assertEqual(config["lsp"]["ty"]["binary"]["arguments"], ["--package", package, "server"])
            generated.extend([manifest, settings])
        self.assertFalse((self.root / "shared/pyproject.toml").exists())
        self.assertFalse((self.root / "pyproject.toml").exists())
        self.assertEqual((self.root / "ty.toml").read_bytes(), ty_config)
        self.assertEqual((self.root / ".zed/settings.json").read_bytes(), root_settings)

        modified = [path.stat().st_mtime_ns for path in generated]
        self.launch("go")
        self.assertEqual([path.stat().st_mtime_ns for path in generated], modified)

    def test_missing_package_manifests_must_be_added_not_generated(self) -> None:
        manifest = self.root / "cargo/pyproject.toml"
        manifest.unlink()
        with self.assertRaisesRegex(SystemExit, "missing Python project marker"):
            self.launch("cargo")
        self.assertFalse(manifest.exists())

    def test_lint_rejects_a_package_without_a_manifest(self) -> None:
        (self.root / "cargo/pyproject.toml").unlink()
        labels = "\n".join(label for label in self.targets if label.endswith("-ty"))
        with (
            mock.patch.dict(os.environ, {"BUCK2_BINARY": "buck2"}),
            mock.patch.object(dev, "_cell_root", return_value=self.root),
            mock.patch.object(dev, "_starlark_srcs", return_value=[]),
            mock.patch.object(dev, "_orphan_tests", return_value=[]),
            mock.patch.object(dev, "_run"),
            mock.patch.object(dev, "buck_output", return_value=labels),
            self.assertRaisesRegex(SystemExit, "cargo/pyproject.toml"),
        ):
            dev._lint(argparse.Namespace(ruff="ruff"))

    def test_refuses_to_overwrite_custom_zed_settings(self) -> None:
        settings = self.root / "cargo/.zed/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"tab_size": 4}\n')
        with self.assertRaisesRegex(SystemExit, "refusing to overwrite non-generated Zed settings"):
            self.launch("cargo")
        self.assertEqual(settings.read_text(), '{"tab_size": 4}\n')

    @contextlib.contextmanager
    def server(self, package: str) -> Generator[LanguageServer]:
        command, env = self.launch(package)
        # Exercise Zed's configurationFile expansion without the CLI configuration as a fallback.
        env.pop("TY_CONFIG_FILE")
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env) as process:
            try:
                server = LanguageServer(process)
                server.request(
                    "initialize",
                    {
                        "processId": None,
                        "rootUri": self.root.as_uri(),
                        "workspaceFolders": [{"uri": self.root.as_uri(), "name": "tine"}],
                        "capabilities": {
                            "workspace": {"configuration": True},
                            "textDocument": {"diagnostic": {}},
                        },
                    },
                )
                server.send({"method": "initialized", "params": {}})
                yield server
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def test_servers_with_the_same_workspace_keep_import_environments_separate(self) -> None:
        (self.root / "go/pyproject.toml").write_text(
            '[tool.tine.ty]\nbox = "catalog//:fedora.rawhide.box"\n'
        )
        with self.server("cargo") as cargo, self.server("go") as go:
            for package, server, kind in (("cargo", cargo, "str"), ("go", go, "int")):
                with self.subTest(package=package):
                    path = self.root / package / "use.py"
                    self.assertEqual(
                        server.check(path, f"from build import VALUE\nvalue: {kind} = VALUE\n"), []
                    )
                    definition = server.request(
                        "textDocument/definition",
                        {"textDocument": {"uri": path.as_uri()}, "position": {"line": 0, "character": 7}},
                    )
                    assert isinstance(definition, list)
                    self.assertEqual(definition[0]["uri"], (self.root / package / "build.py").as_uri())

            for package, server in (("cargo", cargo), ("go", go)):
                with self.subTest(package=package, import_name="helper"):
                    path = self.root / package / "probe.py"
                    diagnostics = server.check(path, "from helper import VALUE\nvalue: int = VALUE\n")
                    if package == "cargo":
                        self.assertIn("import-not-found", [item["code"] for item in diagnostics])
                    else:
                        self.assertEqual(diagnostics, [])

            for package, server in (("cargo", cargo), ("go", go)):
                with self.subTest(package=package, import_name="box modules"):
                    diagnostics = server.check(
                        self.root / package / "box_probe.py",
                        "import libdnf5\nimport createrepo_c\nimport pefile\n",
                    )
                    if package == "cargo":
                        self.assertEqual([item["code"] for item in diagnostics], ["import-not-found"] * 3)
                    else:
                        self.assertEqual(diagnostics, [])

            with self.subTest(package="go", import_name="upper layer"):
                diagnostics = go.check(
                    self.root / "go/layer_probe.py", "from shadowed import VALUE\nvalue: int = VALUE\n"
                )
                self.assertEqual(diagnostics, [])
