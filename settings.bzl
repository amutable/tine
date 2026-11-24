# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Read `SETTINGS` from the settings cell, and add a default for every field that the cell leaves out."""

load("@settings//:settings.bzl", _SETTINGS = "SETTINGS")

# A project's settings cell only sets the fields that it changes, and it can be older than a field that tine
# reads. Buck fails to load a file that reads a field that the project's `SETTINGS` doesn't have.
_DEFAULTS = struct(
    package_manager = struct(
        additional_repositories = [],
    ),
    tools_box = struct(
        additional_repositories = [],
        packages = [],
    ),
)

def _section(name: str, defaults: struct) -> struct:
    settings = getattr(_SETTINGS, name, struct())
    return struct(**{field: getattr(settings, field, getattr(defaults, field)) for field in dir(defaults)})

SETTINGS = struct(**{name: _section(name, getattr(_DEFAULTS, name)) for name in dir(_DEFAULTS)})
