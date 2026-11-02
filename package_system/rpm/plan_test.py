# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the planner against real repodata and a real solve.

Builds throwaway packages and indexes them with the same driver a build uses, thus exercises
libdnf5's actual resolution.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import transaction

try:
    import createrepo
    import plan
except ModuleNotFoundError as error:
    # The dev box contains libdnf5 and createrepo_c only when it builds from a Fedora release.
    if error.name not in ("createrepo_c", "libdnf5"):
        raise
    raise unittest.SkipTest(f"the box contains no {error.name}") from error

SPEC = """\
Name: {name}
Version: {version}
Release: 1
Summary: A package to solve for
License: MIT
BuildArch: noarch
{extra}
%description
%install
mkdir -p %{{buildroot}}/usr/share/{name}
touch %{{buildroot}}/usr/share/{name}/marker
%files
/usr/share/{name}
"""


class TestPlan(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._scratch.name)
        cls.addClassCleanup(cls._scratch.cleanup)

    def build(self, name: str, version: str, extra: str = "") -> Path:
        spec = self.scratch / f"{name}-{version}.spec"
        spec.write_text(SPEC.format(name=name, version=version, extra=extra))
        subprocess.run(
            [
                "rpmbuild",
                "-bb",
                "--quiet",
                "--define",
                f"_topdir {self.scratch}/rpmbuild",
                "--define",
                "_buildhost tine",
                str(spec),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return self.scratch / f"rpmbuild/RPMS/noarch/{name}-{version}-1.noarch.rpm"

    def repository(self, name: str, *packages: Path) -> transaction.Repository:
        out = Path(tempfile.mkdtemp(dir=self.scratch)) / name
        createrepo.createrepo([(package.name, package) for package in packages], out, "0")
        # No base URL: these are packages this test produced, so the transaction names them by path.
        return transaction.Repository(name, out.absolute(), 99, None)

    def solve(self, install: list[str], *repos: transaction.Repository) -> list[str]:
        """The package ids for solving `install` against `repos`"""
        # separate cache directory, so one case's parsed metadata cannot answer for another's.
        cachedir = Path(tempfile.mkdtemp(dir=self.scratch)) / "cache"
        resolved = plan.plan(list(repos), install, cachedir, None, "x86_64", [])
        return sorted(package["package_id"] for package in resolved)

    def test_resolves_a_spec_to_the_package_it_names(self) -> None:
        repo = self.repository("plain", self.build("gadget", "1"))
        self.assertEqual(self.solve(["gadget"], repo), ["gadget-1-1.noarch"])

    def test_resolves_a_spec_naming_a_capability(self) -> None:
        repo = self.repository("capability", self.build("gadget", "1", "Provides: gizmo = 1"))
        self.assertEqual(self.solve(["gizmo"], repo), ["gadget-1-1.noarch"])

    def test_resolves_a_spec_naming_a_file(self) -> None:
        repo = self.repository("file", self.build("gadget", "1"))
        self.assertEqual(self.solve(["/usr/share/gadget/marker"], repo), ["gadget-1-1.noarch"])

    def test_a_conditional_spec_whose_condition_is_false_installs_nothing(self) -> None:
        repo = self.repository("conditional", self.build("gadget", "1"), self.build("trigger", "1"))
        self.assertEqual(self.solve(["(gadget if trigger)"], repo), [])

    def test_a_conditional_spec_whose_condition_is_true_installs_its_package(self) -> None:
        repo = self.repository("conditional-true", self.build("gadget", "1"), self.build("trigger", "1"))
        self.assertEqual(
            self.solve(["trigger", "(gadget if trigger)"], repo), ["gadget-1-1.noarch", "trigger-1-1.noarch"]
        )

    def test_an_alternative_spec_installs_the_available_package(self) -> None:
        # The solve satisfies (A or B) with the package that the repository contains.
        repo = self.repository("alternative", self.build("gadget", "1"))
        self.assertEqual(self.solve(["(widget or gadget)"], repo), ["gadget-1-1.noarch"])

    def test_an_alternative_spec_without_any_package_fails(self) -> None:
        # undelivered() skips rich dependencies. The solve itself must fail on (A or B) when the
        # repository contains neither A nor B.
        repo = self.repository("no-alternative", self.build("gadget", "1"))
        with self.assertRaises(SystemExit):
            self.solve(["(widget or gizmo)"], repo)

    def test_refuses_a_spec_an_obsoleting_package_displaced(self) -> None:
        """split-out subpackage obsoleting its parent of older version is refused."""

        old = self.repository("old", self.build("widget", "1"))
        new = self.repository("new", self.build("widget-keys", "2", "Obsoletes: widget < 2"))
        with self.assertRaises(SystemExit) as failure:
            self.solve(["widget"], old, new)
        self.assertIn("does not install", str(failure.exception))
        self.assertIn("widget, obsoleted by widget-keys-2-1.noarch", str(failure.exception))


class TestMain(unittest.TestCase):
    def test_an_empty_install_file_writes_an_empty_transaction(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as scratch:
            root = Path(scratch)
            (root / "requires.json").write_text("[]")
            spec = {
                "arch": "x86_64",
                "cache": [],
                "install": [],
                "install_files": [str(root / "requires.json")],
                "lower": [],
                "repositories": [],
            }
            (root / "solve.spec.json").write_text(json.dumps(spec))
            plan.main(
                ["solve", "--spec", str(root / "solve.spec.json"), "--out", str(root / "transaction.json")]
            )
            self.assertEqual(json.loads((root / "transaction.json").read_text()), [])


if __name__ == "__main__":
    unittest.main()
