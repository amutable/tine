# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Unpack a tar archive downloaded by `http.tool` into a directory."""

import sys
import tarfile
from pathlib import Path


def main() -> None:
    # The host's python3 runs this script to unpack the pinned Python, so it must keep working on 3.12, the
    # oldest release the README allows.
    source, destination = (Path(argument) for argument in sys.argv[1:])
    destination.mkdir()
    with tarfile.open(source) as archive:
        archive.extractall(destination, filter="data")


if __name__ == "__main__":
    main()
