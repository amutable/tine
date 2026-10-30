# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the Python request the sandbox launcher composes.

buck test tine//box:test
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout, suppress
from pathlib import Path

import sandbox
from isolation import AT_FDCWD, Bind, Close, Devices, File, Filesystem, Symlink, Tmpfs, open_layers
from sandbox import _PROJECT


def _tools(root: Path) -> Path:
    """A tools tree holding one of each entry the launcher treats differently."""
    tools = root / "tools"
    (tools / "usr/bin").mkdir(parents=True)
    (tools / "bin").symlink_to("usr/bin")
    (tools / "etc").mkdir()
    (tools / "etc/os-release").touch()
    (tools / "home").mkdir()  # empty, as a distribution ships it
    for name in ("proc", "sys", "dev", "run", "tmp", "boot"):
        (tools / name).mkdir()
    return tools


@contextmanager
def _project() -> Generator[tuple[Path, Path]]:
    """Enter a temporary project directory, with a tools tree beside it."""
    with tempfile.TemporaryDirectory(prefix="sandbox-test.", dir="/var/tmp") as scratch:
        root = Path(scratch)
        project = root / "project"
        project.mkdir()
        previous = Path.cwd()
        os.chdir(project)
        try:
            # No scratch directory: `buck test` is not a run action, and a test that wants one
            # sets it itself.
            with unittest.mock.patch.dict(os.environ):
                os.environ.pop("BUCK_SCRATCH_PATH", None)
                yield project, _tools(root)
        finally:
            os.chdir(previous)


def _launch(*args: str) -> sandbox.Launch:
    options = sandbox._parse(list(args))
    with Close(open_layers(tuple(reversed(options.tools)))) as tools:
        return sandbox._launch(options, tools)


def _unshare(*args: str) -> unittest.mock.MagicMock:
    unshare = unittest.mock.MagicMock()
    with (
        unittest.mock.patch.object(sandbox, "unshare_sandbox", unshare),
        unittest.mock.patch.object(sandbox, "enter_sandbox"),
        unittest.mock.patch.object(os, "execvpe"),
        suppress(SystemExit),
    ):
        sandbox.main(list(args))
    return unshare


def _binds(launch: sandbox.Launch) -> list[Bind]:
    return [filesystem for filesystem in launch.filesystems if isinstance(filesystem, Bind)]


def _bind_specs(launch: sandbox.Launch) -> list[tuple[Path, Path, bool, bool]]:
    return [(Path(bind.source), Path(bind.target), bind.readonly, bind.nofollow) for bind in _binds(launch)]


def _symlinks(launch: sandbox.Launch) -> list[Symlink]:
    return [filesystem for filesystem in launch.filesystems if isinstance(filesystem, Symlink)]


def _tmpfs(launch: sandbox.Launch) -> list[Tmpfs]:
    return [filesystem for filesystem in launch.filesystems if isinstance(filesystem, Tmpfs)]


class TestRequest(unittest.TestCase):
    def test_options_accept_separate_and_attached_values(self) -> None:
        for attached in (False, True):
            with self.subTest(attached=attached):
                values = [
                    ("--tools", "tools"),
                    ("--ro-bind", "/src:/dst"),
                    ("--ro-bind", "/other:/elsewhere"),
                    ("--setenv", "KEY=value=with=equals"),
                    ("--setenv", "EMPTY="),
                    ("--source-date-epoch", "-1"),
                ]
                argv = [
                    word
                    for option, value in values
                    for word in ([f"{option}={value}"] if attached else [option, value])
                ]
                argv += ["--bind-cwd", "--network", "--", "command", "--tools", "argument", "--help"]
                original = argv.copy()

                args = sandbox._parse(argv)

                self.assertEqual(argv, original)
                self.assertEqual(args.tools, ["tools"])
                self.assertEqual(args.ro_bind, [("/src", "/dst"), ("/other", "/elsewhere")])
                self.assertEqual(args.setenv, {"KEY": "value=with=equals", "EMPTY": ""})
                self.assertEqual(args.source_date_epoch, -1)
                self.assertTrue(args.bind_cwd)
                self.assertTrue(args.network)
                self.assertEqual(args.cmd, ["command", "--tools", "argument", "--help"])

    def test_first_command_stops_option_parsing(self) -> None:
        args = sandbox._parse(["--tools=tools", "command", "--network", "--help"])

        self.assertFalse(args.network)
        self.assertEqual(args.cmd, ["command", "--network", "--help"])

    def test_command_arguments_are_preserved_verbatim(self) -> None:
        command = ["command", "", "-", "--", "--tools=other", "--network=yes", "a=b", "--help"]
        for separator in ([], ["--"]):
            with self.subTest(separator=separator):
                argv = ["--tools=tools", *separator, *command]
                original = argv.copy()

                self.assertEqual(sandbox._parse(argv).cmd, command)
                self.assertEqual(argv, original)

    def test_repeated_values_keep_the_last_scalar_and_all_binds_and_layers(self) -> None:
        args = sandbox._parse(
            [
                "--tools=first",
                "--tools",
                "second",
                "--source-date-epoch=1",
                "--source-date-epoch",
                "-2",
                "--setenv=KEY=first",
                "--setenv",
                "KEY=second=last",
                "--ro-bind=/first:/dst",
                "--ro-bind",
                "/second:/dst",
                "--",
                "true",
            ]
        )

        self.assertEqual(args.tools, ["first", "second"])
        self.assertEqual(args.source_date_epoch, -2)
        self.assertEqual(args.setenv, {"KEY": "second=last"})
        self.assertEqual(args.ro_bind, [("/first", "/dst"), ("/second", "/dst")])

    def test_lone_dash_is_a_command_not_an_option(self) -> None:
        args = sandbox._parse(["--tools=tools", "-", "--network", "--help"])

        self.assertFalse(args.network)
        self.assertEqual(args.cmd, ["-", "--network", "--help"])

    def test_option_values_can_start_with_a_dash(self) -> None:
        for value in ("-", "-tools", "--tools"):
            with self.subTest(value=value):
                argument = [f"--tools={value}"] if value.startswith("--") else ["--tools", value]
                self.assertEqual(sandbox._parse([*argument, "--", "true"]).tools, [value])

    def test_default_arguments_come_from_sys_argv(self) -> None:
        with unittest.mock.patch.object(sys, "argv", ["sandbox", "--tools=tools", "--", "true"]):
            self.assertEqual(sandbox._parse(None).cmd, ["true"])

    def test_help_needs_no_tools_or_command(self) -> None:
        for option in ("-h", "--help"):
            with self.subTest(option=option), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    sandbox._parse([option])
                self.assertEqual(raised.exception.code, 0)
                self.assertIn("--tools", output.getvalue())

    def test_tools_are_required(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--tools is required"):
            sandbox._parse(["--", "true"])

    def test_missing_option_values_are_errors(self) -> None:
        for option in ("--tools", "--ro-bind", "--setenv", "--source-date-epoch", "--box"):
            with self.subTest(option=option), self.assertRaisesRegex(SystemExit, "requires a value"):
                sandbox._parse([option])

    def test_empty_option_values_are_errors(self) -> None:
        for option in ("--tools", "--ro-bind", "--setenv", "--source-date-epoch", "--box"):
            for argument in ([option, ""], [f"{option}="]):
                with self.subTest(argument=argument), self.assertRaises(SystemExit):
                    sandbox._parse(["--tools=tools", *argument, "--", "true"])

    def test_options_do_not_consume_flags_or_the_command_separator(self) -> None:
        for option in ("--tools", "--ro-bind", "--setenv", "--source-date-epoch", "--box"):
            for following in ("--network", "--", "-h"):
                with (
                    self.subTest(option=option, following=following),
                    self.assertRaisesRegex(SystemExit, "requires a value"),
                ):
                    sandbox._parse([option, following, "true"])

    def test_an_option_cannot_supply_another_options_value(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--tools requires a value"):
            sandbox._parse(["--tools", "--network", "--", "true"])

    def test_unknown_options_and_values_for_flags_are_errors(self) -> None:
        for option in ("--unknown", "--network=yes", "--bind-cwd=no", "--relaxed=1"):
            with self.subTest(option=option), self.assertRaisesRegex(SystemExit, "unrecognized option"):
                sandbox._parse(["--tools=tools", option, "--", "true"])

    def test_source_date_epoch_must_be_an_integer(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--source-date-epoch requires an integer"):
            sandbox._parse(["--tools=tools", "--source-date-epoch=invalid", "--", "true"])

    def test_repeated_parses_do_not_share_options(self) -> None:
        sandbox._parse(["--tools=tools", "--setenv=KEY=value", "--network", "--", "true"])
        args = sandbox._parse(["--tools=other", "--", "false"])

        self.assertEqual(args.setenv, {})
        self.assertFalse(args.network)
        self.assertEqual(args.cmd, ["false"])

    def test_command_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools")

    def test_relaxed_rejects_bind_cwd(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools", "--relaxed", "--bind-cwd", "--", "true")

    def test_box_requires_relaxed(self) -> None:
        with self.assertRaises(SystemExit):
            _launch("--tools", "tools", "--box", "dev", "--", "true")

    def test_ro_bind_needs_a_destination(self) -> None:
        with _project() as (_, tools):
            with self.assertRaises(SystemExit):
                _launch("--tools", str(tools), "--ro-bind", "/run/signer.sock", "--", "true")

    def test_invalid_bind_pairs_are_rejected_during_parsing(self) -> None:
        for value in ("/src", ":/dst", "/src:", "/src:relative"):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "--ro-bind"):
                sandbox._parse(["--tools=tools", "--ro-bind", value, "--", "true"])

    def test_invalid_environment_pairs_are_rejected_during_parsing(self) -> None:
        for value in ("KEY", "=value"):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "--setenv"):
                sandbox._parse(["--tools=tools", "--setenv", value, "--", "true"])


class TestHermetic(unittest.TestCase):
    def test_usr_merge_links_are_recreated_rather_than_bound(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            self.assertIn(("usr/bin", "/bin"), [(link.source, link.target) for link in _symlinks(launch)])
            self.assertNotIn("bin", [bind.source for bind in _binds(launch) if bind.root != AT_FDCWD])

    def test_tools_directories_are_bound_read_only(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            bound = {
                bind.source: bind.target
                for bind in _binds(launch)
                if bind.readonly and bind.root != AT_FDCWD
            }
            self.assertEqual(bound["usr"], "/usr")
            self.assertEqual(bound["etc"], "/etc")
            self.assertEqual(bound["home"], "/home")

    def test_the_sandbox_supplies_the_api_filesystems_itself(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--", "true")

            for name in ("proc", "sys", "dev", "run", "tmp", "boot"):
                self.assertNotIn(name, [bind.source for bind in _binds(launch) if bind.root != AT_FDCWD])
            self.assertIn((Path("/proc"), Path("/proc"), False, False), _bind_specs(launch))
            self.assertIn(
                ("/dev", None),
                [(fs.target, fs.tty) for fs in launch.filesystems if isinstance(fs, Devices)],
            )
            self.assertEqual([tmpfs.target for tmpfs in _tmpfs(launch)], ["/run", "/tmp", "/var/tmp"])

    def test_project_is_bound_at_a_fixed_path(self) -> None:
        with _project() as (project, tools):
            launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn((project, Path(_PROJECT), False, False), _bind_specs(launch))
            self.assertEqual(launch.chdir, _PROJECT)

    def test_tools_directory_on_the_project_path_is_bound_like_any_other(self) -> None:
        with _project() as (project, tools):
            # A checkout under /var/lib or /root shares its first path component with a directory
            # the box populates. The fixed project path is what keeps the two independent.
            occupied = tools / project.parts[1]
            (occupied / "lib").mkdir(parents=True)

            launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertIn((Path(occupied.name), Path("/") / occupied.name, True, False), _bind_specs(launch))

    def test_absolute_project_paths_in_the_command_follow_the_mount(self) -> None:
        with _project() as (project, tools):
            launch = _launch(
                "--tools",
                str(tools),
                "--bind-cwd",
                "--",
                f"{project}/buck-out/driver.py",
                f"PYTHONPATH={project}/buck-out/lib",
                str(project),
            )

            self.assertEqual(
                launch.command,
                (
                    f"{_PROJECT}/buck-out/driver.py",
                    f"PYTHONPATH={_PROJECT}/buck-out/lib",
                    _PROJECT,
                ),
            )

    def test_paths_outside_the_project_are_left_alone(self) -> None:
        with _project() as (project, tools):
            sibling = f"{project}-other/artifact"

            launch = _launch(
                "--tools",
                str(tools),
                "--bind-cwd",
                "--",
                "true",
                "/run/signer.sock",
                sibling,
            )

            self.assertEqual(launch.command, ("true", "/run/signer.sock", sibling))

    def test_scratch_backs_var_tmp_on_disk(self) -> None:
        with _project() as (project, tools):
            with unittest.mock.patch.dict(os.environ, {"BUCK_SCRATCH_PATH": "scratch"}):
                launch = _launch("--tools", str(tools), "--bind-cwd", "--", "true")

            self.assertNotIn("/var/tmp", [tmpfs.target for tmpfs in _tmpfs(launch)])
            self.assertIn((project / "scratch/var-tmp", Path("/var/tmp"), False, False), _bind_specs(launch))
            self.assertTrue((project / "scratch/var-tmp").is_dir())

    def test_network_replaces_the_unshared_namespace(self) -> None:
        with _project() as (_, tools):
            args = ("--tools", str(tools), "--network", "--", "true")
            launch = _launch(*args)

            self.assertFalse(_unshare(*args).call_args.kwargs["isolate_network"])
            self.assertIn((Path("/run"), Path("/run"), True, False), _bind_specs(launch))
            self.assertIn(
                (Path("/etc/resolv.conf"), Path("/etc/resolv.conf"), True, True),
                _bind_specs(launch),
            )

    def test_builds_get_fakeroot_semantics(self) -> None:
        with _project() as (_, tools):
            calls = unittest.mock.MagicMock()
            with (
                unittest.mock.patch.object(sandbox, "unshare_sandbox", calls.unshare_sandbox),
                unittest.mock.patch.object(sandbox, "enter_sandbox", calls.enter_sandbox),
                unittest.mock.patch.object(os, "execvpe"),
                suppress(SystemExit),
            ):
                sandbox.main(["--tools", str(tools), "--", "true"])

            self.assertTrue(calls.unshare_sandbox.call_args.kwargs["become_root"])
            self.assertTrue(calls.enter_sandbox.call_args.kwargs["suppress_chown"])
            self.assertTrue(calls.enter_sandbox.call_args.kwargs["suppress_sync"])


class TestRelaxed(unittest.TestCase):
    def test_userspace_comes_from_tools_and_the_rest_from_the_host(self) -> None:
        with _project() as (project, tools):
            args = ("--tools", str(tools), "--relaxed", "--", "true")
            launch = _launch(*args)

            self.assertIn((Path("usr"), Path("/usr"), True, False), _bind_specs(launch))
            self.assertEqual(launch.chdir, str(project))
            namespaces = _unshare(*args).call_args.kwargs
            self.assertFalse(namespaces["isolate_network"])
            self.assertFalse(namespaces["become_root"])

    def test_path_keeps_the_host_order_and_appends_missing_base_directories(self) -> None:
        with (
            _project() as (_, tools),
            unittest.mock.patch.dict(os.environ, {"PATH": "/home/u/bin:/usr/bin"}),
        ):
            launch = _launch("--tools", str(tools), "--relaxed", "--", "true")

        self.assertEqual(launch.environment["PATH"], "/home/u/bin:/usr/bin:/usr/sbin:/bin:/sbin")

    def test_a_development_box_may_choose_its_shell_after_entry(self) -> None:
        with _project() as (_, tools):
            launch = _launch("--tools", str(tools), "--relaxed", "--box", "dev")

        self.assertEqual(launch.command, ())


def _layer(tools: Path) -> Path:
    layer = tools.parent / "layer"
    (layer / "usr/bin").mkdir(parents=True)
    (layer / "usr/bin/tool").write_text("from the layer\n")
    (layer / "etc").mkdir()
    (layer / "etc/passwd").write_text("layered:x:1:1::/:/bin/sh\n")
    (layer / "srv").mkdir()
    return layer


def _runnable(tools: Path) -> Path:
    # The test layers contain no programs, so a command runs the test box's shell, bound in from /usr.
    # The lib and lib64 links point into that bound /usr, so the dynamic loader finds the libraries
    # of the shell.
    host = tools.parent / "host-usr"
    for name in ("lib", "lib64"):
        (tools / name).symlink_to(host / name)
    # Relaxed mode binds files over /etc/passwd, /etc/group and /etc/resolv.conf for every command,
    # even for a command that reads none of the files. The tools tree's /etc is bound read-only, so
    # the sandbox cannot create a missing mount point in it. enter_sandbox() then fails with EROFS.
    for name in ("passwd", "group", "resolv.conf"):
        (tools / "etc" / name).touch()
    (tools / "usr/bin/base").write_text("from the base\n")
    return host


def _start_relaxed(layers: tuple[Path, ...], host: Path, script: str) -> subprocess.Popen[str]:
    # The hermetic mode loads libseccomp from the box's root, and the test's tools tree doesn't contain
    # it. Run the sandbox in relaxed mode instead.
    return subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import sandbox; sandbox.main(sys.argv[2:])",
            str(Path(sandbox.__file__).parent),
            *(word for layer in layers for word in ("--tools", str(layer))),
            *("--relaxed", "--ro-bind", f"/usr:{host}"),
            *("--", str(host / "bin/sh"), "-c", script),
        ],
        # Arch's dynamic loader searches only /usr/lib, which the test's tools tree replaces, so the lib
        # and lib64 links do not lead it to the bound /usr.
        env=os.environ | {"LD_LIBRARY_PATH": str(host / "lib")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_relaxed(layers: tuple[Path, ...], host: Path, script: str) -> subprocess.CompletedProcess[str]:
    with _start_relaxed(layers, host, script) as process:
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


class TestLayers(unittest.TestCase):
    def test_launch_reads_the_merged_tree(self) -> None:
        with _project() as (_, tools):
            layer = _layer(tools)
            hermetic = _launch("--tools", str(tools), "--tools", str(layer), "--", "true")
            relaxed = _launch("--tools", str(tools), "--tools", str(layer), "--relaxed", "--", "true")

            self.assertIn(("usr/bin", "/bin"), [(link.source, link.target) for link in _symlinks(hermetic)])
            self.assertIn((Path("srv"), Path("/srv"), True, False), _bind_specs(hermetic))
            self.assertIn((Path("home"), Path("/home"), True, False), _bind_specs(hermetic))
            files = [filesystem for filesystem in relaxed.filesystems if isinstance(filesystem, File)]
            passwd = next(file for file in files if file.target == "/etc/passwd")
            self.assertTrue(passwd.content.startswith("layered:"))

    def test_main_opens_the_tools_layers_top_first(self) -> None:
        for layers in (["/top"], ["/base", "/top"]):
            with self.subTest(layers=layers):
                calls = unittest.mock.MagicMock()
                launch = sandbox.Launch(filesystems=(), chdir=None, command=("true",), environment={})
                calls._launch.return_value = launch
                tools = os.open("/", os.O_PATH)
                calls.open_layers.return_value = tools
                argv = [word for layer in layers for word in ("--tools", layer)] + ["--", "true"]

                with (
                    unittest.mock.patch.object(sandbox, "unshare_sandbox", calls.unshare_sandbox),
                    unittest.mock.patch.object(sandbox, "open_layers", calls.open_layers),
                    unittest.mock.patch.object(sandbox, "_launch", calls._launch),
                    unittest.mock.patch.object(sandbox, "enter_sandbox", calls.enter_sandbox),
                    unittest.mock.patch.object(os, "execvpe"),
                    self.assertRaises(SystemExit),
                ):
                    sandbox.main(argv)

                self.assertEqual(
                    [call[0] for call in calls.mock_calls],
                    [
                        "unshare_sandbox",
                        "open_layers",
                        "_launch",
                        "enter_sandbox",
                    ],
                )
                calls.open_layers.assert_called_once_with(tuple(reversed(layers)))
                self.assertEqual(calls._launch.call_args.args[1], tools)
                # main() closes the file descriptor of the tools tree after enter_sandbox().
                with self.assertRaises(OSError):
                    os.fstat(tools)
                calls.enter_sandbox.assert_called_once_with(
                    (), None, suppress_chown=True, suppress_sync=True
                )

    def test_a_command_sees_a_single_layer(self) -> None:
        with _project() as (_, tools):
            host = _runnable(tools)

            process = _run_relaxed((tools,), host, f"{host}/bin/cat /usr/bin/base")

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, "from the base\n")

    def test_a_command_sees_the_merged_layers_and_the_top_layer_itself(self) -> None:
        with _project() as (_, tools):
            layer = _layer(tools)
            host = _runnable(tools)

            # Relaxed mode binds the host's /var, which contains the top layer. The ls command reads the
            # top layer through it. Its output contains only the files of the top layer.
            script = (
                f"{host}/bin/cat /usr/bin/base /usr/bin/tool; {host}/bin/ls {layer}/usr/bin; "
                f"{host}/bin/cat /proc/self/mountinfo"
            )
            process = _run_relaxed((tools, layer), host, script)

        self.assertEqual(process.returncode, 0, process.stderr)
        lines = process.stdout.splitlines()
        self.assertEqual(lines[:3], ["from the base", "from the layer", "tool"])
        # Only the binds of the tools tree show the merged tree. The overlay of the layers stays detached,
        # so it must not appear at any other mount point.
        merged = {line.split()[4] for line in lines[3:] if " - overlay " in line and str(layer) in line}
        self.assertEqual(merged, {"/usr", "/etc"})

    def test_a_start_leaves_the_identity_of_running_boxes_alone(self) -> None:
        with _project() as (_, tools):
            host = _runnable(tools)
            (tools / "etc/passwd").write_text("first:x:0:0::/:/bin/sh\n")
            # Print the first passwd line, wait for another box to start, and print it again.
            script = 'first() { read -r line < /etc/passwd; echo "$line"; }; first; read -r _; first'
            with _start_relaxed((tools,), host, script) as running:
                assert running.stdout is not None
                before = running.stdout.readline()
                (tools / "etc/passwd").write_text("second:x:0:0::/:/bin/sh\n")
                started = _run_relaxed((tools,), host, f"{host}/bin/cat /etc/passwd")
                after, stderr = running.communicate("\n")

        self.assertEqual(running.returncode, 0, stderr)
        self.assertEqual(before + after, "first:x:0:0::/:/bin/sh\n" * 2)
        self.assertEqual(started.returncode, 0, started.stderr)
        passwd = started.stdout.splitlines()
        self.assertEqual(passwd[0], "second:x:0:0::/:/bin/sh")
        self.assertIn(f":x:{os.getuid()}:{os.getgid()}::", passwd[1])


class TestBoxEnvironment(unittest.TestCase):
    def test_nested_box_depth(self) -> None:
        for previous, depth in (
            ("dev", 2),
            ("dev:9", 10),
            ("dev:other:19", 20),
            ("dev:", 2),
            ("dev:0", 2),
            ("dev:01", 2),
            ("dev:-3", 2),
            ("dev:abc", 2),
            ("dev:٣", 2),
        ):
            with (
                self.subTest(previous=previous),
                unittest.mock.patch.dict(os.environ, {"TINE_IN_BOX": "1", "TINE_BOX": previous}, clear=True),
            ):
                environment = sandbox._box("next")
                self.assertEqual(environment["TINE_BOX"], f"next:{depth}")
                self.assertEqual(environment["SHELL_PROMPT_PREFIX"], f"(next:{depth})")


class TestShellLookup(unittest.TestCase):
    def test_path_order_and_executable_files(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            root = Path(directory)
            entries = [root / name for name in ("directory", "nonexecutable", "first", "second")]
            for entry in entries:
                entry.mkdir()
            (entries[0] / "shell").mkdir()
            for entry in entries[1:]:
                (entry / "shell").touch(mode=0o755 if entry.name != "nonexecutable" else 0o644)
            path = os.pathsep.join(map(str, entries))

            self.assertEqual(sandbox._which("shell", path), str(entries[2] / "shell"))
            self.assertEqual(sandbox._which(str(entries[3] / "shell"), ""), str(entries[3] / "shell"))
            self.assertIsNone(sandbox._which(str(entries[3] / "shell") + "/", path))
            self.assertIsNone(sandbox._which("missing", path))
            self.assertIsNone(sandbox._which("shell", ""))

    def test_empty_path_components_search_cwd(self) -> None:
        with _project() as (project, _):
            (project / "shell").touch(mode=0o755)

            self.assertEqual(sandbox._which("shell", ":/nonexistent"), "shell")
            self.assertEqual(sandbox._which("./shell", "/nonexistent"), "./shell")


class TestStartup(unittest.TestCase):
    def test_legacy_python_imports_the_eager_annotation_names(self) -> None:
        for minor in (12, 13):
            with self.subTest(minor=minor):
                # Exercise the compatibility branch with the pinned interpreter, then force annotations.
                subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "sys.version_info = (3, int(sys.argv[2])); import sandbox, isolation, typing; "
                        "assert sandbox._fail.__annotations__['return'] is typing.NoReturn; "
                        "assert sandbox.main.__annotations__['return'] is typing.NoReturn; "
                        "assert isolation._Mount.__enter__.__annotations__['return'] is typing.Self",
                        str(Path(sandbox.__file__).parent),
                        str(minor),
                    ],
                    check=True,
                )

    def test_launch_does_not_import_heavy_helpers(self) -> None:
        with _project() as (_, tools):
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import sandbox; "
                    "sandbox._launch(sandbox._parse(['--tools', sys.argv[2], '--', 'true']), "
                    "sandbox.open_layers((sys.argv[2],))); "
                    "print('\\n'.join(sys.modules))",
                    str(Path(sandbox.__file__).parent),
                    str(tools),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        unwanted = {"annotationlib", "argparse", "dataclasses", "inspect", "pathlib", "shutil", "util"}
        if sys.version_info >= (3, 14):  # noqa: UP036
            unwanted.add("typing")
        self.assertFalse(unwanted & set(process.stdout.splitlines()))


class TestInteractiveShell(unittest.TestCase):
    def test_shell_from_the_environment_wins(self) -> None:
        with unittest.mock.patch.object(
            sandbox,
            "_which",
            return_value="/box/bin/zsh",
        ) as which:
            shell = sandbox._interactive_shell({"PATH": "/box/bin", "SHELL": "zsh"})

        self.assertEqual(shell, "/box/bin/zsh")
        which.assert_called_once_with("zsh", "/box/bin")

    def test_bash_is_the_fallback(self) -> None:
        environment = {
            "PATH": "/box/bin",
            "SHELL": "/bin/fish",
            "STARSHIP_SHELL": "fish",
            "TINE_BOX": "systemd",
        }
        with unittest.mock.patch.object(
            sandbox,
            "_which",
            side_effect=[None, "/box/bin/bash"],
        ) as which:
            shell = sandbox._interactive_shell(environment)

        self.assertEqual(shell, "/box/bin/bash")
        self.assertEqual(environment["SHELL"], "/box/bin/bash")
        self.assertEqual(environment["SHELL_PROMPT_PREFIX"], "(systemd)")
        self.assertNotIn("STARSHIP_SHELL", environment)
        self.assertEqual(
            which.call_args_list,
            [
                unittest.mock.call("/bin/fish", "/box/bin"),
                unittest.mock.call("bash", "/box/bin"),
            ],
        )

    def test_no_installed_shell_is_an_error(self) -> None:
        with (
            unittest.mock.patch.object(sandbox, "_which", return_value=None),
            self.assertRaisesRegex(SystemExit, "no shell installed in box"),
        ):
            sandbox._interactive_shell({"PATH": "/box/bin"})

    def test_shell_is_chosen_after_entering_the_box(self) -> None:
        launch = sandbox.Launch(filesystems=(), chdir=None, command=(), environment={})
        entered = False

        def enter(_filesystems: tuple[Filesystem, ...], _chdir: str | None, **_suppress: bool) -> None:
            nonlocal entered
            entered = True

        def shell(_environment: dict[str, str]) -> str:
            self.assertTrue(entered)
            return "/bin/bash"

        with (
            unittest.mock.patch.object(
                sandbox, "_parse", return_value=sandbox._parse(["--tools=/tools", "--", "true"])
            ),
            unittest.mock.patch.object(sandbox, "_launch", return_value=launch),
            unittest.mock.patch.object(sandbox, "unshare_sandbox"),
            unittest.mock.patch.object(
                sandbox, "open_layers", side_effect=lambda _: os.open("/", os.O_PATH)
            ),
            unittest.mock.patch.object(sandbox, "enter_sandbox", side_effect=enter),
            unittest.mock.patch.object(sandbox, "_interactive_shell", side_effect=shell),
            unittest.mock.patch.object(os, "execvpe") as execute,
            self.assertRaises(SystemExit) as raised,
        ):
            sandbox.main([])

        self.assertEqual(raised.exception.code, 127)
        execute.assert_called_once_with("/bin/bash", ("/bin/bash",), {})


if __name__ == "__main__":
    unittest.main()
