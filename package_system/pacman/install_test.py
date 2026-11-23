# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the pacman installer.

buck test tine//package_system/pacman:test
"""

import contextlib
import io
import sys
import unittest

import install


class TestRunTransaction(unittest.TestCase):
    def test_a_failed_scriptlet_fails_the_install(self) -> None:
        """A failed scriptlet fails the install, although pacman exits with status 0.

        Run a stand-in for pacman that prints the error of a failed scriptlet on stderr and exits with
        status 0. Expect `run_transaction` to fail, and the error to reach stderr.
        """
        error = "error: command failed to execute correctly"
        pacman = [sys.executable, "-c", f"import sys; print({error!r}, file=sys.stderr)"]
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaisesRegex(SystemExit, "a scriptlet or hook failed"):
                install.run_transaction(pacman)
        self.assertIn(error, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
