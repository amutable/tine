# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Exercise Python test execution with bootstrap and boxed interpreters."""

import os
import sys
import unittest
from pathlib import Path

import util

import cpio


class PythonTest(unittest.TestCase):
    def test_uses_the_requested_interpreter(self) -> None:
        self.assertTrue(Path(sys.executable).samefile(os.environ["PYTHON_TEST_INTERPRETER"]))

    def test_preserves_environment_values(self) -> None:
        self.assertEqual(os.environ["PYTHON_TEST_VALUE"], "spaces 'quotes' \"double quotes\" $literal;")

    def test_expands_artifacts_in_the_environment(self) -> None:
        source = Path(os.environ["PYTHON_TEST_INPUT"]).read_text()
        self.assertIn('name = "test-input"', source)

    def test_copies_sources_and_transitive_dependencies(self) -> None:
        root = Path(__file__).parent
        for source in (__file__, cpio.__file__, util.__file__):
            with self.subTest(source=source):
                path = Path(source)
                self.assertEqual(path.parent, root)
                self.assertFalse(path.is_symlink())
