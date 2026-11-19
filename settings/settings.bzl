# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""`SETTINGS` configures tine's own targets.

`tools_box` lists the packages and local repositories that tine//distribution:tools.box installs in addition
to its own. Images run their tools in the tools box. A local repository with systemd in it therefore makes
every image build with that systemd. Either list can be a `distribution.select()`.
"""

SETTINGS = struct(
    tools_box = struct(
        additional_repositories = [],
        packages = [],
    ),
)
