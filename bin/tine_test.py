# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the wrapper's git query, buckconfig reading and argument handling.

    buck test tine//bin:test

Each test builds a real throwaway repository or checkout; the wrapper's git calls run for real.
"""

import collections.abc
import contextlib
import hashlib
import io
import json
import os
import re
import runpy
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path
from typing import cast, override

import cache_shim
import tine

TOOL_PATH = Path(__file__).parent / "tine"


class TestCommand(unittest.TestCase):
    def test_loads_the_module_in_isolated_mode(self) -> None:
        proc = subprocess.run([str(TOOL_PATH), "--help"], check=True, text=True, stdout=subprocess.PIPE)
        self.assertEqual(proc.stdout, tine.USAGE)

    def test_an_old_python_is_rejected_before_the_module_is_loaded(self) -> None:
        with (
            unittest.mock.patch.object(sys, "version_info", (3, 11)),
            unittest.mock.patch.object(sys, "argv", [str(TOOL_PATH)]),
            self.assertRaisesRegex(SystemExit, "Python 3.12 or newer is required"),
        ):
            runpy.run_path(str(TOOL_PATH), run_name="__main__")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def mount_config(root: Path) -> Path:
    """The machine-local mount file, in a state directory `tine mount` would have created."""
    path = root / tine.MOUNT_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def declare(root: Path, mounts: dict[str, str]) -> None:
    """Write the machine-local declarations `tine mount` would have left."""
    mount_config(root).write_text(tine.render_mounts(mounts))


def configure_cells(root: Path, cell: str) -> Path:
    path = root / tine.CONFIG
    path.write_text(f'[buckconfig.cells]\nroot = "."\ntine = {json.dumps(cell)}\n')
    return path


def refresh_project(root: Path) -> None:
    tine.refresh_project_buckconfig(root, overrides(root))


def overrides(root: Path) -> dict[str, dict[str, str]]:
    return tine.buckconfig_overrides(tine.project_settings(root), root / tine.CONFIG)


def wrapper_command(cell: Path) -> Path:
    """Stand in for a checkout's executable when handover is mocked."""
    command = cell / tine.COMMAND
    command.parent.mkdir(parents=True, exist_ok=True)
    command.touch()
    return command


def read_generated_buckconfig(path: Path) -> dict[str, dict[str, str]]:
    """What the generated block in a config says, for asserting on it."""
    config: dict[str, dict[str, str]] = {}
    tine._parse_buckconfig_lines(
        tine._logical_buckconfig_lines(tine._split_generated_block(path)[1]), config, "", path.parent
    )
    return config


def scratch(case: unittest.TestCase, prefix: str = "tine-test.") -> Path:
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def pins(cell: Path, spec: object | None = None) -> Path:
    """Write the `tine.lock.json` of a cell. It pins a usable Buck2 unless the caller passes `spec`."""
    path = cell / tine.PINS
    path.parent.mkdir(parents=True, exist_ok=True)
    default = {
        "buck2": {
            "repository": "example/buck2",
            "release": "2026-01-01",
            "platforms": {tine._host_platform(): {"artifact": "buck2.zst", "sha256": "a" * 64}},
        }
    }
    path.write_text(json.dumps(default if spec is None else spec))
    return path


def isolate_git(case: unittest.TestCase) -> None:
    """Keep the developer's own git configuration out of it; `tag.gpgSign` alone breaks the suite."""
    patched = unittest.mock.patch.dict(
        os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    )
    patched.start()
    case.addCleanup(patched.stop)


class RepositoryTestCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        isolate_git(self)
        self.repo = scratch(self)
        git("init", "--quiet", "--initial-branch=main", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "user.email", "test@example.com", cwd=self.repo)

    def commit(self, message: str = "commit") -> str:
        git("commit", "--allow-empty", "--quiet", "--message", message, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def dirty(self) -> None:
        (self.repo / "uncommitted").write_text("wip")


class TestVersionComponents(RepositoryTestCase):
    def test_release(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "1.2.3", "count": 0, "height": 1, "commit": commit},
        )

    def test_snapshot_counts_commits(self) -> None:
        # The whole commit, never an abbreviation: how much of it fits is the renderer's question.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        commit = self.commit("third")
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "1.2.3", "count": 2, "height": 3, "commit": commit},
        )

    def test_no_tag(self) -> None:
        commit = self.commit()
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit},
        )

    def test_v_prefixed_word_is_not_a_version_tag(self) -> None:
        # `v*` would match this and derive the version `endor-drop-2024` from it.
        commit = self.commit()
        git("tag", "vendor-drop-2024", cwd=self.repo)
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit},
        )

    def test_latest_tag_wins(self) -> None:
        self.commit()
        git("tag", "v1.0.0", cwd=self.repo)
        commit = self.commit("second")
        git("tag", "v2.0.0", cwd=self.repo)
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "2.0.0", "count": 0, "height": 2, "commit": commit},
        )

    def test_tag_sharing_a_branch_name(self) -> None:
        # An unqualified revision would be ambiguous, which git resolves with a warning.
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("branch", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "1.2.3", "count": 1, "height": 2, "commit": commit},
        )

    def test_uncommitted_work_sets_the_dirty_bit(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        commit = self.commit("second")
        self.dirty()
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "1.2.3", "count": 1, "height": 2, "commit": commit, "dirty": 1},
        )

    def test_the_bit_does_not_move_as_the_tree_keeps_changing(self) -> None:
        # It says that a checkout holds more than its commit, and nothing about how much: the
        # version it renders has to stand still while the work on top of that commit goes on.
        commit = self.commit()
        self.dirty()
        first = tine.version_components(self.repo)
        (self.repo / "uncommitted").write_text("more")
        (self.repo / "another").write_text("wip")
        git("add", "another", cwd=self.repo)
        self.assertEqual(tine.version_components(self.repo), first)
        self.assertEqual(
            first,
            {"base": "0.0.0", "count": 1, "height": 1, "commit": commit, "dirty": 1},
        )

    def test_untracked_files_are_dirty_whatever_git_is_configured_to_show(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        git("config", "status.showUntrackedFiles", "no", cwd=self.repo)
        self.dirty()
        self.assertEqual(
            tine.version_components(self.repo),
            {"base": "1.2.3", "count": 0, "height": 1, "commit": commit, "dirty": 1},
        )

    def test_height_ignores_tags(self) -> None:
        # A version whose base a tag never named counts in the height, so a new tag must not move it.
        self.commit()
        self.commit("second")
        before = tine.version_components(self.repo)
        git("tag", "v9.9.9", cwd=self.repo)
        after = tine.version_components(self.repo)
        assert isinstance(before, dict) and isinstance(after, dict)
        self.assertEqual((after["height"], after["count"]), (before["height"], 0))

    def test_ignores_the_repository_git_was_pointed_at(self) -> None:
        # `git -C` would leave GIT_DIR alone, so a run from a hook would describe the hook's repo.
        other = scratch(self, "tine-test-other.")
        git("init", "--quiet", cwd=other)
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(
                tine.version_components(self.repo),
                {"base": "1.2.3", "count": 0, "height": 1, "commit": commit},
            )


class TestNoVersionComponents(RepositoryTestCase):
    """A checkout that cannot answer says why, rather than taking the command down with it."""

    def test_not_a_repository(self) -> None:
        self.assertEqual(tine.version_components(scratch(self)), "not a git checkout")

    def test_repository_without_commits(self) -> None:
        self.assertIn("git rev-parse HEAD", str(tine.version_components(self.repo)))

    def test_shallow_checkout(self) -> None:
        self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.commit("second")
        clone = scratch(self, "tine-test-clone.") / "clone"
        git("clone", "--quiet", "--depth", "1", f"file://{self.repo}", str(clone), cwd=self.repo)
        self.assertEqual(tine.version_components(clone), "shallow git checkout")

    def test_tag_that_is_not_a_version(self) -> None:
        self.commit()
        git("tag", "v1.2.3+dirty", cwd=self.repo)
        self.assertEqual(tine.version_components(self.repo), "tag 'v1.2.3+dirty' is not a valid version")

    def test_reason_is_recorded_in_the_generated_config(self) -> None:
        self.assertIn(
            "# no version components: not a git checkout", tine.render_local_config_block(scratch(self), [])
        )


class TestRenderLocalConfigBlock(RepositoryTestCase):
    def test_writes_every_component_it_has(self) -> None:
        commit = self.commit()
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(
            tine.render_local_config_block(self.repo, []),
            [
                tine.BLOCK_BEGIN,
                tine.BLOCK_NOTE,
                "",
                "[tine]",
                "version-base = 1.2.3",
                "version-count = 0",
                "version-height = 1",
                f"version-commit = {commit}",
                tine.BLOCK_END,
            ],
        )

    def test_the_dirty_bit_appears_only_for_an_uncommitted_tree(self) -> None:
        self.commit()
        self.assertNotIn("version-dirty = 1", tine.render_local_config_block(self.repo, []))
        self.dirty()
        self.assertIn("version-dirty = 1", tine.render_local_config_block(self.repo, []))

    def test_every_line_it_writes_is_buckconfig(self) -> None:
        # A checkout without commits fails `rev-parse` with three lines of git advice; all but the
        # first would land in the file as configuration, and Buck would refuse to parse it.
        (self.repo / ".buckconfig").write_text("")
        tine.refresh_local_buckconfig(self.repo, [])
        for line in (self.repo / tine.LOCAL).read_text().splitlines():
            self.assertTrue(not line or line.startswith(("#", "[", "<")) or "=" in line)
        self.assertEqual(tine.read_project_buckconfig(self.repo), {"": {}})

    def test_writes_mount_ignores_after_the_version_components(self) -> None:
        self.commit()
        lines = tine.render_local_config_block(
            self.repo, ["**/.git", "packages/demo/BUCK", "packages/demo/**/BUCK"]
        )
        self.assertEqual(
            lines[-6:],
            [
                "",
                "[project]",
                "ignore = **/.git, \\",
                "    packages/demo/BUCK, \\",
                "    packages/demo/**/BUCK",
                tine.BLOCK_END,
            ],
        )

    def test_a_single_mount_ignore_needs_no_continuation(self) -> None:
        self.commit()
        self.assertIn(
            "ignore = packages/demo/BUCK", tine.render_local_config_block(self.repo, ["packages/demo/BUCK"])
        )

    def test_mount_ignores_read_back_as_one_entry_the_project_does_not_own(self) -> None:
        (self.repo / ".buckconfig").write_text("[project]\nignore = .git\n")
        tine.refresh_local_buckconfig(self.repo, [".git", "packages/demo/BUCK", "packages/demo/**/BUCK"])
        self.assertEqual(
            read_generated_buckconfig(self.repo / tine.LOCAL)["project"][tine.PROJECT_IGNORE],
            ".git, packages/demo/BUCK, packages/demo/**/BUCK",
        )
        self.assertEqual(tine.read_project_buckconfig(self.repo)["project"][tine.PROJECT_IGNORE], ".git")

    def cache(self, authority: list[str] | None = None) -> cache_shim.CacheSettings:
        """Cache settings trusting the given authority, otherwise allowing unsigned."""

        trust: dict[str, object] = {"authority": authority} if authority else {"unsigned": True}
        table = {"read_url": "https://cache.example"} | trust
        cache = cache_shim.settings({"cache": table}, self.repo, tine.SETTINGS)
        assert cache is not None
        return cache

    def test_a_configured_cache_is_where_buck_reads_its_address(self) -> None:
        self.commit()
        cache = self.cache()
        lines = tine.render_local_config_block(self.repo, [], cache)
        self.assertIn(f"[{tine.RE_CLIENT}]", lines)
        self.assertIn(f"address = 127.0.0.1:{cache.port}", lines)
        # Buck defaults this to true, and the shim serves plaintext on the loopback.
        self.assertIn("tls = false", lines)
        self.assertNotIn(f"[{tine.RE_CLIENT}]", tine.render_local_config_block(self.repo, []))

    def test_cache_uploads_flag(self) -> None:
        # not present without a cache setting
        self.assertNotIn(tine.CACHE_UPLOADS, "".join(tine.render_local_config_block(self.repo, [])))
        # writer can upload
        self.assertIn(
            f"{tine.CACHE_UPLOADS} = true", tine.render_local_config_block(self.repo, [], self.cache())
        )
        # reader cannot
        reader = self.cache(authority=["ca.pem"])
        self.assertIn(f"{tine.CACHE_UPLOADS} = false", tine.render_local_config_block(self.repo, [], reader))

    def test_the_address_reads_back_out_of_the_block(self) -> None:
        self.commit()
        cache = self.cache()
        tine.refresh_local_buckconfig(self.repo, [], cache)
        generated = read_generated_buckconfig(self.repo / tine.LOCAL)
        self.assertEqual(generated[tine.RE_CLIENT]["address"], f"127.0.0.1:{cache.port}")
        # The project's own view of its configuration excludes what tine wrote there.
        self.assertNotIn(tine.RE_CLIENT, tine.read_project_buckconfig(self.repo))


class TestMergeGeneratedConfigBlock(unittest.TestCase):
    """The block is this command's; the rest of the file stays the developer's."""

    def local(self, content: str | None = None) -> Path:
        path = scratch(self) / tine.LOCAL
        if content is not None:
            path.write_text(content)
        return path

    def test_a_file_that_does_not_exist_yet(self) -> None:
        self.assertEqual(tine.merge_generated_config_block(self.local(), ["one", "two"]), "one\ntwo\n")

    def test_content_that_was_there_first_is_kept_after_the_block(self) -> None:
        path = self.local("[buck2]\nmaterializations = all\n")
        self.assertEqual(
            tine.merge_generated_config_block(path, [tine.BLOCK_BEGIN, tine.BLOCK_END]),
            f"{tine.BLOCK_BEGIN}\n{tine.BLOCK_END}\n\n[buck2]\nmaterializations = all\n",
        )

    def test_an_earlier_block_is_replaced_and_not_repeated(self) -> None:
        path = self.local(
            f"{tine.BLOCK_BEGIN}\n[tine]\nversion-base = 1.0.0\n{tine.BLOCK_END}\n\n[demo]\nkey = mine\n"
        )
        merged = tine.merge_generated_config_block(
            path, [tine.BLOCK_BEGIN, "[tine]", "version-base = 2.0.0", tine.BLOCK_END]
        )
        self.assertEqual(merged.count(tine.BLOCK_BEGIN), 1)
        self.assertNotIn("1.0.0", merged)
        self.assertIn("[demo]\nkey = mine\n", merged)

    def test_a_block_missing_its_end_is_reported(self) -> None:
        path = self.local(f"{tine.BLOCK_BEGIN}\n[tine]\nversion-base = 1.0.0\n")
        with self.assertRaisesRegex(SystemExit, "has no .* line"):
            tine.merge_generated_config_block(path, [tine.BLOCK_BEGIN, tine.BLOCK_END])

    def test_a_block_missing_its_beginning_is_reported(self) -> None:
        # Keeping it would leave the old block after the new one, where it wins for good.
        path = self.local(f"[tine]\nversion-base = 1.0.0\n{tine.BLOCK_END}\n")
        with self.assertRaisesRegex(SystemExit, "ends without beginning"):
            tine.merge_generated_config_block(path, [tine.BLOCK_BEGIN, tine.BLOCK_END])

    def test_the_block_is_not_read_back_as_configuration(self) -> None:
        # Reading our own `disabled` back would make the cell look unexpanded and drop the entry.
        root = scratch(self)
        (root / ".buckconfig").write_text("[external_cells]\nsub = git\n")
        (root / tine.LOCAL).write_text(
            f"{tine.BLOCK_BEGIN}\n[external_cells]\nsub = disabled\n{tine.BLOCK_END}\n"
        )
        self.assertEqual(tine.read_project_buckconfig(root)["external_cells"], {"sub": "git"})


class TestReadLines(unittest.TestCase):
    def test_only_newlines_split_config_lines(self) -> None:
        path = scratch(self) / ".buckconfig"
        path.write_bytes(b"[custom]\r\nvalue = a\rb\fc\n")
        self.assertEqual(tine.read_lines(path), ["[custom]\r", "value = a\rb\fc"])

    def test_a_file_that_is_not_text(self) -> None:
        # Buck2 answers the same file with a parse error naming it; a traceback would not.
        path = scratch(self) / ".buckconfig"
        path.write_bytes(b"[cells]\nroot = \xff\n")
        with self.assertRaisesRegex(SystemExit, "cannot read"):
            tine.read_lines(path)


class TestReadToml(unittest.TestCase):
    def test_an_absent_file_is_empty_configuration(self) -> None:
        self.assertEqual(tine.read_toml(scratch(self) / tine.CONFIG), {})

    def test_a_malformed_file_names_itself(self) -> None:
        path = scratch(self) / tine.CONFIG
        path.write_text("[commands\n")
        with self.assertRaisesRegex(SystemExit, rf"cannot parse {re.escape(str(path))}"):
            tine.read_toml(path)

    def test_project_settings_reject_unknown_sections(self) -> None:
        root = scratch(self)
        (root / tine.CONFIG).write_text("[command.check]\nsteps = []\n")
        with self.assertRaisesRegex(SystemExit, "unknown sections: command"):
            tine.project_settings(root)


class TestLocalSettings(unittest.TestCase):
    def settings(self, committed: str = "", local: str = "") -> dict[str, dict[str, object]]:
        root = scratch(self)
        if committed:
            (root / tine.CONFIG).write_text(committed)
        if local:
            (root / tine.LOCAL_SETTINGS).write_text(local)
        return tine.project_settings(root)

    def test_empty(self) -> None:
        self.assertEqual(self.settings(), {})

    def test_local_only(self) -> None:
        settings = self.settings(local='[buck2]\nrelease = "local"\n')
        self.assertEqual(settings, {"buck2": {"release": "local"}})

    def test_override_one_key(self) -> None:
        settings = self.settings(
            committed='[buck2]\nrepository = "committed/buck2"\nrelease = "committed"\n',
            local='[buck2]\nrelease = "local"\n',
        )
        self.assertEqual(settings["buck2"], {"repository": "committed/buck2", "release": "local"})

    def test_local_new_section(self) -> None:
        settings = self.settings(
            committed='[commands.check]\nsteps = [["buck", "build"]]\n',
            local='[buck2]\nrelease = "local"\n',
        )
        self.assertEqual(sorted(settings), ["buck2", "commands"])

    def test_unknown_section(self) -> None:
        root = scratch(self)
        (root / tine.LOCAL_SETTINGS).write_text("[invalid]\nkey = 1\n")
        expected = re.escape(str(root / tine.LOCAL_SETTINGS))
        with self.assertRaisesRegex(SystemExit, rf"{expected} has unknown sections: invalid"):
            tine.project_settings(root)

    def test_local_unknown_key(self) -> None:
        root = scratch(self)
        (root / tine.LOCAL_SETTINGS).write_text('[buck2]\nbogus = "local"\n')
        with self.assertRaisesRegex(SystemExit, "unsupported keys: bogus"):
            tine.buck2_pin_overrides(tine.project_settings(root), tine._host_platform())

    def test_section_is_not_a_table(self) -> None:
        root = scratch(self)
        (root / tine.LOCAL_SETTINGS).write_text("buck2 = 1\n")
        expected = re.escape(str(root / tine.LOCAL_SETTINGS))
        with self.assertRaisesRegex(SystemExit, rf"\[buck2\] in {expected} must be a table"):
            tine.project_settings(root)

    def test_local_command_is_validated(self) -> None:
        root = scratch(self)
        (root / tine.LOCAL_SETTINGS).write_text('[commands.check]\nsteps = [["make"]]\n')
        with self.assertRaisesRegex(SystemExit, "must start with buck, not make"):
            tine.validate_commands(tine.project_settings(root).get("commands", {}))

    def test_cache_keys_describing_the_machine_are_refused_when_committed(self) -> None:
        """A branch must not move the store or weaken trust for whoever checks it out."""
        for key in sorted(cache_shim.LOCAL_ONLY):
            committed = f'[cache]\nread_url = "https://cache.example"\n{key} = true\n'
            with self.subTest(key=key), self.assertRaisesRegex(SystemExit, f"sets {key}, which belong in"):
                self.settings(committed=committed)
        local = '[cache]\ndir = "store"\ns3_insecure = true\nunsigned = true\n'
        settings = self.settings(committed='[cache]\nread_url = "https://cache.example"\n', local=local)
        self.assertEqual(sorted(settings["cache"]), ["dir", "read_url", "s3_insecure", "unsigned"])


class TestBuckconfigOverrides(unittest.TestCase):
    def test_overrides_are_separate_from_the_binary_pin(self) -> None:
        root = scratch(self)
        (root / tine.CONFIG).write_text(
            '[buck2]\nrelease = "pin"\n[buckconfig.buck2]\nmaterializations = "all"\n'
        )
        self.assertEqual(overrides(root), {"buck2": {"materializations": "all"}})
        self.assertEqual(tine.project_settings(root)["buck2"], {"release": "pin"})

    def test_invalid_overrides_are_rejected_before_writing_config(self) -> None:
        root = scratch(self)
        for content, message in (
            ('buckconfig = "bad"', "must be a table"),
            ('[buckconfig]\nbuck2 = "bad"', "must be a table"),
            ("[buckconfig.buck2]\nsqlite_materializer_state = true", "must contain only strings"),
            ("[buckconfig.buck2]\nthreads = 2", "must contain only strings"),
            ('[buckconfig.project]\nignore = ["a"]', "must contain only strings"),
            ('[buckconfig."bad]section"]\nkey = "value"', "invalid section name"),
            ('[buckconfig.project]\n"bad=key" = "value"', "cannot be written"),
            ('[buckconfig.project]\nignore = " leading"', "cannot be written"),
            ('[buckconfig.project]\nignore = "line\\nbreak"', "cannot be written"),
            ('[buckconfig.project]\nignore = "trailing\\\\"', "cannot be written"),
        ):
            with self.subTest(content=content):
                (root / tine.CONFIG).write_text(content)
                with self.assertRaisesRegex(SystemExit, message):
                    overrides(root)
                self.assertFalse((root / ".buckconfig").exists())


class TestParseBuckconfig(unittest.TestCase):
    def parse(self, files: dict[str, str]) -> dict[str, dict[str, str]]:
        root = scratch(self)
        for name, content in files.items():
            (root / name).write_text(content)
        return tine.read_project_buckconfig(root)

    def test_sections_and_entries(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nroot = .\n# comment\nsub = sub\n"})
        self.assertEqual(config["cells"], {"root": ".", "sub": "sub"})

    def test_section_marker_with_a_trailing_comment(self) -> None:
        # Buck2 accepts these, and losing the marker would file the entries under the last section.
        config = self.parse(
            {".buckconfig": "[cells]\nroot = .\n[external_cells] # @oss-enable\nsub = git\n"}
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})
        self.assertEqual(config["cells"], {"root": "."})

    def test_continuation_lines(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nsub = \\\n    some/deep/path\n"})
        self.assertEqual(config["cells"]["sub"], "some/deep/path")

    def test_values_keep_what_is_not_a_comment(self) -> None:
        config = self.parse({".buckconfig": "[project]\nignore = a, b # c\nempty =\n"})
        self.assertEqual(config["project"], {"ignore": "a, b # c", "empty": ""})

    def test_includes_are_followed(self) -> None:
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n<file:cells.bcfg>\n",
                "cells.bcfg": "[external_cells]\nsub = git\n",
            }
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})

    def test_an_include_carrying_a_comment_is_still_an_include(self) -> None:
        # Buck2 reads the include off the front of the line, as `# @oss-enable` markers rely on.
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n<file:cells.bcfg> # @oss-enable\n",
                "cells.bcfg": "[external_cells]\nsub = git\n",
            }
        )
        self.assertEqual(config["external_cells"], {"sub": "git"})

    def test_what_follows_an_include_continues_in_the_included_file(self) -> None:
        # Buck2 does not go back to the section the including file was in.
        config = self.parse(
            {
                ".buckconfig": "[demo]\na = 1\n<file:sub.bcfg>\nafter = 2\n",
                "sub.bcfg": "[other]\nx = 1\n",
            }
        )
        self.assertEqual(config["demo"], {"a": "1"})
        self.assertEqual(config["other"], {"x": "1", "after": "2"})

    def test_a_missing_optional_include_is_not_an_error(self) -> None:
        self.assertEqual(
            self.parse({".buckconfig": "[cells]\nsub = sub\n<?file:absent.bcfg>\n"})["cells"], {"sub": "sub"}
        )

    def test_a_line_with_no_key_is_not_an_entry(self) -> None:
        self.assertEqual(
            self.parse({".buckconfig": "[cells]\n= orphan\nsub = sub\n"})["cells"], {"sub": "sub"}
        )

    def test_a_value_may_contain_the_separator(self) -> None:
        config = self.parse({".buckconfig": "[build]\nflags = -c a=b\n"})
        self.assertEqual(config["build"]["flags"], "-c a=b")

    def test_an_unterminated_section_marker_starts_no_section(self) -> None:
        config = self.parse({".buckconfig": "[cells]\nsub = sub\n[broken\nkey = value\n"})
        self.assertEqual(config["cells"], {"sub": "sub", "key": "value"})

    def test_the_lowest_layer_is_read_too(self) -> None:
        # Buck2 reads `.buckconfig.d` whole, below `.buckconfig`.
        root = scratch(self)
        (root / ".buckconfig.d" / "nested").mkdir(parents=True)
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / ".buckconfig.d" / "10-project.bcfg").write_text("[project]\nname = example\n")
        (root / ".buckconfig.d" / "nested" / "20-cells.bcfg").write_text("[cells]\nsub = sub\n")
        config = tine.read_project_buckconfig(root)
        self.assertEqual(config["project"]["name"], "example")
        self.assertEqual(config["cells"], {"root": ".", "sub": "sub"})

    def test_a_symlink_in_the_lowest_layer_is_not_read(self) -> None:
        # Buck2 lists that directory with `lstat` and skips one, so reading it would configure the
        # build with something Buck2 never sees.
        root = scratch(self)
        (root / ".buckconfig.d").mkdir()
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / "elsewhere.bcfg").write_text("[cells]\nsub = sub\n")
        (root / ".buckconfig.d" / "10-cells.bcfg").symlink_to(root / "elsewhere.bcfg")
        self.assertEqual(tine.read_project_buckconfig(root)["cells"], {"root": "."})

    def test_local_overrides_win(self) -> None:
        config = self.parse(
            {
                ".buckconfig": "[cells]\nsub = sub\n",
                ".buckconfig.local": "[cells]\nsub = elsewhere\n",
            }
        )
        self.assertEqual(config["cells"]["sub"], "elsewhere")

    def test_pending_toml_cells_do_not_override_local_cells(self) -> None:
        root = scratch(self)
        (root / ".buckconfig").write_text("[cells]\ntine = old\n")
        self.assertEqual(
            tine.read_project_buckconfig(root, {"cells": {"tine": "new"}})["cells"]["tine"], "new"
        )
        (root / tine.LOCAL).write_text("[cells]\ntine = local\n")
        self.assertEqual(
            tine.read_project_buckconfig(root, {"cells": {"tine": "new"}})["cells"]["tine"], "local"
        )


class MountTestCase(unittest.TestCase):
    """Test mounts from `self.source` into the project at `self.root`."""

    @override
    def setUp(self) -> None:
        # Commands resolve the project root from the working directory.
        self.root = scratch(self, "tine-test-project.").resolve()
        (self.root / ".buckconfig").write_text(
            "[cells]\nroot = .\nsub = sub\nother = other\ninner = sub/inner\n"
        )
        (self.root / "sub").mkdir()
        self.source = scratch(self, "tine-test-source.").resolve()

    def mount(self, *arguments: str) -> str:
        """Run `tine mount` and return its stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tine.mount_command(self.root, list(arguments))
        return stderr.getvalue()

    def declared(self) -> dict[str, str]:
        return tine.declared_mounts(self.root)

    def local(self) -> str:
        return (self.root / tine.MOUNT_CONFIG).read_text()


class TestMountAdd(MountTestCase):
    def test_add_records_an_external_source(self) -> None:
        report = self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(report, f"tine: sub is built from {self.source}\n")
        self.assertEqual(self.declared(), {"sub": str(self.source)})
        self.assertIn(f'[{tine.MOUNTS}]\n"sub" = "{self.source}"', self.local())

    def test_target_is_project_relative(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        self.mount("add", "sub", str(self.source))
        self.assertEqual(self.declared(), {"sub": str(self.source)})

    def test_add_replaces_an_existing_mount(self) -> None:
        other = scratch(self).resolve()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.mount("add", str(self.root / "sub"), str(other))
        self.assertEqual(self.declared(), {"sub": str(other)})

    def test_add_repairs_a_missing_source_without_querying_the_graph(self) -> None:
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        declare(self.root, {"sub": "/nonexistent/source"})
        with unittest.mock.patch.object(tine, "graph_mount_targets") as query:
            self.mount("add", str(self.root / "sub"), str(self.source))
        query.assert_not_called()
        self.assertEqual(self.declared(), {"sub": str(self.source)})

    def test_add_does_not_validate_unrelated_stale_mounts(self) -> None:
        (self.root / "other").mkdir()
        declare(self.root, {"other": "/nonexistent/source"})
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(
            tine.local_mounts(self.root), {"other": "/nonexistent/source", "sub": str(self.source)}
        )

    def test_add_does_not_rewrite_buckconfig_local(self) -> None:
        local = self.root / tine.LOCAL
        local.write_text("[build]\nthreads = 4\n")
        before = local.read_bytes()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.assertEqual(local.read_bytes(), before)

    def test_target_cannot_hide_tines_private_config(self) -> None:
        (self.root / tine.HOME / "nested").mkdir(parents=True)
        for target in (self.root / tine.HOME, self.root / tine.HOME / "nested"):
            with self.subTest(target=target), self.assertRaisesRegex(SystemExit, "reserved for tine"):
                self.mount("add", str(target), str(self.source))

    def test_target_cannot_contain_whitespace_or_commas(self) -> None:
        for name in ("sub dir", "sub,dir", "sub\tdir"):
            (self.root / name).mkdir()
            with self.subTest(target=name), self.assertRaisesRegex(SystemExit, "whitespace or commas"):
                self.mount("add", str(self.root / name), str(self.source))

    def test_add_rejects_overlapping_mounts(self) -> None:
        (self.root / "sub" / "inner").mkdir()
        for existing, target in (("sub", "sub/inner"), ("sub/inner", "sub")):
            with self.subTest(existing=existing, target=target):
                self.mount("add", str(self.root / existing), str(self.source))
                with self.assertRaisesRegex(SystemExit, "overlaps mount"):
                    self.mount("add", str(self.root / target), str(self.source))
                self.assertEqual(self.declared(), {existing: str(self.source)})
                self.mount("remove", str(self.root / existing))

    def test_target_must_be_inside_the_project(self) -> None:
        with self.assertRaisesRegex(SystemExit, "outside the project"):
            self.mount("add", str(self.source), str(self.source))

    def test_target_cannot_be_the_project_root(self) -> None:
        with self.assertRaisesRegex(SystemExit, "project root"):
            self.mount("add", str(self.root), str(self.source))

    def test_unknown_target_is_rejected(self) -> None:
        with (
            unittest.mock.patch.object(tine, "graph_mount_targets", return_value=set()),
            self.assertRaisesRegex(SystemExit, "not a valid target.*tine mount list"),
        ):
            self.mount("add", str(self.root / "missing"), str(self.source))
        self.assertFalse((self.root / "missing").exists())

    def test_declared_checkout_target_is_created(self) -> None:
        with unittest.mock.patch.object(
            tine,
            "graph_mount_targets",
            return_value={"missing"},
        ):
            self.mount("add", str(self.root / "missing"), str(self.source))
        self.assertTrue((self.root / "missing").is_dir())
        self.assertEqual(self.declared(), {"missing": str(self.source)})

    def test_source_must_be_a_directory(self) -> None:
        with self.assertRaisesRegex(SystemExit, "is not a directory"):
            self.mount("add", str(self.root / "sub"), str(self.source / "missing"))

    def test_toml_quotes_a_source_buckconfig_could_not_represent(self) -> None:
        awkward = self.source / "trailing\\"
        awkward.mkdir()
        self.mount("add", str(self.root / "sub"), str(awkward))
        self.assertEqual(self.declared(), {"sub": str(awkward)})

    def test_toml_quotes_targets_buckconfig_could_not_represent(self) -> None:
        for name in ("a=b", "[weird]", "<weird>"):
            (self.root / name).mkdir()
            with unittest.mock.patch.object(tine, "graph_mount_targets", return_value={name}):
                self.mount("add", str(self.root / name), str(self.source))
            self.assertEqual(self.declared(), {name: str(self.source)})
            self.mount("remove", str(self.root / name))

    def test_target_cannot_be_a_symlink(self) -> None:
        # The kernel would apply the bind mount to the symlink's destination.
        (self.root / "link").symlink_to(self.root / "sub")
        with self.assertRaisesRegex(SystemExit, "target is a symlink"):
            self.mount("add", str(self.root / "link"), str(self.source))

    def test_source_must_be_outside_the_project(self) -> None:
        (self.root / "inside").mkdir()
        with self.assertRaisesRegex(SystemExit, "must be outside the project"):
            self.mount("add", str(self.root / "sub"), str(self.root / "inside"))


class TestMountRemove(MountTestCase):
    def test_remove_restores_the_project_directory(self) -> None:
        self.mount("add", str(self.root / "sub"), str(self.source))
        report = self.mount("remove", str(self.root / "sub"))
        self.assertEqual(report, "tine: sub is no longer mounted\n")
        self.assertEqual(self.declared(), {})
        self.assertEqual(tine.local_mounts(self.root), {})

    def test_remove_preserves_other_mounts(self) -> None:
        (self.root / "other").mkdir()
        self.mount("add", str(self.root / "sub"), str(self.source))
        self.mount("add", str(self.root / "other"), str(self.source))
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(set(self.declared()), {"other"})

    def test_remove_rejects_an_unknown_mount(self) -> None:
        with self.assertRaisesRegex(SystemExit, "sub is not mounted"):
            self.mount("remove", str(self.root / "sub"))

    def test_remove_accepts_a_stale_declaration(self) -> None:
        # Removing a mount must not require its missing source to validate.
        declare(self.root, {"sub": "/nonexistent/source"})
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(self.declared(), {})


class TestMountList(MountTestCase):
    def test_list_prints_mount_targets_and_sources(self) -> None:
        (self.root / "other").mkdir()
        self.mount("add", str(self.root / "sub"), str(self.source))
        stdout = io.StringIO()
        with (
            unittest.mock.patch.object(
                tine,
                "graph_mount_targets",
                return_value={"checkout", "sub"},
            ),
            contextlib.redirect_stdout(stdout),
        ):
            report = self.mount("list")
        self.assertEqual(
            stdout.getvalue(),
            f"TARGET   SOURCE\ncheckout default\nother    default\nsub      {self.source}\n",
        )
        self.assertEqual(report, "")

    def test_graph_targets_come_from_labeled_buck_targets(self) -> None:
        output = "root//:top.git\nroot//packages/hello:hello.git\n"
        proc = subprocess.CompletedProcess([], 0, stdout=output)
        with unittest.mock.patch("subprocess.run", return_value=proc) as run:
            targets = tine.graph_mount_targets(self.root)
        self.assertEqual(
            targets,
            {"top", "packages/hello/hello"},
        )
        self.assertIn(tine.MOUNT_TARGET_LABEL, run.call_args.args[0][-1])


class TestDeclaredMounts(MountTestCase):
    """Invalid declarations stop the command instead of falling back to project contents."""

    def test_missing_source_is_rejected(self) -> None:
        declare(self.root, {"sub": "/nonexistent/source"})
        with self.assertRaisesRegex(SystemExit, "is not a directory; `tine mount remove sub`"):
            self.declared()

    def test_missing_target_is_rejected(self) -> None:
        declare(self.root, {"gone": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "no such directory; `tine mount remove gone`"):
            self.declared()

    def test_parent_target_is_rejected(self) -> None:
        declare(self.root, {"../elsewhere": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "target must be a project-relative path"):
            self.declared()

    def test_absolute_target_is_rejected(self) -> None:
        declare(self.root, {str(self.root / "sub"): str(self.source)})
        with self.assertRaisesRegex(SystemExit, "target must be a project-relative path"):
            self.declared()

    def test_relative_source_is_rejected(self) -> None:
        declare(self.root, {"sub": "../elsewhere"})
        with self.assertRaisesRegex(SystemExit, "source must be an absolute path"):
            self.declared()

    def test_reserved_target_is_rejected(self) -> None:
        (self.root / tine.HOME).mkdir()
        declare(self.root, {tine.HOME: str(self.source)})
        with self.assertRaisesRegex(SystemExit, "reserved for tine"):
            self.declared()

    def test_overlapping_mounts_are_rejected(self) -> None:
        # The outer mount can hide the inner target, making the result order-dependent.
        (self.root / "sub" / "inner").mkdir()
        declare(self.root, {"sub": str(self.source), "sub/inner": str(self.source)})
        with self.assertRaisesRegex(SystemExit, "overlaps mount"):
            self.declared()

    def test_target_that_becomes_a_symlink_is_rejected(self) -> None:
        # A target can become a symlink after the declaration was written.
        elsewhere = scratch(self).resolve()
        declare(self.root, {"sub": str(self.source)})
        (self.root / "sub").rmdir()
        (self.root / "sub").symlink_to(elsewhere)
        with self.assertRaisesRegex(SystemExit, "target is a symlink.*`tine mount remove sub`"):
            self.declared()

    def test_symlink_target_can_still_be_removed(self) -> None:
        elsewhere = scratch(self).resolve()
        declare(self.root, {"sub": str(self.source)})
        (self.root / "sub").rmdir()
        (self.root / "sub").symlink_to(elsewhere)
        self.mount("remove", str(self.root / "sub"))
        self.assertEqual(self.declared(), {})

    def test_hand_written_internal_source_is_rejected(self) -> None:
        # An internal source can itself be covered by a mount, making its digest namespace-dependent.
        (self.root / "inside").mkdir()
        declare(self.root, {"sub": str(self.root / "inside")})
        with self.assertRaisesRegex(SystemExit, "must be outside the project"):
            self.declared()

    def test_a_project_with_no_block_to_read(self) -> None:
        self.assertEqual(self.declared(), {})

    def test_local_mount_values_must_be_strings(self) -> None:
        mount_config(self.root).write_text("[mounts]\nsub = 1\n")
        with self.assertRaisesRegex(SystemExit, "must contain only strings"):
            self.declared()

    def test_the_mount_file_rejects_unrelated_local_settings(self) -> None:
        mount_config(self.root).write_text('[commands.check]\nsteps = [["buck"]]\n')
        with self.assertRaisesRegex(SystemExit, "owned by `tine mount`.*unsupported keys"):
            self.declared()


class TestMountUpdates(MountTestCase):
    @contextlib.contextmanager
    def process(self, code: str, *args: str) -> collections.abc.Generator[subprocess.Popen[str]]:
        prefix = f"import sys\nsys.path[:] = {sys.path!r}\nimport tine\n"
        with subprocess.Popen(
            [sys.executable, "-c", prefix + textwrap.dedent(code), str(self.root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def line(self, process: subprocess.Popen[str]) -> str:
        assert process.stdout is not None
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "child did not respond")
        line = process.stdout.readline().strip()
        if not line:
            assert process.stderr is not None
            self.fail(process.stderr.read())
        return line

    def wait_for_lock(self, process: subprocess.Popen[str]) -> None:
        self.assertEqual(self.line(process), "ready")
        assert process.stderr is not None
        self.assertTrue(select.select([process.stderr], [], [], 5)[0], "child did not wait")
        self.assertEqual(
            process.stderr.readline().strip(), "tine: waiting for another mount update to finish"
        )

    def test_concurrent_mount_updates_preserve_both_entries(self) -> None:
        (self.root / "other").mkdir()
        code = """
            from pathlib import Path
            print("ready", flush=True)
            tine.mount_command(Path(sys.argv[1]), ["add", sys.argv[2], sys.argv[3]])
            print("updated", flush=True)
        """
        with contextlib.ExitStack() as cleanup:
            with tine.mount_table_lock(self.root):
                client = cleanup.enter_context(self.process(code, str(self.root / "sub"), str(self.source)))
                self.wait_for_lock(client)
                declare(self.root, {"other": str(self.source)})
            self.assertEqual(self.line(client), "updated")
            self.assertEqual(client.wait(timeout=5), 0)
        self.assertEqual(tine.local_mounts(self.root), {"other": str(self.source), "sub": str(self.source)})

    def test_removed_mount_is_revalidated_after_waiting_for_lock(self) -> None:
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        declare(self.root, {"sub": "/nonexistent/source"})
        code = """
            from pathlib import Path
            from unittest.mock import patch
            print("ready", flush=True)
            with patch.object(tine, "graph_mount_targets", return_value=set()):
                tine.mount_command(Path(sys.argv[1]), ["add", sys.argv[2], sys.argv[3]])
        """
        with contextlib.ExitStack() as cleanup:
            with tine.mount_table_lock(self.root):
                client = cleanup.enter_context(self.process(code, str(self.root / "sub"), str(self.source)))
                self.wait_for_lock(client)
                declare(self.root, {})
            _, stderr = client.communicate(timeout=5)
            self.assertEqual(client.returncode, 1, stderr)
            self.assertIn("mount sub: not a valid target", stderr)
        self.assertEqual(self.declared(), {})

    def test_lock_parent_creation_error_is_reported(self) -> None:
        (self.root / tine.HOME).touch()
        with self.assertRaisesRegex(SystemExit, "cannot prepare .* for locking"):
            with tine.mount_table_lock(self.root):
                self.fail("lock unexpectedly acquired")

    def test_lock_open_error_is_reported(self) -> None:
        (self.root / tine.MOUNT_LOCK).mkdir(parents=True)
        with self.assertRaisesRegex(SystemExit, "cannot prepare .* for locking"):
            with tine.mount_table_lock(self.root):
                self.fail("lock unexpectedly acquired")


class TestMountPrivacy(MountTestCase):
    """Mount declarations stay machine-local."""

    def test_refresh_does_not_publish_mount_state(self) -> None:
        path = self.root / ".buckconfig"
        original = path.read_bytes()
        declare(self.root, {"sub": str(self.source)})
        refresh_project(self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(read_generated_buckconfig(path), {})


class TestNamespaces(MountTestCase):
    """Create and inspect bind mounts in child-process namespaces."""

    def answer(self, work: collections.abc.Callable[[], str]) -> str:
        """Run namespace-changing work in a child and return its result."""
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(read)
                with contextlib.redirect_stderr(io.StringIO()):
                    answer = work()
                os.write(write, answer.encode())
            except BaseException:
                import traceback

                os.write(write, traceback.format_exc().encode())
                code = 1
            os._exit(code)
        os.close(write)
        with os.fdopen(read, "rb") as pipe:
            answer = pipe.read().decode()
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), 0, answer)
        return answer

    def create(self, mounts: dict[str, str]) -> None:
        tine.create_mount_namespace(self.root, mounts)

    @override
    def setUp(self) -> None:
        super().setUp()
        (self.root / "sub" / "witness").write_text("the project's own")
        (self.source / "witness").write_text("the mounted directory's")

    def test_bind_mount_replaces_target_contents(self) -> None:
        def mounted() -> str:
            self.create({"sub": str(self.source)})
            return (self.root / "sub" / "witness").read_text()

        self.assertEqual(self.answer(mounted), "the mounted directory's")
        # The child's mount must not propagate back into the test process.
        self.assertEqual((self.root / "sub" / "witness").read_text(), "the project's own")

    def test_user_namespace_maps_only_the_caller(self) -> None:
        def mapped() -> str:
            self.create({"sub": str(self.source)})
            return Path("/proc/self/uid_map").read_text()

        lines = self.answer(mapped).splitlines()
        uid = str(os.getuid())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split(), [uid, uid, "1"])

    def test_all_declared_mounts_are_applied(self) -> None:
        (self.root / "other").mkdir()
        other = scratch(self).resolve()
        (other / "witness").write_text("the other one's")

        def mounted() -> str:
            self.create({"sub": str(self.source), "other": str(other)})
            return (self.root / "other" / "witness").read_text()

        self.assertEqual(self.answer(mounted), "the other one's")

    def test_mount_error_names_its_target(self) -> None:
        def mounted() -> str:
            with self.assertRaisesRegex(SystemExit, "mount sub: cannot build it from"):
                tine.create_mount_namespace(
                    self.root,
                    {"sub": "/nonexistent/source"},
                )
            return "refused"

        self.assertEqual(self.answer(mounted), "refused")

    def test_symlinked_root_config_stays_shared(self) -> None:
        original = self.root / ".buckconfig"
        shared = scratch(self).resolve() / ".buckconfig"
        shared.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(shared)

        def mounted() -> str:
            self.create({"sub": str(self.source)})
            tine.write_if_changed(original, "[cells]\nroot = .\n")
            return original.read_text()

        self.assertEqual(self.answer(mounted), shared.read_text())
        self.assertTrue(original.is_symlink())

    def test_root_config_stays_shared_inside_the_namespace(self) -> None:
        original = self.root / ".buckconfig"

        def mounted() -> str:
            self.create({"sub": str(self.source)})
            util_content = "[cells]\nroot = .\n[buck2]\nmaterializations = deferred\n"
            tine.write_if_changed(original, util_content)
            return original.read_text()

        content = self.answer(mounted)
        self.assertEqual(original.read_text(), content)
        self.assertIn("materializations = deferred", content)

    def test_mount_table_recovers_mounts_and_shared_layers_stay(self) -> None:
        local = self.root / tine.LOCAL
        local.write_text("[build]\nthreads = 4\n")
        layer = self.root / ".buckconfig.d" / "settings"
        layer.parent.mkdir()
        layer.write_text("[custom]\nkey = original\n")
        before = {path: path.read_bytes() for path in (self.root / ".buckconfig", local, layer)}
        mounts = {"sub": str(self.source)}

        def mounted() -> str:
            table = tine.create_mount_namespace(self.root, mounts)
            with unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}):
                self.assertEqual(tine.namespace_mount_targets(), ["sub"])
                self.assertEqual(tine.namespace_gitdirs(self.root), {})
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            return table

        self.answer(mounted)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def check_mount_change(self, *, replace_source: bool) -> None:
        mounts = {"sub": str(self.source)}
        declare(self.root, mounts)
        replacement = scratch(self)
        (replacement / "witness").write_text("replacement")
        retired = scratch(self) / "retired"

        def first() -> str:
            first_table = tine.create_mount_namespace(self.root, mounts)
            if replace_source:
                self.source.rename(retired)
                replacement.rename(self.source)
            else:
                declare(self.root, {"sub": str(replacement)})

            def second() -> str:
                second_mounts = tine.declared_mounts(self.root)
                table = tine.create_mount_namespace(self.root, second_mounts)
                with unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}):
                    _, prepared = tine.prepare_buck(self.root, {}, ["buck", "build"])
                self.assertEqual(prepared, sorted(second_mounts))
                self.assertEqual((self.root / "sub" / "witness").read_text(), "replacement")
                return table

            self.answer(second)
            # Resume A only after B has prepared its replacement, at the point where a shared
            # carrier used to make A advertise B's mounts while retaining A's checkout.
            with unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: first_table}):
                _, prepared = tine.prepare_buck(self.root, {}, ["buck", "build"])
            self.assertEqual(prepared, sorted(mounts))
            self.assertEqual((self.root / "sub" / "witness").read_text(), "the mounted directory's")
            return "separate"

        self.assertEqual(self.answer(first), "separate")

    def test_source_replacement_cannot_relabel_an_existing_namespace(self) -> None:
        self.check_mount_change(replace_source=True)

    def test_mount_update_cannot_relabel_an_existing_namespace(self) -> None:
        self.check_mount_change(replace_source=False)

    def test_defaults_come_from_the_mounted_cell(self) -> None:
        def mounted() -> str:
            table = tine.create_mount_namespace(self.root, {"sub": str(self.source)})
            with (
                unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}),
                unittest.mock.patch.object(tine, "COMMAND_PATH", self.root / cell / tine.COMMAND),
            ):
                config, targets = tine.prepare_buck(
                    self.root, tine.project_settings(self.root), ["buck", "build"]
                )
            self.assertEqual(targets, ["sub"])
            self.assertEqual(config["buck2"]["defer_write_actions"], "true")
            return config["cells"]["tine"]

        for cell in ("sub", "sub/inner"):
            with self.subTest(cell=cell):
                source = self.source / Path(cell).relative_to("sub")
                source.mkdir(exist_ok=True)
                (source / ".buckconfig").write_text(CELL_BUCKCONFIG)
                wrapper_command(source)
                configure_cells(self.root, cell)
                self.assertEqual(self.answer(mounted), cell)

    def test_handover_and_defaults_use_the_locally_selected_mounted_cell(self) -> None:
        configure_cells(self.root, "missing")
        (self.root / tine.LOCAL).write_text("[cells]\ntine = sub\n")
        (self.source / ".buckconfig").write_text(CELL_BUCKCONFIG)
        wrapper_command(self.source)
        declare(self.root, {"sub": str(self.source)})

        def mounted() -> str:
            with (
                unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as handover,
                self.assertRaisesRegex(SystemExit, "handover"),
            ):
                tine.prepare_buck(self.root, tine.project_settings(self.root), ["buck", "build"])
            selected, _, environment = handover.call_args.args
            self.assertEqual(selected, self.root / "sub" / tine.COMMAND)
            with (
                unittest.mock.patch.dict(os.environ, environment),
                unittest.mock.patch.object(tine, "COMMAND_PATH", selected),
            ):
                config, targets = tine.prepare_buck(
                    self.root, tine.project_settings(self.root), ["buck", "build"]
                )
            self.assertEqual(targets, ["sub"])
            self.assertEqual(config["cells"]["tine"], "sub")
            self.assertEqual(config["buck2"]["defer_write_actions"], "true")
            return "mounted defaults"

        self.assertEqual(self.answer(mounted), "mounted defaults")

    def test_ignores_follow_the_mounted_checkout_after_source_replacement(self) -> None:
        isolate_git(self)
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        replacement = scratch(self)
        for checkout, ignored in ((self.source, "original-output"), (replacement, "replacement-output")):
            git("init", "--quiet", "--initial-branch=main", cwd=checkout)
            (checkout / ".gitignore").write_text(f"/{ignored}\n")
            (checkout / ignored).mkdir()
        retired = scratch(self) / "retired"

        def mounted() -> str:
            table = tine.create_mount_namespace(self.root, {"sub": str(self.source)})
            self.source.rename(retired)
            replacement.rename(self.source)
            with unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}):
                config, targets = tine.prepare_buck(self.root, {}, ["buck", "build"])
                ignores = tine.collect_project_ignores(
                    self.root, config, targets, tine.namespace_gitdirs(self.root)
                )
            self.assertIn("sub/original-output", ignores)
            self.assertNotIn("sub/replacement-output", ignores)
            return "mounted ignores"

        self.assertEqual(self.answer(mounted), "mounted ignores")

    def test_mounted_relative_gitdir_keeps_its_metadata_and_worktree(self) -> None:
        isolate_git(self)
        metadata = scratch(self).resolve()
        git("init", "--quiet", "--separate-git-dir", str(metadata), cwd=self.source)
        pointer = f"gitdir: ../{metadata.name}\n"
        (self.source / ".git").write_text(pointer)
        git("config", "core.worktree", str(self.source), cwd=self.source)
        (self.source / ".gitignore").write_text("/original-output\n/tracked-output\n")
        (self.source / "original-output").mkdir()
        (self.source / "tracked-output").touch()
        git("add", "--force", "tracked-output", cwd=self.source)
        (metadata / "info" / "exclude").write_text("/metadata-output\n")
        (self.source / "metadata-output").mkdir()
        replacement = scratch(self)
        (replacement / ".gitignore").write_text("/replacement-output\n")
        (replacement / "replacement-output").mkdir()
        retired = scratch(self) / "retired"

        def mounted() -> str:
            table = tine.create_mount_namespace(self.root, {"sub": str(self.source)})
            self.source.rename(retired)
            replacement.rename(self.source)
            declare(self.root, {})
            with unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}):
                _, targets = tine.prepare_buck(self.root, {}, ["buck", "build"])
                ignores = tine.collect_project_ignores(
                    self.root, {"cells": {"root": "."}}, targets, tine.namespace_gitdirs(self.root)
                )
            self.assertIn("sub/original-output", ignores)
            self.assertIn("sub/metadata-output", ignores)
            self.assertNotIn("sub/tracked-output", ignores)
            self.assertNotIn("sub/replacement-output", ignores)
            self.assertEqual((self.root / "sub" / ".git").read_text(), pointer)
            return "relative gitdir"

        self.assertEqual(self.answer(mounted), "relative gitdir")
        self.assertEqual((retired / ".git").read_text(), pointer)

    def test_mounted_submodule_cell_keeps_its_gitignores(self) -> None:
        isolate_git(self)
        origin = scratch(self)
        git("init", "--quiet", "--initial-branch=main", cwd=origin)
        (origin / ".gitignore").write_text("/generated\n")
        git("add", ".gitignore", cwd=origin)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "--message",
            "initial",
            cwd=origin,
        )
        parent = scratch(self)
        git("init", "--quiet", "--initial-branch=main", cwd=parent)
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            str(origin),
            "checkout",
            cwd=parent,
        )
        source = parent / "checkout"
        (source / "generated").mkdir()
        (source / ".buckconfig").write_text(CELL_BUCKCONFIG)
        wrapper_command(source)
        configure_cells(self.root, "sub")

        def mounted() -> str:
            table = tine.create_mount_namespace(self.root, {"sub": str(source)})
            with (
                contextlib.chdir(self.root),
                unittest.mock.patch.dict(
                    os.environ, {tine.TINE_MOUNTS_ENV: table, "BUCK2_BINARY": str(self.root / "buck2")}
                ),
                unittest.mock.patch.object(tine, "COMMAND_PATH", self.root / "sub" / tine.COMMAND),
                unittest.mock.patch.object(tine, "ensure_buck2_binary", return_value=True),
                unittest.mock.patch.object(
                    tine, "namespace_gitdirs", wraps=tine.namespace_gitdirs
                ) as gitdirs,
                unittest.mock.patch.object(os, "execv") as execve,
            ):
                tine.buck_command(["build", "tine//..."])
            execve.assert_called_once()
            gitdirs.assert_called_once_with(self.root)
            config = read_generated_buckconfig(self.root / "sub" / tine.LOCAL)
            self.assertEqual(
                config["project"][tine.PROJECT_IGNORE], ", ".join([*tine.VCS_IGNORES, "generated"])
            )
            return "submodule ignores"

        self.assertEqual(self.answer(mounted), "submodule ignores")

    def test_mounting_does_not_make_version_dirty(self) -> None:
        isolate_git(self)
        (self.root / "sub" / "witness").write_text((self.source / "witness").read_text())
        git("init", "--quiet", "--initial-branch=main", cwd=self.root)
        git("config", "user.name", "Test", cwd=self.root)
        git("config", "user.email", "test@example.com", cwd=self.root)
        git("add", ".", cwd=self.root)
        git("commit", "--quiet", "--message", "initial", cwd=self.root)
        self.mount("add", str(self.root / "sub"), str(self.source))

        def mounted() -> str:
            self.create({"sub": str(self.source)})
            derived = tine.version_components(self.root)
            assert isinstance(derived, dict), derived
            self.assertNotIn("dirty", derived)
            return "clean"

        self.assertEqual(self.answer(mounted), "clean")


class TestConfiguredWrapperEntrypoint(MountTestCase):
    """The configured tine cell supplies the command, mounted or not."""

    def config(self, cell: str) -> dict[str, dict[str, str]]:
        return {"cells": {"root": ".", tine.CELL: cell}}

    def test_configured_cell_uses_its_own_command(self) -> None:
        for cell in (".", "sub", "sub/vendor", str(self.source)):
            with self.subTest(cell=cell):
                command = wrapper_command(self.root / cell)
                self.assertEqual(tine.configured_wrapper_entrypoint(self.root, self.config(cell)), command)

    def test_configured_cell_requires_a_command(self) -> None:
        # Falling back would combine one checkout's command with another's rules.
        with self.assertRaisesRegex(SystemExit, "tine cell has no bin/tine"):
            tine.configured_wrapper_entrypoint(self.root, self.config("sub"))

    def test_legacy_repositories_select_the_command(self) -> None:
        command = wrapper_command(self.root / "sub")
        config = {"repositories": {tine.CELL: "sub"}}
        self.assertEqual(tine.configured_wrapper_entrypoint(self.root, config), command)

    def test_project_without_tine_cell_keeps_current_command(self) -> None:
        entrypoint = tine.configured_wrapper_entrypoint(self.root, {"cells": {"root": "."}})
        self.assertEqual(entrypoint, TOOL_PATH.absolute())

    def test_empty_cells_do_not_fall_back_to_repositories(self) -> None:
        wrapper_command(self.root / "sub")
        config = {"cells": {}, "repositories": {tine.CELL: "sub"}}
        entrypoint = tine.configured_wrapper_entrypoint(self.root, config)
        self.assertEqual(entrypoint, TOOL_PATH.absolute())


class TestReexecConfiguredWrapper(MountTestCase):
    """Select the wrapper without performing real namespace operations."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.made: list[object] = []
        created = unittest.mock.patch.object(
            tine,
            "create_mount_namespace",
            side_effect=lambda root, mounts: (
                self.made.append((root, mounts)) or tine.encode_mount_table(sorted(mounts), {})
            ),
        )
        created.start()
        self.addCleanup(created.stop)
        patched = unittest.mock.patch.dict(os.environ)
        patched.start()
        os.environ.pop(tine.TINE_MOUNTS_ENV, None)
        self.addCleanup(patched.stop)

    @contextlib.contextmanager
    def running(self) -> collections.abc.Generator[list[object]]:
        execve: list[object] = []
        with unittest.mock.patch.object(os, "execve", side_effect=lambda *a: execve.extend(a)):
            yield execve

    def declare_one(self) -> str:
        self.mount("add", str(self.root / "sub"), str(self.source))
        return tine.encode_mount_table(["sub"], {})

    def prepare(self) -> None:
        tine.prepare_buck(self.root, {}, ["buck", "build"])

    def test_no_mounts_need_no_namespace(self) -> None:
        with self.running() as execve:
            _, mounts = tine.prepare_buck(self.root, {}, ["buck", "build"])
        self.assertEqual(mounts, [])
        self.assertEqual((self.made, execve), ([], []))

    def test_unmounted_handover_precedes_reading_defaults_and_pins(self) -> None:
        command = wrapper_command(self.root / "sub")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = missing\n")
        configure_cells(self.root, "sub")
        before = (self.root / ".buckconfig").read_bytes()
        arguments = ["buck", "build", "tine//...", "--", "an argument"]
        with (
            contextlib.chdir(self.root),
            unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as execve,
            unittest.mock.patch.object(
                tine, "ensure_buck2_binary", side_effect=AssertionError("read outer pin")
            ),
            self.assertRaisesRegex(SystemExit, "handover"),
        ):
            tine.main(arguments)
        execve.assert_called_once_with(command, [str(command), *arguments], dict(os.environ))
        self.assertEqual(self.made, [])
        self.assertEqual((self.root / ".buckconfig").read_bytes(), before)
        self.assertFalse((self.root / tine.LOCAL).exists())

    def test_local_cell_override_selects_the_unmounted_wrapper(self) -> None:
        configure_cells(self.root, "missing")
        command = wrapper_command(self.root / "sub")
        (self.root / tine.LOCAL).write_text("[cells]\ntine = sub\n")
        with (
            unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as execve,
            self.assertRaisesRegex(SystemExit, "handover"),
        ):
            tine.prepare_buck(self.root, tine.project_settings(self.root), ["buck", "build"])
        self.assertEqual(execve.call_args.args[0], command)

    def test_same_wrapper_and_its_symlinks_need_no_handover(self) -> None:
        command = wrapper_command(self.root / "sub")
        (self.root / "alias").symlink_to("sub", target_is_directory=True)
        for cell in ("sub", "./sub", str(self.root / "sub"), "alias"):
            with self.subTest(cell=cell):
                (self.root / ".buckconfig").write_text(f"[cells]\nroot = .\ntine = {cell}\n")
                with unittest.mock.patch.object(tine, "COMMAND_PATH", command), self.running() as execve:
                    tine.prepare_buck(self.root, {}, ["buck", "build"], refresh_config=False)
                self.assertEqual((self.made, execve), ([], []))

    def test_mounted_invocations_still_select_the_wrapper(self) -> None:
        command = wrapper_command(self.root / "sub")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = sub\n")
        with (
            unittest.mock.patch.dict(
                os.environ, {tine.TINE_MOUNTS_ENV: tine.encode_mount_table(["sub"], {})}
            ),
            unittest.mock.patch.object(tine, "namespace_mount_targets", return_value=["sub"]),
            unittest.mock.patch.object(tine, "declared_mounts", side_effect=AssertionError("reread mounts")),
            unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as execve,
        ):
            with self.assertRaisesRegex(SystemExit, "handover"):
                self.prepare()
            execve.assert_called_once_with(command, [str(command), "buck", "build"], dict(os.environ))
        self.assertEqual(self.made, [])

    def test_completion_uses_the_configured_wrapper(self) -> None:
        command = wrapper_command(self.root / "sub")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = sub\n")
        for arguments in (["buck", "complete", "--target=tine//"], ["completion", "bash"]):
            with (
                self.subTest(arguments=arguments),
                contextlib.chdir(self.root),
                unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as execve,
                self.assertRaisesRegex(SystemExit, "handover"),
            ):
                tine.main(arguments)
            self.assertEqual(execve.call_args.args[:2], (command, [str(command), *arguments]))
        self.assertEqual(self.made, [])

    def test_mounts_reload_the_same_wrapper_path_once(self) -> None:
        self.declare_one()
        command = wrapper_command(self.root / "sub")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = sub\n")
        with (
            unittest.mock.patch.object(tine, "COMMAND_PATH", command),
            unittest.mock.patch.object(os, "execve", side_effect=SystemExit("handover")) as execve,
            self.assertRaisesRegex(SystemExit, "handover"),
        ):
            self.prepare()
        self.assertEqual(self.made, [(self.root, {"sub": str(self.source)})])
        self.assertEqual(execve.call_args.args[0], command)
        with (
            unittest.mock.patch.object(tine, "namespace_mount_targets", return_value=["sub"]),
            unittest.mock.patch.object(tine, "COMMAND_PATH", command),
            self.running() as handover,
        ):
            tine.prepare_buck(self.root, {}, ["buck", "build"], refresh_config=False)
        self.assertEqual(handover, [])
        self.assertEqual(len(self.made), 1)

    def test_failed_handover_names_the_selected_wrapper(self) -> None:
        command = wrapper_command(self.root / "sub")
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = sub\n")
        with (
            unittest.mock.patch.object(os, "execve", side_effect=PermissionError("not executable")),
            self.assertRaisesRegex(SystemExit, f"cannot run {re.escape(str(command))}: not executable"),
        ):
            self.prepare()

    def test_matching_current_namespace_needs_no_handover(self) -> None:
        table = self.declare_one()
        with (
            unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}),
            unittest.mock.patch.object(tine, "namespace_mount_targets", return_value=["sub"]),
            self.running() as execve,
        ):
            self.prepare()
        self.assertEqual((self.made, execve), ([], []))

    def test_a_garbled_mount_table_fails_instead_of_remounting(self) -> None:
        # Quietly rebuilding the namespace would reread declarations the marked command froze.
        self.declare_one()
        with (
            unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: "not json"}),
            self.assertRaises(SystemExit),
        ):
            self.prepare()

    def test_mounts_create_an_equivalent_namespace_and_handover(self) -> None:
        table = self.declare_one()
        with self.running() as execve:
            self.prepare()
        self.assertEqual(self.made, [(self.root, {"sub": str(self.source)})])
        _, argv, environment = execve
        self.assertEqual(argv, [str(TOOL_PATH.absolute()), "buck", "build"])
        assert isinstance(environment, dict)
        self.assertEqual(environment[tine.TINE_MOUNTS_ENV], table)


class TestBuck2Binary(unittest.TestCase):
    """Resolving the pin, short of fetching anything."""

    def pin(self, sha256: str = "a" * 64) -> dict[str, object]:
        platform = tine._host_platform()
        return {
            "buck2": {
                "repository": "example/buck2",
                "release": "2026-01-01",
                "platforms": {platform: {"artifact": "buck2.zst", "sha256": sha256}},
            },
        }

    def cache(self, sha256: str) -> Path:
        home = scratch(self)
        binary = home / "tine" / "buck2" / sha256 / "buck2"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        return home

    def cell(self, spec: object | None = None) -> Path:
        cell = scratch(self, "tine-test-cell.")
        pins(cell, spec)
        return cell

    def test_the_cell_declares_the_pin(self) -> None:
        home = self.cache("a" * 64)
        binary = home / "tine" / "buck2" / ("a" * 64) / "buck2"
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            self.assertTrue(tine.ensure_buck2_binary({}, self.cell()))
            self.assertEqual(os.environ["BUCK2_BINARY"], str(binary))
            self.assertEqual(os.environ["BUCK2_ARG0"], "tine buck")

    def test_a_project_pin_overrides_the_cell_key_by_key(self) -> None:
        platform = tine._host_platform()
        home = self.cache("b" * 64)
        config = {"buck2": {"platforms": {platform: {"sha256": "b" * 64}}}}
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            self.assertTrue(tine.ensure_buck2_binary(config, self.cell()))
            binary = os.environ["BUCK2_BINARY"]
        self.assertEqual(binary, str(home / "tine" / "buck2" / ("b" * 64) / "buck2"))

    def test_the_override_is_read_from_tine_toml(self) -> None:
        platform = tine._host_platform()
        root = scratch(self)
        (root / tine.CONFIG).write_text(
            f'''[buck2.platforms."{platform}"]
sha256 = "{"b" * 64}"
'''
        )
        self.assertEqual(
            tine.buck2_pin_overrides(tine.project_settings(root), platform),
            {"sha256": "b" * 64},
        )

    def test_override_values_have_their_structured_type(self) -> None:
        with self.assertRaisesRegex(SystemExit, "sha256.*must be a non-empty string"):
            tine.buck2_pin_overrides(
                {"buck2": {"platforms": {tine._host_platform(): {"sha256": 1}}}},
                tine._host_platform(),
            )

    def test_unknown_override_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: repositroy"):
            tine.buck2_pin_overrides({"buck2": {"repositroy": "example/buck2"}}, tine._host_platform())

    def test_unknown_override_platforms_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported platforms: Linux-x86-64"):
            tine.buck2_pin_overrides(
                {"buck2": {"platforms": {"Linux-x86-64": {"sha256": "a" * 64}}}},
                tine._host_platform(),
            )

    def test_every_platform_override_is_validated(self) -> None:
        other = next(platform for platform in tine.PLATFORMS if platform != tine._host_platform())
        with self.assertRaisesRegex(SystemExit, rf"{other}.*unsupported keys: sh256"):
            tine.buck2_pin_overrides(
                {"buck2": {"platforms": {other: {"sh256": "a" * 64}}}},
                tine._host_platform(),
            )

    def test_every_platform_hash_is_validated(self) -> None:
        other = next(platform for platform in tine.PLATFORMS if platform != tine._host_platform())
        with self.assertRaisesRegex(SystemExit, rf"{other}.*64 lowercase hexadecimal"):
            tine.buck2_pin_overrides(
                {"buck2": {"platforms": {other: {"sha256": "abc"}}}},
                tine._host_platform(),
            )

    def test_an_uncached_pin_is_nothing_to_complete_with(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(scratch(self))}):
            self.assertFalse(tine.ensure_buck2_binary(self.pin(), self.cell(), fetch=False))

    def test_a_cell_declaring_no_buck2(self) -> None:
        with self.assertRaisesRegex(SystemExit, "declares no buck2"):
            tine.ensure_buck2_binary({}, self.cell({"ruff": {}}))

    def test_a_cell_with_no_pins(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot read .*tine.lock.json"):
            tine.ensure_buck2_binary({}, scratch(self, "tine-test-cell."))

    def test_a_machine_buck2_is_not_published_for(self) -> None:
        uname = os.uname_result(("Linux", "host", "7.0", "#1", "m68k"))
        with unittest.mock.patch.object(os, "uname", return_value=uname):
            with self.assertRaisesRegex(SystemExit, "unsupported platform: Linux-m68k"):
                tine.ensure_buck2_binary(self.pin(), scratch(self, "tine-test-cell."))

    def test_a_pin_that_is_not_a_sha256(self) -> None:
        with self.assertRaisesRegex(SystemExit, "64 lowercase hexadecimal"):
            tine.ensure_buck2_binary(self.pin("abc"), self.cell())


class TestDownload(unittest.TestCase):
    """The pin is what makes an unverified binary safe to run, so it is checked before it is kept."""

    def artifact(self, content: bytes) -> str:
        path = scratch(self) / "buck2.zst"
        path.write_bytes(
            subprocess.run(["zstd", "--stdout"], input=content, stdout=subprocess.PIPE, check=True).stdout
        )
        return path.as_uri()

    def test_a_download_that_matches_its_pin(self) -> None:

        url = self.artifact(b"binary")
        digest = hashlib.sha256(Path(url.removeprefix("file://")).read_bytes()).hexdigest()
        binary = scratch(self) / "cache" / "buck2"
        for version in ((3, 12), (3, 14)):
            with (
                self.subTest(version=version),
                unittest.mock.patch.object(sys, "version_info", version),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                fetched = tine._download_binary(url, digest, binary, compressed=True)
                self.assertEqual(fetched.read_bytes(), b"binary")

    def test_an_artifact_that_is_the_binary_itself(self) -> None:

        path = scratch(self) / "tool"
        path.write_bytes(b"binary")
        binary = scratch(self) / "cache" / "tool"
        digest = hashlib.sha256(b"binary").hexdigest()
        with contextlib.redirect_stderr(io.StringIO()):
            fetched = tine._download_binary(path.as_uri(), digest, binary, compressed=False)
        self.assertEqual(fetched.read_bytes(), b"binary")
        self.assertTrue(os.access(fetched, os.X_OK))

    def test_a_download_that_does_not(self) -> None:
        into = scratch(self) / "cache" / ("a" * 64)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "not the pinned"):
                tine._download_binary(self.artifact(b"binary"), "a" * 64, into / "buck2", compressed=True)
        # Nothing half-verified is left where the next command would take it for the binary, and
        # nothing at all under the name the pin is cached by.
        self.assertFalse(into.exists())

    def test_an_artifact_that_is_not_what_the_pin_says_it_is(self) -> None:

        # Wrong magic, and a stream that ends early: a pin can match either.
        compressed = Path(self.artifact(b"binary").removeprefix("file://")).read_bytes()
        for payload in (b"not zstd", compressed[:-4]):
            path = scratch(self) / "buck2.zst"
            path.write_bytes(payload)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "not a whole zstd stream"):
                    tine._download_binary(
                        path.as_uri(),
                        hashlib.sha256(payload).hexdigest(),
                        scratch(self) / "buck2",
                        compressed=True,
                    )


class TestBuckCommand(unittest.TestCase):
    """The verb every keypress in a completing shell reaches."""

    @override
    def setUp(self) -> None:
        self.root = scratch(self)
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n")
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)
        self.binary = self.root / "buck2"
        self.binary.write_text("")
        # As under a real wrapper run: resolution exported the environment before anything here runs.
        environment = unittest.mock.patch.dict(os.environ, {"BUCK2_BINARY": str(self.binary)})
        environment.start()
        self.addCleanup(environment.stop)

    @contextlib.contextmanager
    def running(self) -> collections.abc.Generator[list[object]]:
        execve: list[object] = []
        self.served: list[object] = []
        with contextlib.ExitStack() as patches:
            patch = unittest.mock.patch.object
            # Like the real one with nothing serving yet: what it readies is what Buck would start.
            patches.enter_context(
                patch(cache_shim, "ensure", side_effect=lambda _, ready: self.served.append(ready()))
            )
            patches.enter_context(patch(tine, "ensure_buck2_binary", return_value=True))
            patches.enter_context(patch(os, "execv", side_effect=lambda *a: execve.extend(a)))
            yield execve

    def configure_cache(self) -> cache_shim.CacheSettings:
        (self.root / tine.CONFIG).write_text('[cache]\nread_url = "https://cache.example"\n')
        (self.root / tine.LOCAL_SETTINGS).write_text("[cache]\nunsigned = true\n")
        cache = cache_shim.settings(tine.project_settings(self.root), self.root, tine.SETTINGS)
        assert cache is not None
        return cache

    def test_no_cache_configured_starts_no_shim(self) -> None:
        with self.running():
            tine.buck_command(["build", "//..."])
        self.assertEqual(self.served, [])

    def test_a_subcommand_that_runs_no_action_starts_nothing(self) -> None:
        """`tine buck kill` starting a cache that then idles for fifteen minutes reads as a fault."""
        self.configure_cache()
        inert = (
            ["complete", "--target", "//"],
            ["kill"],
            ["killall"],
            ["status"],
            ["clean"],
            ["log", "what-ran"],
        )
        for argv in inert:
            with self.subTest(argv=argv), self.running():
                tine.buck_command(argv)
                self.assertEqual(self.served, [])

    def test_a_configured_cache_reaches_the_generated_block(self) -> None:
        cache = self.configure_cache()
        with self.running():
            tine.buck_command(["build", "//..."])
            self.assertEqual(len(self.served), 1, "the shim buck is about to talk to has to exist")
        self.assertEqual(
            read_generated_buckconfig(self.root / tine.LOCAL)[tine.RE_CLIENT], tine.cache_client(cache)
        )

    def test_starting_the_shim_takes_the_address_out(self) -> None:
        """Buck tries a configured cache despite `--no-remote-cache`, and nothing answers it yet."""
        cache = self.configure_cache()
        tine.refresh_local_buckconfig(self.root, [], cache)
        with self.running():
            buck = tine.before_shim(self.root, tine.project_settings(self.root), "x")
        self.assertEqual(buck, [str(self.binary), "--no-buckd", tine.FLAG_ISOLATION, "x"])
        self.assertNotIn(tine.RE_CLIENT, read_generated_buckconfig(self.root / tine.LOCAL))

    def test_the_shim_builds_with_the_defaults_of_the_current_checkout(self) -> None:
        """The shim is a target of the tine cell, so it needs the cells that checkout declares now."""
        configure_cells(self.root, "tine")
        checkout = self.root / "tine"
        checkout.mkdir()
        (checkout / ".buckconfig").write_text("[cells]\ntine = .\ncatalog = catalog\n")
        with self.running():
            tine.before_shim(self.root, tine.project_settings(self.root), None)
        self.assertEqual(tine.cells_of(tine.read_project_buckconfig(self.root))["catalog"], "tine/catalog")

    def test_a_changed_address_reaches_the_next_invocation(self) -> None:
        """Each command's daemon reads the current configuration, so a new address only needs writing."""
        self.configure_cache()
        with self.running():
            tine.buck_command(["build", "//..."])
            (self.root / tine.LOCAL_SETTINGS).write_text("[cache]\nunsigned = true\nport = 21000\n")
            tine.buck_command(["build", "//..."])
        self.assertEqual(
            read_generated_buckconfig(self.root / tine.LOCAL)[tine.RE_CLIENT]["address"],
            "127.0.0.1:21000",
        )

    def test_a_project_may_not_name_the_cache_address_itself(self) -> None:
        self.configure_cache()
        (self.root / tine.LOCAL).write_text(f"[{tine.RE_CLIENT}]\naddress = 127.0.0.1:1\n")
        with self.running(), self.assertRaisesRegex(SystemExit, f"\\[{tine.RE_CLIENT}\\] is reserved"):
            tine.buck_command(["build", "//..."])

    def test_a_project_naming_it_with_no_cache_configured_is_its_own_business(self) -> None:
        (self.root / tine.LOCAL).write_text(f"[{tine.RE_CLIENT}]\naddress = 127.0.0.1:1\n")
        with self.running() as execve:
            tine.buck_command(["build", "//..."])
        self.assertTrue(execve)

    def test_it_configures_then_hands_over(self) -> None:
        with self.running() as execve:
            tine.buck_command(["build", "//x"])
        binary, argv = execve
        self.assertEqual(
            (binary, argv), (str(self.binary), [str(self.binary), "--no-buckd", "build", "//x"])
        )
        self.assertTrue((self.root / tine.LOCAL).is_file())

    def test_no_mounts_skip_private_git_metadata(self) -> None:
        table = tine.encode_mount_table([], {}).replace("{}", '"invalid"')
        with (
            self.running() as execve,
            unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}),
            unittest.mock.patch.object(tine, "namespace_gitdirs", wraps=tine.namespace_gitdirs) as gitdirs,
        ):
            tine.buck_command(["build", "//x"])
        gitdirs.assert_not_called()
        self.assertTrue(execve)

    def build_mounted(self, *targets: str) -> dict[str, dict[str, str]]:
        table = tine.encode_mount_table(list(targets), {})
        with self.running(), unittest.mock.patch.dict(os.environ, {tine.TINE_MOUNTS_ENV: table}):
            tine.buck_command(["build", "//x"])
        return read_generated_buckconfig(self.root / tine.LOCAL)

    def test_mounted_targets_build_in_dev_mode(self) -> None:
        self.assertEqual(
            self.build_mounted("packages/b.source", "packages/a.source")[tine.SECTION][tine.DEV],
            "packages/a.source, packages/b.source",
        )

    def test_the_projects_dev_targets_are_kept_beside_the_mounted_ones(self) -> None:
        (self.root / ".buckconfig").write_text(
            "[cells]\nroot = .\n[tine]\ndev = tools/app, packages/a.source\n"
        )
        self.assertEqual(
            self.build_mounted("packages/a.source")[tine.SECTION][tine.DEV], "tools/app, packages/a.source"
        )

    def test_without_mounts_buck_reads_the_projects_dev_targets(self) -> None:
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\n[tine]\ndev = tools/app\n")
        self.assertNotIn(tine.DEV, self.build_mounted().get(tine.SECTION, {}))

    def test_local_dev_targets_would_drop_the_mounted_ones(self) -> None:
        (self.root / tine.LOCAL).write_text("[tine]\ndev = tools/app\n")
        with self.assertRaisesRegex(SystemExit, "would replace the mounted targets"):
            self.build_mounted("packages/a.source")
        self.assertNotIn(tine.DEV, self.build_mounted().get(tine.SECTION, {}))

    def test_completing_writes_no_shared_config(self) -> None:
        defaults = Path(__file__).with_name("tine.buckconfig")
        for cell in (".", "vendor/tine"):
            with self.subTest(cell=cell):
                config = self.root / ".buckconfig"
                config.write_text(
                    defaults.read_text()
                    if cell == "."
                    else tine.render_project_buckconfig(defaults, {"cells": {"tine": cell}})
                )
                before = config.stat()
                with (
                    self.running() as execve,
                    unittest.mock.patch.object(tine, "COMMAND_PATH", wrapper_command(self.root / cell)),
                ):
                    tine.buck_command(["complete", "--target=//x"])
                self.assertTrue(execve)
                self.assertFalse((self.root / tine.LOCAL).exists())
                after = config.stat()
                self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
                self.assertEqual(
                    tine.read_project_buckconfig(self.root).get("project", {}).get(tine.PROJECT_IGNORE),
                    ", ".join(tine.VCS_IGNORES),
                )

    def test_completing_runs_in_its_own_isolation_dir(self) -> None:
        """A keypress must never queue on the daemon-less slot of a running build."""
        with unittest.mock.patch.dict(os.environ, {"BUCK_ISOLATION_DIR": "elsewhere"}):
            with self.running() as execve:
                tine.buck_command(["complete", "--target=//x"])
        self.assertEqual(
            execve[1],
            [
                str(self.binary),
                "--no-buckd",
                tine.FLAG_ISOLATION,
                tine.ISOLATION_COMPLETE,
                "complete",
                "--target=//x",
            ],
        )

    def test_completing_respects_a_callers_own_isolation_dir(self) -> None:
        with self.running() as execve:
            tine.buck_command([tine.FLAG_ISOLATION, "x", "complete", "--target=//x"])
        self.assertEqual(
            execve[1],
            [str(self.binary), "--no-buckd", tine.FLAG_ISOLATION, "x", "complete", "--target=//x"],
        )

    def test_tine_cell_uses_its_own_gitignores(self) -> None:
        isolate_git(self)
        checkout = self.root / "vendor" / "tine"
        checkout.mkdir(parents=True)
        self.enterContext(unittest.mock.patch.object(tine, "COMMAND_PATH", wrapper_command(checkout)))
        git("init", "--quiet", "--initial-branch=main", cwd=checkout)
        (self.root / ".buckconfig").write_text("[cells]\nroot = .\ntine = vendor/tine\n")
        (checkout / ".buckconfig").write_text("[cells]\ntine = .\n")
        (checkout / ".gitignore").write_text("*.generated\n!kept.generated\nBUCK\nBUCK.v2\n")
        (checkout / "ignored.generated").touch()
        (checkout / "kept.generated").touch()
        (checkout / "tracked.generated").touch()
        git("add", "--force", "tracked.generated", cwd=checkout)
        (checkout / "BUCK").touch()
        (checkout / "BUCK.v2").touch()

        with self.running():
            tine.buck_command(["complete", "--target=tine//"])
        self.assertFalse((checkout / tine.LOCAL).exists())

        with self.running():
            tine.buck_command(["build", "tine//..."])
        self.assertEqual(
            read_generated_buckconfig(checkout / tine.LOCAL)["project"][tine.PROJECT_IGNORE],
            ", ".join([*tine.VCS_IGNORES, "ignored.generated"]),
        )
        self.assertEqual(
            read_generated_buckconfig(self.root / tine.LOCAL)["project"][tine.PROJECT_IGNORE],
            ", ".join(tine.VCS_IGNORES),
        )

        (checkout / "ignored.generated").unlink()
        with self.running():
            tine.buck_command(["build", "tine//..."])
        self.assertEqual(
            read_generated_buckconfig(checkout / tine.LOCAL)["project"][tine.PROJECT_IGNORE],
            ", ".join(tine.VCS_IGNORES),
        )
        self.assertNotIn("project", tine.read_project_buckconfig(checkout))

    def test_tine_as_root_is_refreshed_once(self) -> None:
        for cell in (".", str(self.root)):
            with self.subTest(cell=cell):
                (self.root / ".buckconfig").write_text(f"[cells]\ntine = {cell}\n")
                with (
                    self.running(),
                    unittest.mock.patch.object(tine, "COMMAND_PATH", wrapper_command(self.root)),
                    unittest.mock.patch.object(tine, "refresh_local_buckconfig") as refresh,
                ):
                    tine.buck_command(["build", "tine//..."])
                refresh.assert_called_once_with(self.root, list(tine.VCS_IGNORES), None, [])

    def test_a_run_without_a_home_gets_one(self) -> None:
        environment = dict(os.environ)
        environment.pop("HOME", None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with self.running() as execve:
                tine.buck_command(["build", "//x"])
            self.assertTrue(execve)
            self.assertEqual(os.environ["HOME"], str(self.root / tine.HOME))
        self.assertTrue((self.root / tine.HOME).is_dir())

    def test_a_run_with_a_home_keeps_it(self) -> None:
        home = scratch(self, "tine-home.")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
            with self.running():
                tine.buck_command(["build", "//x"])
            self.assertEqual(os.environ["HOME"], str(home))
        self.assertFalse((self.root / tine.HOME).exists())


CELL_BUCKCONFIG = """# Standalone root cell. Consuming projects supply their own; see README.md.

[cells]
# Named `tine`, not `root`, so same-cell labels read `tine//...` as in a consuming project.
tine = .
catalog = catalog
prelude = prelude
none = none

[cell_aliases]
config = prelude
toolchains = tine
# Satisfy the bundled prelude's internal platform alias.
fbsource = none

[external_cells]
prelude = bundled

[parser]
target_platform_detector_spec = target:tine//...->tine//platform:default \\
    target:catalog//...->tine//platform:default

[build]
execution_platforms = tine//platform:default

[custom]
continued = one, \\
    two, three

[buck2]
defer_write_actions = true
"""


class TestRenderProjectBuckconfig(unittest.TestCase):
    """What a project's configuration takes from the cell's own, and what it decides for itself."""

    def written(self, root: Path | None = None) -> str:
        source = scratch(self) / ".buckconfig"
        source.write_text(CELL_BUCKCONFIG)
        return tine.render_project_buckconfig(source, overrides(root or scratch(self)))

    def config(self, cell: str = "tine") -> dict[str, dict[str, str]]:
        root = scratch(self, "tine-test-project.")
        configure_cells(root, cell)
        (root / ".buckconfig").write_text(self.written(root))
        return tine.read_project_buckconfig(root)

    def test_the_cell_is_a_path_in_the_project_rather_than_its_root(self) -> None:
        cells = self.config("vendor/tine")["cells"]
        self.assertEqual(cells["root"], ".")
        self.assertEqual(cells["tine"], "vendor/tine")
        # Cells the project has no say in are the cell's own, and the toolchain is reached through an
        # alias to this cell rather than a cell of the project's.
        self.assertEqual(cells["prelude"], "prelude")
        self.assertEqual(self.config("vendor/tine")["cell_aliases"]["toolchains"], "tine")

    def test_the_project_gets_a_platform_for_its_own_targets_too(self) -> None:
        detector = self.config()["parser"][tine.DETECTOR].split()
        # This cell's platform, not the prelude's: that is what gives the project the shared cache.
        self.assertEqual(
            detector,
            [f"target:{cell}//...->tine//platform:default" for cell in ("root", "tine", "catalog")],
        )

    def test_a_project_without_a_catalog_gets_tine_s(self) -> None:
        self.assertEqual(self.config("vendor/tine")["cells"]["catalog"], "vendor/tine/catalog")

    def test_a_project_catalog_replaces_tine_s(self) -> None:
        root = scratch(self, "tine-test-project.")
        configure_cells(root, "vendor/tine").write_text(
            '[buckconfig.cells]\nroot = "."\ntine = "vendor/tine"\ncatalog = "os/catalog"\n'
        )
        (root / ".buckconfig").write_text(self.written(root))
        self.assertEqual(tine.read_project_buckconfig(root)["cells"]["catalog"], "os/catalog")

    def test_the_project_runs_actions_on_the_cell_s_platform(self) -> None:
        # Copied rather than rewritten, and it is what gives the project the shared cache.
        self.assertEqual(self.config()["build"]["execution_platforms"], "tine//platform:default")

    def test_defaults_and_continued_values_are_copied(self) -> None:
        written = self.written()
        for kept in ("fbsource = none", "one, two, three", "prelude = bundled"):
            self.assertIn(kept, written)
        # A value written across lines stays one, which is what Buck2 reads it as.
        self.assertEqual(
            self.config()["custom"]["continued"],
            "one, two, three",
        )

    def test_what_the_cell_says_about_being_one_is_left_behind(self) -> None:
        written = self.written()
        self.assertNotIn("Standalone root cell", written)
        self.assertNotIn("Named `tine`, not `root`", written)
        self.assertIn("@generated by `tine`", written.splitlines()[0])


class TestRefreshProjectBuckconfig(unittest.TestCase):
    """Generated defaults follow the checkout while project cells survive updates."""

    @override
    def setUp(self) -> None:
        self.root = scratch(self)
        self.cell = self.root / "vendor" / "tine"
        self.cell.mkdir(parents=True)
        self.enterContext(unittest.mock.patch.object(tine, "COMMAND_PATH", wrapper_command(self.cell)))
        self.source = self.cell / ".buckconfig"
        self.source.write_text(CELL_BUCKCONFIG)
        self.path = self.root / ".buckconfig"
        self.cells = configure_cells(self.root, "vendor/tine")
        self.path.write_text(tine.render_project_buckconfig(self.source, overrides(self.root)))
        self.updated = CELL_BUCKCONFIG.replace("defer_write_actions = true", "materializations = deferred")

    def refresh(self) -> None:
        refresh_project(self.root)

    def config(self) -> dict[str, dict[str, str]]:
        return tine.read_project_buckconfig(self.root)

    def test_updates_defaults_and_preserves_project_cells(self) -> None:
        self.cells.write_text(
            self.cells.read_text().replace('root = "."', '# Project cells\nroot = "."\napp = "app"')
        )
        self.source.write_text(self.updated)
        self.refresh()
        config = self.config()
        self.assertEqual(config["buck2"], {"materializations": "deferred"})
        self.assertEqual(config["cells"]["app"], "app")
        self.assertEqual(config["cells"]["tine"], "vendor/tine")
        self.assertIn('# Project cells\nroot = "."\napp = "app"', self.cells.read_text())
        self.assertIn("target:root//...->", config["parser"][tine.DETECTOR])

    def test_native_cell_declarations_select_and_survive_refresh(self) -> None:
        self.source.write_text(self.updated)
        for materializations in (None, "all"):
            with self.subTest(materializations=materializations):
                self.path.write_text("[cells]\nroot = .\ntine = vendor/tine\napp = app\n")
                self.cells.write_text(
                    f'[buckconfig.buck2]\nmaterializations = "{materializations}"\n'
                    if materializations is not None
                    else ""
                )
                self.refresh()
                self.refresh()
                config = self.config()
                self.assertEqual(config["cells"]["tine"], "vendor/tine")
                self.assertEqual(config["cells"]["app"], "app")
                self.assertEqual(config["buck2"], {"materializations": materializations or "deferred"})

    def test_a_moved_checkout_takes_its_catalog_along(self) -> None:
        moved = self.root / "third-party" / "tine"
        moved.mkdir(parents=True)
        (moved / ".buckconfig").write_text(CELL_BUCKCONFIG)
        configure_cells(self.root, "third-party/tine")
        self.enterContext(unittest.mock.patch.object(tine, "COMMAND_PATH", wrapper_command(moved)))
        self.refresh()
        self.assertEqual(self.config()["cells"]["catalog"], "third-party/tine/catalog")

    def test_native_cell_declarations_keep_a_project_catalog(self) -> None:
        self.cells.write_text("")
        self.path.write_text("[cells]\nroot = .\ntine = vendor/tine\ncatalog = os/catalog\n")
        self.refresh()
        self.assertEqual(self.config()["cells"]["catalog"], "os/catalog")

    def test_unchanged_defaults_do_not_rewrite_the_file(self) -> None:
        before = self.path.stat()
        self.refresh()
        self.refresh()
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_checkout_generated_block_is_not_copied_into_project_defaults(self) -> None:
        self.source.write_text(
            CELL_BUCKCONFIG + f"{tine.BLOCK_BEGIN}\n[tine]\nmounted = leftover\n{tine.BLOCK_END}\n"
        )
        self.refresh()
        self.assertNotIn("mounted", self.config().get("tine", {}))

    def test_mount_add_does_not_require_checkout_defaults(self) -> None:
        self.source.unlink()
        checkout = scratch(self)
        tine.mount_command(self.root, ["add", str(self.cell), str(checkout)])
        self.assertEqual(tine.local_mounts(self.root), {"vendor/tine": str(checkout)})

    def test_missing_defaults_do_not_replace_the_project_config(self) -> None:
        before = self.path.read_bytes()
        self.source.unlink()
        with self.assertRaisesRegex(SystemExit, "cannot read.*buckconfig"):
            self.refresh()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unreadable_defaults_do_not_replace_the_project_config(self) -> None:
        before = self.path.read_bytes()
        self.source.unlink()
        self.source.mkdir()
        with self.assertRaisesRegex(SystemExit, "cannot read.*buckconfig"):
            self.refresh()
        self.assertEqual(self.path.read_bytes(), before)

    def test_retired_defaults_are_removed(self) -> None:
        self.source.write_text(self.updated)
        self.refresh()
        self.assertNotIn("defer_write_actions", self.config()["buck2"])

    def test_toml_overrides_defaults_and_removed_overrides_disappear(self) -> None:
        original = self.cells.read_text()
        self.cells.write_text(original + '\n[buckconfig.buck2]\nmaterializations = "all"\n')
        self.source.write_text(self.updated)
        self.refresh()
        self.assertEqual(self.config()["buck2"]["materializations"], "all")
        self.cells.write_text(original)
        self.refresh()
        self.assertEqual(self.config()["buck2"]["materializations"], "deferred")
        self.assertEqual(self.cells.read_text(), original)

    def test_reexecuted_wrapper_refreshes_shared_defaults(self) -> None:
        self.source.write_text(self.updated)
        with (
            unittest.mock.patch.object(tine, "namespace_mount_targets", return_value=["vendor/tine"]),
            unittest.mock.patch.object(os, "execve") as handover,
        ):
            tine.prepare_buck(self.root, tine.project_settings(self.root), ["buck", "build"])
        handover.assert_not_called()
        self.assertEqual(self.config()["buck2"], {"materializations": "deferred"})

    def test_handover_precedes_reading_defaults(self) -> None:
        self.source.unlink()
        declare(self.root, {"vendor/tine": str(scratch(self))})
        before = self.path.read_bytes()
        with (
            unittest.mock.patch.object(
                tine, "reexec_configured_wrapper", side_effect=SystemExit("handed over")
            ),
            self.assertRaisesRegex(SystemExit, "handed over"),
        ):
            tine.prepare_buck(self.root, tine.project_settings(self.root), ["buck", "build"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_standalone_config_is_not_rewritten(self) -> None:
        self.cells.unlink()
        for cell in (".", "./", str(self.root)):
            with self.subTest(cell=cell):
                content = f"[cells]\ntine = {cell}"
                self.path.write_text(content)
                before = self.path.stat()
                self.refresh()
                self.assertEqual(self.path.read_text(), content)
                self.assertEqual(self.path.stat().st_ino, before.st_ino)
                self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_standalone_check_uses_local_cell_override(self) -> None:
        (self.root / tine.LOCAL).write_text("[cells]\ntine = .\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(SystemExit, "set standalone Buck overrides in .buckconfig"):
            self.refresh()
        self.assertEqual(self.path.read_bytes(), before)

    def test_edits_to_bootstrap_do_not_override_rendered_defaults(self) -> None:
        content = "[cells]\nroot = .\ntine = vendor/tine\n[buck2]\ndefer_write_actions = false\n"
        self.path.write_text(content)
        self.source.write_text(self.updated)
        self.refresh()
        self.assertEqual(
            self.path.read_text(), tine.render_project_buckconfig(self.source, overrides(self.root))
        )
        self.assertEqual(self.config()["buck2"], {"materializations": "deferred"})

    def test_local_overrides_survive(self) -> None:
        local = self.root / tine.LOCAL
        content = "[buck2]\nmaterializations = immediate\n"
        local.write_text(content)
        self.source.write_text(self.updated)
        self.refresh()
        self.assertEqual(self.config()["buck2"]["materializations"], "immediate")
        self.assertEqual(local.read_text(), content)

    def test_local_cell_selects_the_defaults_without_reading_the_toml_cell(self) -> None:
        selected = self.root / "other"
        selected.mkdir()
        (selected / ".buckconfig").write_text(self.updated)
        local = self.root / tine.LOCAL
        local.write_text("[cells]\ntine = other\n")
        self.source.unlink()
        bootstrap = self.path.read_text()
        for content in (self.cells.read_text(), ""):
            with self.subTest(toml=content):
                self.path.write_text(bootstrap)
                self.cells.write_text(content)
                self.refresh()
                self.assertEqual(self.config()["cells"]["tine"], "other")
                self.assertEqual(self.config()["buck2"], {"materializations": "deferred"})
                self.assertEqual(tine.parse_buckconfig(self.path)["cells"]["tine"], "vendor/tine")
                self.assertEqual(self.cells.read_text(), content)
                self.assertEqual(local.read_text(), "[cells]\ntine = other\n")

    def test_toml_overrides_still_apply_to_the_locally_selected_defaults(self) -> None:
        selected = self.root / "other"
        selected.mkdir()
        (selected / ".buckconfig").write_text(self.updated)
        (self.root / tine.LOCAL).write_text("[cells]\ntine = other\n")
        self.cells.write_text(self.cells.read_text() + '\n[buckconfig.buck2]\nmaterializations = "all"\n')

        self.refresh()

        self.assertEqual(self.config()["buck2"], {"materializations": "all"})

    def test_cell_path_changes_in_toml_override_the_previous_output(self) -> None:
        new = self.root / "new"
        new.mkdir()
        (new / ".buckconfig").write_text(self.updated)
        configure_cells(self.root, "new")
        self.assertIn("new", tine.configured_mount_targets(self.root))
        self.assertNotIn("vendor/tine", tine.configured_mount_targets(self.root))
        self.refresh()
        config = self.config()
        self.assertEqual(config["cells"]["tine"], "new")
        self.assertEqual(config["buck2"], {"materializations": "deferred"})
        self.assertIn("new", tine.configured_mount_targets(self.root))
        self.assertNotIn("vendor/tine", tine.configured_mount_targets(self.root))

    def test_toml_project_ignores_are_merged_with_generated_ignores(self) -> None:
        self.cells.write_text(self.cells.read_text() + '\n[buckconfig.project]\nignore = "custom"\n')
        self.refresh()
        self.assertEqual(
            tine.collect_project_ignores(self.root, self.config(), [], {}), ["custom", *tine.VCS_IGNORES]
        )

    def test_completion_does_not_refresh_defaults(self) -> None:
        before = self.path.read_bytes()
        self.source.write_text(self.updated)
        with (
            contextlib.chdir(self.root),
            unittest.mock.patch.dict(os.environ, {"BUCK2_BINARY": str(self.root / "buck2")}),
            unittest.mock.patch.object(tine, "ensure_buck2_binary", return_value=True),
            unittest.mock.patch.object(os, "execv"),
        ):
            tine.buck_command(["complete"])
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse((self.root / tine.LOCAL).exists())

    def test_completion_script_does_not_refresh_defaults(self) -> None:
        before = self.path.read_bytes()
        self.source.write_text(self.updated)
        with (
            contextlib.chdir(self.root),
            unittest.mock.patch.object(tine, "ensure_buck2_binary", return_value=True),
            unittest.mock.patch.object(tine, "_buck2_completion_script", return_value="script"),
            unittest.mock.patch.object(tine, "rewrite_completion_script", return_value="rendered"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            tine.completion_command(["bash"])
        self.assertEqual(output.getvalue(), "rendered")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / tine.LOCAL).exists())

    def test_standalone_overrides_are_not_silently_discarded(self) -> None:
        self.path.write_text("[cells]\nroot = .\n")
        for cell in (None, ".", str(self.root)):
            with self.subTest(cell=cell):
                configured = {"buck2": {"materializations": "eager"}, "custom": {"key": "value"}}
                if cell is not None:
                    configured["cells"] = {"tine": cell}
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SystemExit, "set standalone Buck overrides in .buckconfig"):
                    tine.refresh_project_buckconfig(self.root, configured)
                self.assertEqual(self.path.read_bytes(), before)


class TestInit(unittest.TestCase):
    """Tests for `init`. The checkout is a directory with a `tine.lock.json` and a `.buckconfig`."""

    @override
    def setUp(self) -> None:
        isolate_git(self)
        self.into = scratch(self, "tine-test-project.")
        self.checkout = self.into / "tine"
        pins(self.checkout)
        (self.checkout / ".buckconfig").write_text(CELL_BUCKCONFIG)

    def init(self, *arguments: str) -> dict[str, dict[str, str]]:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.init_command(self.into, list(arguments))
        return tine.read_project_buckconfig(self.into)

    def test_it_writes_a_project_buck_can_be_run_in(self) -> None:
        self.assertEqual(self.init("tine")["cells"]["tine"], "tine")
        self.assertEqual(
            sorted(p.name for p in self.into.iterdir()),
            [".buckconfig", ".gitignore", "tine", "tine.toml"],
        )
        self.assertEqual(
            (self.into / tine.CONFIG).read_text(), '[buckconfig.cells]\nroot = "."\ntine = "tine"\n'
        )
        gitignore = (self.into / ".gitignore").read_text()
        self.assertIn("/buck-out\n", gitignore)
        self.assertIn("target/\n", gitignore)
        self.assertIn(f"/{tine.LOCAL}\n", gitignore)
        # The name an interrupted write leaves behind, which is nobody's to commit either.
        self.assertIn(f"/{tine.LOCAL}.*.tmp\n", gitignore)
        self.assertIn(f"/{tine.LOCAL_SETTINGS}\n", gitignore)
        # Covers the mount file tine writes under it.
        self.assertIn(f"/{tine.HOME}\n", gitignore)

    def test_the_checkout_it_is_part_of_is_the_one_it_writes(self) -> None:
        with unittest.mock.patch.object(tine, "wrapper_cell_root", return_value=self.checkout):
            self.assertEqual(self.init()["cells"]["tine"], "tine")

    def test_a_checkout_outside_the_project(self) -> None:
        with self.assertRaisesRegex(SystemExit, "cannot be a cell of this project"):
            self.init(str(scratch(self)))
        self.assertFalse((self.into / ".buckconfig").exists())

    def test_a_directory_that_is_no_checkout_of_the_cell(self) -> None:
        (self.into / "elsewhere").mkdir()
        with self.assertRaisesRegex(SystemExit, "no checkout of the tine cell"):
            self.init("elsewhere")

    def test_it_refuses_to_overwrite_a_project(self) -> None:
        (self.into / ".buckconfig").write_text("")
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.init("tine")

    def test_it_leaves_a_file_the_project_already_has(self) -> None:
        (self.into / ".gitignore").write_text("mine\n")
        self.init("tine")
        self.assertEqual((self.into / ".gitignore").read_text(), "mine\n")

    def test_init_preserves_existing_commands_pin_and_overrides(self) -> None:
        settings = self.into / tine.CONFIG
        original = (
            '[commands.check]\nsteps = [["buck", "test", "tine//..."]]\n'
            '[buck2]\nrelease = "pin"\n'
            '[buckconfig.buck2]\nmaterializations = "all"\n'
        )
        settings.write_text(original)
        self.assertEqual(self.init("tine")["buck2"]["materializations"], "all")
        self.assertTrue(settings.read_text().startswith(original))
        self.assertEqual(tine.project_settings(self.into)["buck2"], {"release": "pin"})

    def test_init_keeps_existing_cell_overrides(self) -> None:
        settings = configure_cells(self.into, "tine")
        original = settings.read_text() + 'app = "app"\n'
        settings.write_text(original)
        self.assertEqual(self.init("tine")["cells"]["app"], "app")
        self.assertEqual(settings.read_text(), original)

    def test_init_rejects_conflicting_cell_overrides(self) -> None:
        settings = configure_cells(self.into, "other")
        original = settings.read_text()
        with self.assertRaisesRegex(SystemExit, "must set tine"):
            self.init("tine")
        self.assertEqual(settings.read_text(), original)
        self.assertFalse((self.into / ".buckconfig").exists())

    def test_a_gitignore_it_may_not_write_is_excluded_in_the_checkout_instead(self) -> None:
        git("init", "--quiet", cwd=self.into)
        (self.into / ".gitignore").write_text("mine\n")
        self.init("tine")
        excluded = (self.into / ".git" / "info" / "exclude").read_text()
        for entry in tine.GITIGNORE.split():
            self.assertIn(f"{entry}\n", excluded)
        # An exclude nobody reads must not hide a source directory that happens to have this name.
        self.assertNotIn("target/", excluded)

    def test_it_takes_one_path(self) -> None:
        # `--help` is argparse's to answer, and neither it nor a second path writes a project.
        for arguments in (["one", "two"], ["--help"], ["--nope"]):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.init(*arguments)
        self.assertFalse((self.into / ".buckconfig").exists())


class TestGitExcludes(RepositoryTestCase):
    """What a `.gitignore` this command may not write says, said where only the checkout reads it."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.repo
        self.path = self.checkout / ".git" / "info" / "exclude"

    def exclude(self, entries: str = tine.GITIGNORE) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.update_git_excludes(self.checkout, entries)
        return self.path.read_text()

    def test_what_the_file_already_says_is_kept(self) -> None:
        self.path.write_text("theirs\n")
        self.assertTrue(self.exclude().startswith("theirs\n"))

    def test_an_entry_it_already_has_is_not_added_twice(self) -> None:
        self.exclude()
        before = self.path.read_text()
        self.assertEqual(self.exclude(), before)

    def test_the_entries_it_is_missing_are_the_ones_added(self) -> None:
        self.path.write_text("/buck-out\n")
        added = self.exclude("/buck-out\n/elsewhere\n")
        self.assertEqual(added.count("/buck-out\n"), 1)
        self.assertIn("/elsewhere\n", added)

    def test_a_worktree_is_excluded_in_the_checkout_it_belongs_to(self) -> None:
        # `.git` is a file there, and the exclude file is the one the whole repository shares.
        self.commit()
        worktree = scratch(self) / "tree"
        git("worktree", "add", "--quiet", str(worktree), "-b", "other", cwd=self.checkout)
        with contextlib.redirect_stderr(io.StringIO()):
            tine.update_git_excludes(worktree, "/buck-out\n")
        self.assertIn("/buck-out\n", self.path.read_text())

    def test_a_directory_that_is_not_a_checkout(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            tine.update_git_excludes(scratch(self), tine.GITIGNORE)


class TestProjectRoot(unittest.TestCase):
    def test_the_furthest_ancestor_wins(self) -> None:
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        (root / "nested").mkdir()
        (root / "nested" / ".buckconfig").write_text("")
        self.assertEqual(tine.project_root(root / "nested"), root)

    def test_buckroot_stops_the_search(self) -> None:
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        (root / "nested").mkdir()
        (root / "nested" / ".buckconfig").write_text("")
        (root / "nested" / ".buckroot").write_text("")
        self.assertEqual(tine.project_root(root / "nested"), root / "nested")

    def test_the_home_directory_is_never_the_root(self) -> None:
        # `~/.buckconfig` is a configuration override, which Buck refuses to take for a project.
        home = scratch(self)
        (home / ".buckconfig").write_text("")
        (home / "project").mkdir()
        (home / "project" / ".buckconfig").write_text("")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(tine.project_root(home / "project"), home / "project")

    def test_an_unset_home_skips_no_directory(self) -> None:
        # Buck reads `$HOME` and nothing else, so with it unset there is no home directory to skip,
        # whatever `Path.home()` falls back to.
        root = scratch(self)
        (root / ".buckconfig").write_text("")
        environment = dict(os.environ)
        environment.pop("HOME", None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with unittest.mock.patch.object(tine.Path, "home", return_value=root):
                self.assertEqual(tine.project_root(root), root)

    def test_no_buckconfig_at_all(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no .buckconfig"):
            tine.project_root(scratch(self))


class TestRewriteCompletionScript(unittest.TestCase):
    """The rewriting of Buck2's script, against the anchors this reaches into it for."""

    # What the real script says where each rewrite lands; the rest of it is passed through.
    FISH = """# @generated by buck2
complete -c buck2 -n "__fish_buck2_needs_command" -f -a build
complete -c buck -w buck2
function __fish_buck2_needs_command
    set -l cmd (commandline -opc)
    set -e cmd[1]
    argparse -s (__fish_buck2_global_optspecs)
end
function __buck2_add_target_completions
    set -l subcommand (__buck2_subcommand $cmd)
    buck2 complete --target="$cur"
end
"""
    BASH = """# @generated by buck2
_BUCK_COMPLETE_BIN="${_BUCK_COMPLETE_BIN:-buck2}"
    complete -F _buck2 -o bashdefault -o default buck2
    complete -F __buck2_fix -o bashdefault -o default buck
"""
    ZSH = """#compdef buck2 buck
# @generated by buck2
_buck2() { _describe commands ;}
if [ "$funcstack[1]" = "_buck2" ]; then
    _buck2 "$@"
else
    compdef _buck2 buck2
fi
compdef -d buck2

_BUCK_COMPLETE_BIN="${_BUCK_COMPLETE_BIN:-buck2}"
compdef __buck2_fix buck buck2
"""

    def scripts(self) -> dict[str, str]:
        return {
            shell: tine.rewrite_completion_script(getattr(self, shell.upper()), shell)
            for shell in tine.SHELLS
        }

    def test_every_verb_is_completed_by_every_shell(self) -> None:
        # Generated from VERBS, so a verb added there cannot be missed by a shell.
        with unittest.mock.patch.dict(tine.VERBS, {"newverb": "something else it does"}):
            scripts = self.scripts()
        for shell, script in scripts.items():
            self.assertNotIn(tine.VERBS_MARKER, script, shell)
            for verb in (*tine.VERBS, "newverb"):
                self.assertIn(verb, script, shell)

    def test_nothing_it_emits_still_names_buck2(self) -> None:
        # A surviving name completes against whatever buck2 the shell finds, the `buck` command
        # Buck2 registers for included.
        scripts = self.scripts()
        for shell, script in scripts.items():
            # `_tine_buck2` is what Buck2's own completer is called once this is done with it.
            self.assertNotIn("buck2", script.replace("_tine_buck2", ""), shell)
        self.assertNotIn("default buck\n", scripts["bash"])

    def test_target_completion_routes_back_through_this_command(self) -> None:
        # Left alone it would run whatever `buck2` is on PATH, against unwrapped configuration.
        for shell, script in self.scripts().items():
            self.assertIn("tine buck complete" if shell == "fish" else "(tine buck)", script, shell)

    def test_zsh_autoloads_this_command_rather_than_buck2(self) -> None:
        # `_tine` is the name a shell autoloads the file under, so it has to be the completer that
        # knows this command's verbs; Buck2's own entry point cannot stay where it sits either.
        script = tine.rewrite_completion_script(self.ZSH, "zsh")
        self.assertEqual(script.count('if [ "$funcstack[1]" = "_tine" ]; then'), 1)
        self.assertIn('if [ "$funcstack[1]" = "_tine" ]; then\n    _tine "$@"', script)
        self.assertIn("_tine() {\n    local context state state_descr line", script)
        self.assertIn("_tine_buck2", script)
        # Buck2 unregisters the command before registering its own completer; both have to go.
        self.assertNotIn("compdef -d", script)

    def test_descriptions_are_quoted_the_way_each_shell_reads_them(self) -> None:
        description = r"report the project's health: \fast"
        configured = tine.validate_commands(
            {"report": {"description": description, "steps": [["buck", "build", "//..."]]}}
        )
        self.assertIn(
            r"-d 'report the project\'s health: \\fast'",
            tine.rewrite_completion_script(self.FISH, "fish", configured),
        )
        self.assertIn(
            r"'report:report the project'\''s health\: \\fast'",
            tine.rewrite_completion_script(self.ZSH, "zsh", configured),
        )

    def test_an_anchor_that_is_no_longer_there_is_reported(self) -> None:
        # Rewriting nothing would emit a script that silently completes nothing.
        for shell in tine.SHELLS:
            with self.assertRaisesRegex(SystemExit, "the pin moved under it"):
                tine.rewrite_completion_script("# @generated by buck2\n", shell)

    def test_project_commands_are_completed_by_every_shell(self) -> None:
        configured = tine.validate_commands(
            {
                "smoke": {
                    "description": "run the smoke tests",
                    "steps": [["buck", "test", "//:smoke"]],
                }
            }
        )
        for shell, script in {
            shell: tine.rewrite_completion_script(getattr(self, shell.upper()), shell, configured)
            for shell in tine.SHELLS
        }.items():
            self.assertIn("smoke", script, shell)
        for shell in ("fish", "zsh"):
            self.assertIn(
                "run the smoke tests",
                tine.rewrite_completion_script(getattr(self, shell.upper()), shell, configured),
            )


class TestRunStep(unittest.TestCase):
    def process(self) -> unittest.mock.MagicMock:
        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        return process

    def test_forwards_termination_and_restores_the_handler(self) -> None:
        process = self.process()
        previous = signal.getsignal(signal.SIGTERM)

        def wait() -> int:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            cast(collections.abc.Callable[[int, object], object], handler)(signal.SIGTERM, None)
            # A child may handle the signal and exit cleanly; the chain must still stop.
            return 0

        process.wait.side_effect = wait
        with unittest.mock.patch("subprocess.Popen", return_value=process):
            self.assertEqual(tine.run_step(["helper"]), -signal.SIGTERM)
        process.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_forwards_an_interrupt_without_a_traceback(self) -> None:
        process = self.process()
        process.wait.side_effect = [KeyboardInterrupt, -signal.SIGINT]
        with unittest.mock.patch("subprocess.Popen", return_value=process):
            self.assertEqual(tine.run_step(["helper"]), -signal.SIGINT)
        process.send_signal.assert_called_once_with(signal.SIGINT)


class TestProjectCommands(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.addCleanup(os.chdir, Path.cwd())

    def project(self, content: str) -> Path:
        root = scratch(self, "tine-test-project.")
        (root / ".buckconfig").write_text("[cells]\nroot = .\n")
        (root / tine.CONFIG).write_text(content)
        return root

    def test_steps_run_from_the_configured_directory_and_the_last_gets_the_callers_arguments(
        self,
    ) -> None:
        root = self.project(
            """[commands.build]
cwd = "work"
steps = [
  ["buck", "build", "//:artifacts"],
  ["buck", "test", "//:sysext-duplicates"],
]
"""
        )
        work = root / "work"
        work.mkdir()
        nested = root / "nested"
        nested.mkdir()

        def run_step(_argv: list[str]) -> int:
            self.assertEqual(Path.cwd(), work)
            return 0

        def execv(_executable: str, _argv: list[str]) -> None:
            self.assertEqual(Path.cwd(), work)

        with (
            unittest.mock.patch.object(tine, "cwd", return_value=nested),
            unittest.mock.patch.object(tine, "run_step", side_effect=run_step) as run,
            unittest.mock.patch.object(os, "execv", side_effect=execv) as execute,
        ):
            tine.main(["build", "--show-output"])
        executable = str(tine.COMMAND_PATH.absolute())
        run.assert_called_once_with([executable, "buck", "build", "//:artifacts"])
        execute.assert_called_once_with(
            executable,
            [
                executable,
                "buck",
                "test",
                "//:sysext-duplicates",
                "--show-output",
            ],
        )

    def test_a_failed_step_stops_the_command(self) -> None:
        root = self.project(
            """[commands.check]
steps = [
  ["buck", "test", "//:first"],
  ["buck", "test", "//:second"],
]
"""
        )
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            unittest.mock.patch.object(tine, "run_step", return_value=7) as run,
            self.assertRaises(SystemExit) as raised,
        ):
            tine.main(["check"])
        self.assertEqual(raised.exception.code, 7)
        run.assert_called_once()

    def test_a_step_killed_by_a_signal_uses_the_shell_exit_status(self) -> None:
        command = tine.validate_commands(
            {
                "check": {
                    "steps": [
                        ["buck", "first"],
                        ["buck", "second"],
                    ]
                }
            }
        )["check"]
        with (
            unittest.mock.patch.object(tine, "run_step", return_value=-signal.SIGTERM),
            self.assertRaises(SystemExit) as raised,
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)

    def test_a_final_step_execution_error_names_the_step(self) -> None:
        command = tine.validate_commands({"check": {"steps": [["buck", "build"]]}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "execv", side_effect=error),
            self.assertRaisesRegex(SystemExit, "cannot run command check step 1: absent"),
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])

    def test_steps_are_argument_arrays_not_shell_command_lines(self) -> None:
        configured = tine.validate_commands(
            {"query": {"steps": [["buck", "uquery", "attrfilter(labels, one, //...)", "$HOME"]]}}
        )
        self.assertEqual(
            configured["query"].body,
            (("buck", "uquery", "attrfilter(labels, one, //...)", "$HOME"),),
        )
        self.assertEqual(configured["query"].description, tine.PROJECT_COMMAND_DESCRIPTION)

    def test_a_multiline_script_receives_literal_caller_arguments(self) -> None:
        root = self.project(
            r"""[commands.inline]
script = '''
set -eu
pwd
printf "<%s>\n" "$0" "$@"
'''
"""
        )
        nested = root / "nested"
        nested.mkdir()
        proc = subprocess.run(
            [str(TOOL_PATH), "inline", "one two", "$(false)"],
            cwd=nested,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(proc.stdout, f"{nested}\n<inline>\n<one two>\n<$(false)>\n")

    def test_a_script_failure_is_the_commands_exit_status(self) -> None:
        root = self.project('[commands.fail]\nscript = "exit 23"\n')
        proc = subprocess.run(
            [str(TOOL_PATH), "fail"],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 23)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_shell_execution_error_names_the_command(self) -> None:
        command = tine.validate_commands({"check": {"script": "true"}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "execvp", side_effect=error),
            self.assertRaisesRegex(SystemExit, "cannot run command check: absent"),
        ):
            tine.run_project_command(Path.cwd(), "check", command, [])

    def test_an_unavailable_configured_working_directory_stops_the_command(self) -> None:
        root = scratch(self)
        directory = root / "absent"
        command = tine.validate_commands({"check": {"cwd": "absent", "script": "true"}})["check"]
        error = FileNotFoundError("absent")
        with (
            unittest.mock.patch.object(os, "chdir", side_effect=error),
            unittest.mock.patch.object(os, "execvp") as execute,
            self.assertRaisesRegex(SystemExit, rf"command check from {re.escape(str(directory))}: absent"),
        ):
            tine.run_project_command(root, "check", command, [])
        execute.assert_not_called()

    def test_cwd_must_be_a_non_empty_relative_path(self) -> None:
        for directory in ("", "/elsewhere", "contains\0nul", 1):
            with (
                self.subTest(directory=directory),
                self.assertRaisesRegex(SystemExit, "cwd.*non-empty relative path"),
            ):
                tine.validate_commands({"broken": {"cwd": directory, "script": "true"}})

    def test_a_command_defines_exactly_one_body(self) -> None:
        for definition in (
            {},
            {
                "script": "true",
                "steps": [["buck", "build", "//..."]],
            },
        ):
            with (
                self.subTest(definition=definition),
                self.assertRaisesRegex(SystemExit, "must define exactly one of steps or script"),
            ):
                tine.validate_commands({"broken": definition})

    def test_scripts_must_be_non_empty_strings(self) -> None:
        for script in ("", " \n\t", 1):
            with self.subTest(script=script), self.assertRaisesRegex(SystemExit, "script.*non-empty string"):
                tine.validate_commands({"broken": {"script": script}})

    def test_a_toml_script_cannot_contain_nul(self) -> None:
        root = self.project(
            r"""[commands.broken]
script = "\u0000"
"""
        )
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            self.assertRaisesRegex(SystemExit, "script.*must not contain NUL"),
        ):
            tine.main(["broken"])

    def test_steps_must_be_non_empty_arrays_of_strings(self) -> None:
        invalid = (
            ({"steps": []}, "non-empty array"),
            ({"steps": [[]]}, "step 1.*non-empty array"),
            ({"steps": [["buck", 1]]}, "step 1.*only strings"),
            ({"steps": [["buck", "\0"]]}, "step 1.*must not contain NUL"),
        )
        for definition, message in invalid:
            with self.subTest(definition=definition), self.assertRaisesRegex(SystemExit, message):
                tine.validate_commands({"broken": definition})

    def test_command_names_are_safe_single_words(self) -> None:
        for name in ("two words", "$(command)", "-h"):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, "invalid command name"):
                tine.validate_commands({name: {"steps": [["buck", "build", "//..."]]}})

    def test_reserved_command_names_are_rejected(self) -> None:
        for name in ("buck", "help"):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, "reserved command name"):
                tine.validate_commands({name: {"steps": [["buck", "build", "//..."]]}})

    def test_only_buck_can_be_a_step(self) -> None:
        with self.assertRaisesRegex(SystemExit, "must start with buck, not mount"):
            tine.validate_commands({"broken": {"steps": [["mount", "list"]]}})

    def test_descriptions_must_be_non_empty_printable_strings(self) -> None:
        for description in ("", "   ", "two\nlines", 1):
            with (
                self.subTest(description=description),
                self.assertRaisesRegex(SystemExit, "description.*non-empty printable string"),
            ):
                tine.validate_commands(
                    {
                        "check": {
                            "description": description,
                            "steps": [["buck", "test", "//..."]],
                        }
                    }
                )

    def test_command_definitions_reject_unknown_keys(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: summary"):
            tine.validate_commands(
                {
                    "check": {
                        "summary": "build and test the image",
                        "steps": [["buck", "test", "//..."]],
                    }
                }
            )

    def test_help_lists_project_commands(self) -> None:
        root = self.project(
            """[commands.check]
description = "build and test the image"
steps = [["buck", "test", "//..."]]
"""
        )
        printed = io.StringIO()
        with (
            unittest.mock.patch.object(tine, "cwd", return_value=root),
            contextlib.redirect_stdout(printed),
        ):
            tine.main(["help"])
        # Padded to the longest name, so match the columns rather than one of their widths.
        self.assertRegex(printed.getvalue(), r"\n    check +build and test the image\n")


class TestMain(unittest.TestCase):
    def usage(self, *argv: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            tine.main(list(argv))
        return stdout.getvalue()

    def test_it_prints_what_it_can_do(self) -> None:
        for argv in ([], ["help"], ["-h"], ["--help"]):
            self.assertEqual(self.usage(*argv), tine.USAGE)

    def test_asking_a_verb_for_help_does_not_do_its_job(self) -> None:
        # `init --help` would otherwise take `--help` for the checkout to register.
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertRaises(SystemExit):
            tine.main(["init", "--help"])
        self.assertIn("usage: tine init", printed.getvalue())

    def test_box_quietly_runs_the_root_box_target(self) -> None:
        with unittest.mock.patch.object(tine, "buck_command") as buck:
            tine.main(["box", "--", "pytest", "-q"])
        buck.assert_called_once_with(["-v", "0", "run", "//:box", "--", "pytest", "-q"])

    def test_bump_updates_the_pins_of_the_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tine-test.", dir="/var/tmp") as directory:
            root = Path(directory)
            (root / "ty.toml").touch()
            with (
                unittest.mock.patch.object(tine, "project_root", return_value=root),
                unittest.mock.patch.object(tine, "prepare_buck") as prepare,
                unittest.mock.patch("bump.main") as main,
            ):
                (root / "pyproject.toml").touch()
                tine.main(["bump", "--all"])
                (root / "ty.toml").unlink()
                tine.main(["bump", "--tool", "ruff"])
                (root / "pyproject.toml").unlink()
                tine.main(["bump", "--tool", "go"])
        self.assertEqual(
            [call.args[2] for call in prepare.call_args_list],
            [["bump", "--all"], ["bump", "--tool", "ruff"], ["bump", "--tool", "go"]],
        )
        data = root / "tine.lock.json"
        self.assertEqual(
            [call.args for call in main.call_args_list],
            [
                (data, root / "ty.toml", ["--all"]),
                (data, root / "pyproject.toml", ["--tool", "ruff"]),
                (data, None, ["--tool", "go"]),
            ],
        )

    def test_no_such_command(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no such command: nope"):
            tine.main(["nope"])

    def test_completion_takes_a_shell_it_can_write(self) -> None:
        with self.assertRaisesRegex(SystemExit, "completion takes one of"):
            tine.main(["completion", "tcsh"])


class TestParseBuckCommand(unittest.TestCase):
    """What Buck2 will make of the arguments `tine buck` forwards, which decides the fast path."""

    def test_plain(self) -> None:
        self.assertEqual(tine.parse_buck_command(["build", "//x"]).subcommand, "build")

    def test_options_before_the_subcommand(self) -> None:
        self.assertEqual(
            tine.parse_buck_command(["-v", "0", "complete", "--target=x"]).subcommand, "complete"
        )

    def test_joined_option_values(self) -> None:
        command = tine.parse_buck_command(["--isolation-dir=x", "build", "//x"])
        self.assertEqual(command.subcommand, "build")
        self.assertEqual(command.isolation, "x")

    def test_separated_option_values(self) -> None:
        command = tine.parse_buck_command(["--isolation-dir", "x", "build", "//x"])
        self.assertEqual(command.subcommand, "build")
        self.assertEqual(command.isolation, "x")

    def test_no_subcommand(self) -> None:
        self.assertIsNone(tine.parse_buck_command(["--help"]).subcommand)
        self.assertIsNone(tine.parse_buck_command([]).subcommand)
        self.assertIsNone(tine.parse_buck_command(["--", "build"]).subcommand)

    def test_isolation_falls_back_to_bucks_environment_variable(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"BUCK_ISOLATION_DIR": "elsewhere"}):
            self.assertEqual(tine.parse_buck_command(["build", "//x"]).isolation, "elsewhere")
            self.assertEqual(tine.parse_buck_command(["--isolation-dir", "x", "build"]).isolation, "x")

    def test_option_separator(self) -> None:
        self.assertIsNone(
            tine.parse_buck_command(["run", "//x", "--", "--isolation-dir", "other"]).isolation
        )

    def test_a_callers_own_isolation_dir(self) -> None:
        """Only a flag in the vector itself is the caller's; the environment is a fallback."""
        self.assertTrue(tine.parse_buck_command(["--isolation-dir", "x", "complete"]).own_isolation)
        with unittest.mock.patch.dict(os.environ, {"BUCK_ISOLATION_DIR": "elsewhere"}):
            self.assertFalse(tine.parse_buck_command(["complete", "--target=//x"]).own_isolation)


class TestProjectIgnores(unittest.TestCase):
    """Mounted checkout build files and generated paths are hidden from project discovery."""

    @override
    def setUp(self) -> None:
        isolate_git(self)
        self.root = scratch(self, "tine-test-project.")

    def checkout(self, relative: str | None = None) -> Path:
        checkout = self.root / relative if relative is not None else scratch(self, "tine-test-checkout.")
        checkout.mkdir(parents=True, exist_ok=True)
        git("init", "--quiet", "--initial-branch=main", cwd=checkout)
        return checkout

    def ignores(self, config: dict[str, dict[str, str]], mounts: list[str]) -> list[str]:
        return tine.collect_project_ignores(self.root, config, mounts, {})

    def test_mount_build_files_are_added_to_project_ignores(self) -> None:
        self.assertEqual(
            self.ignores(
                {"cells": {"root": "."}},
                ["packages/demo/project", "packages/other/source"],
            ),
            [
                *tine.VCS_IGNORES,
                "packages/demo/project/BUCK",
                "packages/demo/project/**/BUCK",
                "packages/other/source/BUCK",
                "packages/other/source/**/BUCK",
            ],
        )

    def test_configured_project_ignores_are_preserved_without_duplicates(self) -> None:
        for mounts in ([], ["packages/project"]):
            with self.subTest(mounts=mounts):
                expected = ["vendor", *tine.VCS_IGNORES]
                if mounts:
                    expected += ["packages/project/BUCK", "packages/project/**/BUCK"]
                self.assertEqual(
                    self.ignores({"project": {tine.PROJECT_IGNORE: "vendor, **/.git, vendor,"}}, mounts),
                    expected,
                )

    def test_internal_exclusions_cover_each_supported_vcs(self) -> None:
        self.assertEqual(self.ignores({}, []), ["**/.git", "**/.jj", "**/.hg", "**/.svn"])

    def test_root_gitignores_apply_with_and_without_mounts(self) -> None:
        self.root = self.checkout()
        (self.root / ".gitignore").write_text("/build*\n*.generated\n!kept.generated\n")
        (self.root / "build[debug]").mkdir()
        (self.root / "tracked.generated").touch()
        git("add", "--force", "tracked.generated", cwd=self.root)
        (self.root / "kept.generated").touch()
        (self.root / "ignored.generated").touch()

        for mounts in ([], ["packages/project"]):
            with self.subTest(mounts=mounts):
                expected = [*tine.VCS_IGNORES, r"build\[debug]", "ignored.generated"]
                if mounts:
                    expected += ["packages/project/BUCK", "packages/project/**/BUCK"]
                self.assertEqual(self.ignores({}, mounts), expected)

    def test_native_project_ignores_are_preserved(self) -> None:
        for relative in (".buckconfig.d/ignores", ".buckconfig"):
            with self.subTest(layer=relative):
                root = scratch(self)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[project]\nignore = custom\n")
                self.assertEqual(
                    tine.collect_project_ignores(root, tine.read_project_buckconfig(root), [], {}),
                    ["custom", *tine.VCS_IGNORES],
                )

    def test_gitignored_build_files_remain_visible(self) -> None:
        for ignore_file in (".gitignore", ".git/info/exclude"):
            with self.subTest(ignore_file=ignore_file):
                self.root = self.checkout()
                (self.root / ignore_file).write_text("BUCK\nBUCK.v2\n*.generated\n/output\n")
                for relative in (
                    "BUCK",
                    "BUCK.v2",
                    "nested/BUCK",
                    "nested/BUCK.v2",
                    "ignored.generated",
                    "output/BUCK",
                ):
                    path = self.root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()

                for mounts in ([], ["packages/project"]):
                    with self.subTest(mounts=mounts):
                        expected = [*tine.VCS_IGNORES, "ignored.generated", "output"]
                        if mounts:
                            expected += ["packages/project/BUCK", "packages/project/**/BUCK"]
                        self.assertEqual(self.ignores({"cells": {"root": "."}}, mounts), expected)

    def test_project_ignores_can_hide_build_files(self) -> None:
        self.assertEqual(
            self.ignores({"project": {tine.PROJECT_IGNORE: "BUCK, **/BUCK.v2"}}, []),
            ["BUCK", "**/BUCK.v2", *tine.VCS_IGNORES],
        )

    def test_gitignored_source_mount_build_files_remain_hidden(self) -> None:
        checkout = self.checkout("packages/project")
        (checkout / ".gitignore").write_text("BUCK\nBUCK.v2\n")
        (checkout / "BUCK").touch()
        (checkout / "BUCK.v2").touch()

        self.assertEqual(
            self.ignores({}, ["packages/project"]),
            [
                *tine.VCS_IGNORES,
                "packages/project/BUCK",
                "packages/project/**/BUCK",
                "packages/project/BUCK.v2",
            ],
        )

    def test_a_local_project_ignore_cannot_override_root_gitignores(self) -> None:
        self.root = self.checkout()
        (self.root / ".gitignore").write_text("/generated\n")
        (self.root / "generated").mkdir()
        (self.root / tine.LOCAL).write_text(f"[project]\n{tine.PROJECT_IGNORE} = scratch\n")

        with self.assertRaisesRegex(SystemExit, "would replace the generated ignores"):
            self.ignores({}, [])

    def test_mount_path_is_literal_inside_the_ignore_glob(self) -> None:
        self.assertEqual(
            self.ignores({}, ["packages/[demo]*/project?"]),
            [
                *tine.VCS_IGNORES,
                r"packages/\[demo]\*/project\?/BUCK",
                r"packages/\[demo]\*/project\?/**/BUCK",
            ],
        )

    def test_gitignored_directories_are_added_to_project_ignores(self) -> None:
        checkout = self.checkout("packages/systemd.source")
        (checkout / ".gitignore").write_text("/build*\n/mkosi/mkosi.tools\n")
        (checkout / "build-debug").mkdir()
        mkosi = checkout / "mkosi"
        mkosi.mkdir()
        (mkosi / "tracked").write_text("")
        git("add", "mkosi/tracked", cwd=checkout)
        tools = mkosi / "mkosi.tools"
        tools.mkdir(parents=True)
        (tools / r"system-systemd\x2dcryptsetup.slice").write_text("")

        self.assertEqual(
            self.ignores({}, ["packages/systemd.source"]),
            [
                *tine.VCS_IGNORES,
                "packages/systemd.source/BUCK",
                "packages/systemd.source/**/BUCK",
                "packages/systemd.source/build-debug",
                "packages/systemd.source/mkosi/mkosi.tools",
            ],
        )

    def test_nested_ignores_and_negations_are_resolved_by_git(self) -> None:
        checkout = self.checkout("packages/project")
        nested = checkout / "nested"
        nested.mkdir()
        (nested / ".gitignore").write_text("*.tmp\n!kept.tmp\n")
        (nested / "ignored.tmp").write_text("")
        (nested / "kept.tmp").write_text("")

        ignores = self.ignores({}, ["packages/project"])

        self.assertIn("packages/project/nested/ignored.tmp", ignores)
        self.assertNotIn("packages/project/nested/kept.tmp", ignores)

    def test_an_ignored_root_suppresses_its_reported_descendants(self) -> None:
        checkout = self.checkout()
        generated = checkout / "generated"
        generated.mkdir()
        (generated / ".gitignore").write_text("*\n")
        (generated / "nested").mkdir()
        (generated / "nested" / "artifact").write_text("")

        self.assertEqual(tine.git_ignored_paths(checkout), ["generated"])

    def test_gitignore_and_info_exclude_supply_mount_ignores(self) -> None:
        checkout = self.checkout("packages/project")
        (checkout / ".gitignore").write_text("ignored-by-tree\n")
        (checkout / ".git" / "info" / "exclude").write_text("ignored-locally\n")
        (checkout / "ignored-by-tree").mkdir()
        (checkout / "ignored-locally").mkdir()

        ignores = self.ignores({}, ["packages/project"])

        self.assertIn("packages/project/ignored-by-tree", ignores)
        self.assertIn("packages/project/ignored-locally", ignores)

    def test_gitignore_negations_override_info_exclude(self) -> None:
        checkout = self.checkout()
        (checkout / ".git" / "info" / "exclude").write_text("*.tmp\n")
        (checkout / ".gitignore").write_text("!kept.tmp\n")
        (checkout / "ignored.tmp").touch()
        (checkout / "kept.tmp").touch()

        self.assertEqual(tine.git_ignored_paths(checkout), ["ignored.tmp"])

    def test_global_excludes_do_not_supply_mount_ignores(self) -> None:
        checkout = self.checkout()
        excludes = self.root / "global-excludes"
        excludes.write_text("ignored-globally\n")
        git("config", "core.excludesFile", str(excludes), cwd=checkout)
        (checkout / "ignored-globally").touch()

        self.assertEqual(tine.git_ignored_paths(checkout), [])

    def test_worktrees_use_the_shared_info_exclude(self) -> None:
        checkout = self.checkout()
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            cwd=checkout,
        )
        worktree = self.root / "worktree"
        git("worktree", "add", "--detach", str(worktree), cwd=checkout)
        (checkout / ".git" / "info" / "exclude").write_text("ignored-locally\n")
        (worktree / "ignored-locally").mkdir()

        self.assertEqual(tine.git_ignored_paths(worktree), ["ignored-locally"])

    def test_missing_info_exclude_is_allowed(self) -> None:
        checkout = self.checkout()
        (checkout / ".git" / "info" / "exclude").unlink()
        (checkout / ".gitignore").write_text("ignored\n")
        (checkout / "ignored").touch()

        self.assertEqual(tine.git_ignored_paths(checkout), ["ignored"])

    def test_buck_output_follows_checkout_ignore_rules(self) -> None:
        checkout = self.checkout("packages/project")
        output = checkout / "buck-out"
        output.mkdir()
        (output / "log").touch()

        self.assertEqual(tine.git_ignored_paths(checkout), [])
        self.assertEqual(
            self.ignores({}, ["packages/project"]),
            [*tine.VCS_IGNORES, "packages/project/BUCK", "packages/project/**/BUCK"],
        )

        (checkout / ".gitignore").write_text("/buck-out\n")
        self.assertEqual(tine.git_ignored_paths(checkout), ["buck-out"])
        self.assertIn("packages/project/buck-out", self.ignores({}, ["packages/project"]))

    def test_tracked_paths_matching_gitignore_remain_visible(self) -> None:
        checkout = self.checkout("packages/project")
        (checkout / "tracked.generated").write_text("")
        git("add", "tracked.generated", cwd=checkout)
        (checkout / ".gitignore").write_text("*.generated\n")

        self.assertNotIn("packages/project/tracked.generated", self.ignores({}, ["packages/project"]))
        self.assertIn(
            "packages/project/tracked.generated",
            self.ignores(
                {"project": {tine.PROJECT_IGNORE: "packages/project/tracked.generated"}},
                ["packages/project"],
            ),
        )

    def test_cell_build_files_remain_visible(self) -> None:
        for target in ("vendor/tine", "vendor"):
            with self.subTest(target=target):
                self.assertEqual(
                    self.ignores({"cells": {"root": ".", "tine": "vendor/tine"}}, [target]),
                    list(tine.VCS_IGNORES),
                )

    def test_a_local_project_ignore_is_refused_while_mounts_are_declared(self) -> None:
        # The generated block may carry the key from an earlier command; only what follows it counts.
        mounts = ["packages/project"]
        local = self.root / tine.LOCAL
        local.write_text(
            f"{tine.BLOCK_BEGIN}\n[project]\n{tine.PROJECT_IGNORE} = generated\n{tine.BLOCK_END}\n"
        )
        self.assertEqual(
            self.ignores({}, mounts),
            [
                *tine.VCS_IGNORES,
                "packages/project/BUCK",
                "packages/project/**/BUCK",
            ],
        )

        local.write_text(local.read_text() + f"\n[project]\n{tine.PROJECT_IGNORE} = scratch\n")
        with self.assertRaisesRegex(SystemExit, "would replace the generated ignores"):
            self.ignores({}, mounts)
        with self.assertRaisesRegex(SystemExit, "would replace the generated ignores"):
            self.ignores({}, [])
