# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The switch that turns on the compression of image outputs.

An image says how its outputs are compressed with its `compression` attributes. Those attributes only
take effect when `[tine] compression = true` is set.
"""

def enabled() -> bool:
    """Return whether the outputs of an image are compressed, for the attribute that the macros set."""
    configured = read_root_config("tine", "compression", "false")
    if configured not in ("true", "false"):
        fail("[tine] compression must be true or false, got {!r}".format(configured))
    return configured == "true"
