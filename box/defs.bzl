# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""The public box API.

Load the `box` namespace here to declare a box, enter one from a rule, or run a test suite inside
one; the modules behind this are implementation structure and may be rearranged.
"""

load("//box:build.bzl", "box_layer", "new")
load("//box:runtime.bzl", "BoxInfo", "box_run")
load("//box:test.bzl", "box_sh_test")

box = struct(
    Info = BoxInfo,
    layer = box_layer,
    new = new,
    run = box_run,
    sh_test = box_sh_test,
)
