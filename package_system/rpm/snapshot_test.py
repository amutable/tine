# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the parsing of rpm-md repository metadata.

buck test tine//package_system/rpm:snapshot-test
"""

import io
import unittest
import xml.etree.ElementTree as ET

import snapshot

SHA256 = "ab" * 32
SHA512 = "cd" * 64


def record(algorithm: str, digest: str) -> ET.Element:
    return ET.fromstring(
        f'<data xmlns="http://linux.duke.edu/metadata/repo" type="primary">'
        f'<checksum type="{algorithm}">{digest}</checksum>'
        f'<location href="repodata/{digest}-primary.xml.zst"/>'
        f"<size>42</size>"
        f"</data>"
    )


def primary(algorithm: str, digest: str) -> io.BytesIO:
    return io.BytesIO(
        f'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="1">'
        f'<package type="rpm">'
        f'<checksum type="{algorithm}" pkgid="YES">{digest}</checksum>'
        f'<location href="x86_64/pkg-1-1.x86_64.rpm"/>'
        f'<size package="7"/>'
        f"</package>"
        f"</metadata>".encode()
    )


class Stream(unittest.TestCase):
    def test_a_stream_keeps_the_digest_its_record_states(self) -> None:
        # Fedora states a sha256 for each stream of its repomd.xml and openSUSE a sha512. The pinned
        # stream must carry that digest under the name of its algorithm, and no other digest.
        for algorithm, digest in (("sha256", SHA256), ("sha512", SHA512)):
            stream = snapshot._repository_stream(
                "repo", "https://repo.invalid/", "primary", record(algorithm, digest)
            )
            self.assertEqual(
                stream,
                {
                    "out": f"repodata/{digest}-primary.xml.zst",
                    "url": f"https://repo.invalid/repodata/{digest}-primary.xml.zst",
                    "size": 42,
                    algorithm: digest,
                },
            )

    def test_a_stream_without_a_known_digest_fails(self) -> None:
        # Buck can't check a download against a sha1. The snapshot must fail on a record that
        # states only a sha1.
        with self.assertRaisesRegex(SystemExit, "sha256 or sha512 checksum"):
            snapshot._repository_stream(
                "repo", "https://repo.invalid/", "primary", record("sha1", "ab" * 20)
            )


class Primary(unittest.TestCase):
    def test_a_package_is_keyed_by_its_pkgid(self) -> None:
        # The snapshot keys a package by its pkgid, which openSUSE states as a sha512 and Fedora as a
        # sha256. The sha256 and the sha512 must each become the key of the package's entry.
        for algorithm, digest in (("sha256", SHA256), ("sha512", SHA512)):
            packages = snapshot._parse_primary("repo", primary(algorithm, digest))
            self.assertEqual(packages, {digest: {"location": "x86_64/pkg-1-1.x86_64.rpm", "size": 7}})

    def test_a_pkgid_of_the_wrong_length_fails(self) -> None:
        # A pkgid whose length does not match its stated algorithm is corrupt metadata.
        with self.assertRaisesRegex(SystemExit, "invalid sha512"):
            snapshot._parse_primary("repo", primary("sha512", SHA256))


if __name__ == "__main__":
    unittest.main()
