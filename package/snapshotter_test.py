# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Tests for the download of a file of a repository.

The tests replace the HTTP response.

buck test tine//package:test
"""

import http.client
import io
import unittest
from typing import Self, override
from unittest import mock

import snapshotter


class Response:
    """A response that ends after `content`, `missing` bytes short of its Content-Length or with `error`."""

    def __init__(self, content: bytes, *, missing: int = 0, error: Exception | None = None) -> None:
        self.chunks = [content, b""]
        self.length = missing
        self.error = error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, _size: int) -> bytes:
        if self.error is not None and len(self.chunks) == 1:
            raise self.error
        return self.chunks.pop(0)


class Download(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.enterContext(mock.patch("time.sleep"))
        self.output = io.BytesIO()

    def download(self, *responses: Response) -> None:
        with mock.patch.object(snapshotter, "urlopen", side_effect=responses):
            snapshotter.download("repo", "archive", "https://repo.invalid/archive", self.output)

    def test_a_cut_off_chunked_transfer_is_retried(self) -> None:
        # http.client raises IncompleteRead when a chunked transfer ends early. download() must retry
        # it and write only the content of the second, complete response.
        self.download(Response(b"arch", error=http.client.IncompleteRead(b"arch")), Response(b"archive\n"))
        self.assertEqual(self.output.getvalue(), b"archive\n")

    def test_a_transfer_short_of_its_length_is_retried(self) -> None:
        # http.client returns a body that ends before its Content-Length without an error. download()
        # must notice the missing bytes, retry and write only the content of the complete response.
        self.download(Response(b"arch", missing=4), Response(b"archive\n"))
        self.assertEqual(self.output.getvalue(), b"archive\n")

    def test_a_transfer_that_always_ends_early_fails(self) -> None:
        # When every attempt ends before its Content-Length, download() must give up with an error that
        # names the number of missing bytes.
        with self.assertRaisesRegex(ConnectionError, "ended 4 bytes early"):
            self.download(*(Response(b"arch", missing=4) for _ in range(4)))


if __name__ == "__main__":
    unittest.main()
