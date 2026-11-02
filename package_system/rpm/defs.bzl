# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public RPM package-system API, exported as the `rpm` namespace."""

load("//package_system/rpm:generated.bzl", "rpm_branch")
load(
    "//package_system/rpm:rules.bzl",
    "rpm_package",
    "rpm_remote_repository",
)

rpm = struct(
    branch = rpm_branch,
    package = rpm_package,
    remote_repository = rpm_remote_repository,
)
