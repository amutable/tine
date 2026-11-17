# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Packages and local repositories that tine//distribution:tools.box installs in addition to its own.

Images run their tools in the tools box. A local repository with systemd in it therefore makes every image
build with that systemd. Either value can be a `distribution.select()`.
"""

TOOLS_BOX = struct(
    additional_repositories = [],
    packages = [],
)
