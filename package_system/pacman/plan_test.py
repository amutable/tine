# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the planner against the real libalpm, on repositories faked from database entries.

buck test tine//package_system/pacman:plan-test
"""

import ctypes
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

import alpm
import plan
import transaction

EPOCH = 1739577600


def setUpModule() -> None:
    # The dev box contains libalpm only when it builds from an Arch release.
    try:
        ctypes.CDLL(alpm.SONAME)
    except OSError as error:
        raise unittest.SkipTest(f"the box contains no {alpm.SONAME}") from error


def _entry(name: str, **fields: list[str]) -> dict[str, list[str]]:
    filename = f"{name}-1-1-any.pkg.tar.zst"
    return {
        "FILENAME": [filename],
        "NAME": [name],
        "VERSION": ["1-1"],
        "ARCH": ["any"],
        "CSIZE": ["1"],
        "SHA256SUM": [hashlib.sha256(filename.encode()).hexdigest()],
        **fields,
    }


class TestSolve(unittest.TestCase):
    def solve(
        self, install: list[str], *entries: dict[str, list[str]], install_file: list[str] | None = None
    ) -> list[transaction.TransactionPackage]:
        """Run the planner's solve verb against one repository serving `entries`."""
        scratch = Path(tempfile.mkdtemp(prefix="plan-test."))
        (scratch / "repo").mkdir()
        install_files = []
        if install_file is not None:
            (scratch / "install.json").write_text(json.dumps(install_file))
            install_files.append(str(scratch / "install.json"))
        alpm.write_db([(f"{e['NAME'][0]}-1-1", e) for e in entries], scratch / "repo" / "test.db", EPOCH)
        spec = {
            "arch": "x86_64",
            "cache": [],
            "install": install,
            "install_files": install_files,
            "lower": [],
            "repositories": [
                {
                    "baseurl": "https://example.invalid/test",
                    "directory": str(scratch / "repo"),
                    "id": "test",
                    "priority": 99,
                }
            ],
        }
        (scratch / "spec.json").write_text(json.dumps(spec))
        plan.main(
            ["solve", "--spec", str(scratch / "spec.json"), "--out", str(scratch / "transaction.json")]
        )
        return cast(
            list[transaction.TransactionPackage], json.loads((scratch / "transaction.json").read_text())
        )

    def test_resolves_a_closure(self) -> None:
        transaction = self.solve(["a"], _entry("a", DEPENDS=["b"]), _entry("b"))
        self.assertEqual(sorted(entry["package_id"] for entry in transaction), ["a-1-1", "b-1-1"])

    def test_resolves_the_specs_of_an_install_file(self) -> None:
        transaction = self.solve([], _entry("a", DEPENDS=["b"]), _entry("b"), install_file=["a"])
        self.assertEqual(sorted(entry["package_id"] for entry in transaction), ["a-1-1", "b-1-1"])

    def test_an_empty_install_file_resolves_nothing(self) -> None:
        transaction = self.solve([], _entry("a"), install_file=[])
        self.assertEqual(transaction, [])

    def test_names_both_sides_of_a_conflict(self) -> None:
        # What libalpm hands back for this error is a list of conflicts, not of missing
        # dependencies; read as the latter, the planner used to crash instead of explaining.
        with self.assertRaises(SystemExit) as failure:
            self.solve(["a", "b"], _entry("a", CONFLICTS=["b"]), _entry("b"))
        self.assertIn("a conflicts with b (b): conflicting dependencies", str(failure.exception))

    def test_names_a_missing_dependency_and_who_wants_it(self) -> None:
        with self.assertRaises(SystemExit) as failure:
            self.solve(["a"], _entry("a", DEPENDS=["nothing>=2"]))
        self.assertIn("a requires nothing>=2: could not satisfy dependencies", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
