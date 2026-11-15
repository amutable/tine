# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Define the rpm macros of the drivers that run libdnf5 against an image's root."""

import rpm

DBPATH = "usr/lib/sysimage/rpm"


def define(langs: list[str] | None = None) -> None:
    """Point rpm at the database location and backend that tine reads, and restrict `%lang()` files.

    Fedora patches rpm to keep its database in usr/lib/sysimage/rpm, while upstream rpm, which Debian and
    Arch build, keeps it in var/lib/rpm. Without these macros, the box's rpm reads and writes the database
    of an image in a different place depending on the box's distribution.

    Call this after `libdnf5.base.Base.setup()`. setup() makes librpm read its macro files, which librpm
    does only once per process. A macro file that librpm reads after this call overrides these macros.
    """
    macros = {"_dbpath": f"/{DBPATH}", "_db_backend": "sqlite"}
    if langs:
        macros["_install_langs"] = ":".join(langs)
    for name, value in macros.items():
        # rpm/__init__.py re-exports its C extension with a star import, which ty can't see through.
        rpm.addMacro(name, value)  # ty: ignore[missing-attribute]
