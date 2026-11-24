# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""`SETTINGS` configures tine's own targets.

`package_manager` lists repositories that catalog//:<release>.package-manager installs packages from in
addition to the repositories of the release. An image that names neither a package manager nor a parent
installs its packages with catalog//:<release>.package-manager by default. If a local repository and the
release contain the same package, the package manager installs the package from the local repository. A
buildroot installs packages without the repositories of `package_manager`.

`tools_box` lists the packages and local repositories that tine//distribution:tools.box installs in addition
to its own. Images run their tools in the tools box. A local repository with systemd in it therefore makes
every image build with that systemd.

Every list in `SETTINGS` can be a `distribution.select()`.
"""

SETTINGS = struct(
    package_manager = struct(
        additional_repositories = [],
    ),
    tools_box = struct(
        additional_repositories = [],
        packages = [],
    ),
)
