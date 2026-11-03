# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public quarry API."""

load("//distribution:defs.bzl", "distribution")

def hardhat_compatible_with() -> Select:
    """`target_compatible_with` of hardhat and of everything that runs it.

    hardhat binds libpathrs through cgo. Debian packages only its Rust crate, not the C library.
    """
    return distribution.select({
        "DEFAULT": [],
        "debian": ["prelude//:none"],
    })
