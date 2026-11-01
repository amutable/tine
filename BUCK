# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Shared tine Python support, and the toolchain Buck2 looks up as `toolchains//`."""

load("@prelude//toolchains:python.bzl", "python_bootstrap_toolchain")
load("//box:defs.bzl", "box")
load("//python:defs.bzl", "python")

# Declared here rather than in a `toolchains` cell of its own: Buck2 forbids nesting a cell inside an
# external cell, so declaring it here is what lets a consuming project pull tine in as one.
python_bootstrap_toolchain(
    name = "python_bootstrap",
    interpreter = read_config("tine", "python_interpreter", "tine//tools:python3"),
    visibility = ["PUBLIC"],
)

export_file(
    name = "ty-config",
    src = "ty.toml",
    visibility = ["PUBLIC"],
)

export_file(
    name = "buckconfig",
    src = ".buckconfig",
    out = "tine.buckconfig",
    visibility = ["//bin/..."],
)

python.library(
    name = "util",
    srcs = ["util.py"],
    visibility = ["PUBLIC"],
)

box.python_test(
    name = "test",
    box = "//tools:dev.box",
    srcs = ["util_test.py"],
    deps = [":util"],
)

python.library(
    name = "specs",
    srcs = ["specs.py"],
    visibility = ["PUBLIC"],
)
