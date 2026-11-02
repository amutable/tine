# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the list of the source archives that the `sources` file of a dist-git repository names.

buck test tine//package_system/rpm:lookaside-test
"""

import tempfile
import unittest
from pathlib import Path
from typing import override

import lookaside

_BASE = "https://src.fedoraproject.org/repo/pkgs/rpms/"


class Archives(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.scratch = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rpm-lookaside-test.", dir="/var/tmp"))
        )
        self.sources = self.scratch / "sources"

    def archives(self, text: str | None, sources: Path | None = None) -> list[lookaside.Archive]:
        if text is not None:
            self.sources.write_text(text)
        return lookaside.archives(
            lookaside.Spec(
                base=_BASE,
                package="hello",
                sources=str(sources or self.sources),
                out=str(self.scratch / "out.json"),
            )
        )

    def test_archives_are_listed_with_their_urls(self) -> None:
        # A sources file lists two archives and an empty line. The driver lists both archives with
        # lowercase digests, and with the URL of each archive in the lookaside cache.
        digest = "A" * 128
        self.assertEqual(
            self.archives(f"SHA512 (hello-2.12.tar.gz) = {digest}\n\nSHA512 (hello.sig) = {'b' * 128}\n"),
            [
                {
                    "name": "hello-2.12.tar.gz",
                    "sha512": "a" * 128,
                    "url": f"{_BASE}hello/hello-2.12.tar.gz/sha512/{'a' * 128}/hello-2.12.tar.gz",
                },
                {
                    "name": "hello.sig",
                    "sha512": "b" * 128,
                    "url": f"{_BASE}hello/hello.sig/sha512/{'b' * 128}/hello.sig",
                },
            ],
        )

    def test_a_name_with_a_space_is_quoted_in_the_url(self) -> None:
        # The name of an archive is a component of its URL. A space in the name must be quoted there,
        # and stay in the name of the output.
        (archive,) = self.archives(f"SHA512 (hello 2.12.tar.gz) = {'0' * 128}\n")
        self.assertEqual(archive["name"], "hello 2.12.tar.gz")
        self.assertTrue(archive["url"].endswith("/hello%202.12.tar.gz"))

    def test_a_name_listed_twice_fails(self) -> None:
        # The rule declares an output named after each archive, and Buck fails on two outputs with one
        # name. A sources file that lists one name twice must fail in the driver instead.
        with self.assertRaisesRegex(SystemExit, "lists hello.tar.gz twice"):
            self.archives(f"SHA512 (hello.tar.gz) = {'0' * 128}\nSHA512 (hello.tar.gz) = {'1' * 128}\n")

    def test_an_md5_line_fails(self) -> None:
        # Old packages list an archive by its MD5 digest, which Buck can't check. The driver must fail
        # and say how to list the SHA-512 digest instead.
        with self.assertRaisesRegex(SystemExit, "lists an MD5 digest.*fedpkg new-sources"):
            self.archives(f"{'b' * 32}  hello-1.0.tar.gz\n")

    def test_a_symlink_fails(self) -> None:
        # A symlinked sources file, or one below a symlinked directory, can point at a file of the
        # project. The driver must refuse both instead of sending the lines of that file to the cache.
        secret = self.scratch / "secret"
        secret.mkdir()
        (secret / "sources").write_text(f"SHA512 (password) = {'0' * 128}\n")
        (self.scratch / "file").symlink_to(secret / "sources")
        (self.scratch / "directory").symlink_to(secret)
        for path in (self.scratch / "file", self.scratch / "directory" / "sources"):
            with self.subTest(path=path), self.assertRaisesRegex(SystemExit, "is a symlink"):
                self.archives(None, path)

    def test_a_sources_file_that_is_not_utf8_fails(self) -> None:
        # A sources file with bytes that aren't UTF-8 fails with a message instead of a traceback.
        self.sources.write_bytes(b"\xff\xfe\n")
        with self.assertRaisesRegex(SystemExit, "is not UTF-8"):
            self.archives(None)

    def test_a_name_with_a_slash_fails(self) -> None:
        # The name of an archive becomes a file name and a component of the URL, so a name with a slash
        # must fail.
        with self.assertRaisesRegex(SystemExit, "not a file name"):
            self.archives(f"SHA512 (../hello.tar.gz) = {'0' * 128}\n")

    def test_an_unparsable_line_fails(self) -> None:
        # A line with another hash type, or in no form of the sources file, fails the list.
        for line in (f"SHA1 (hello.tar.gz) = {'0' * 40}", "not a sources line at all"):
            with self.subTest(line=line), self.assertRaisesRegex(SystemExit, "cannot parse"):
                self.archives(line + "\n")


if __name__ == "__main__":
    unittest.main()
