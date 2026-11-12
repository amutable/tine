# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Print how the bootstrap toolchain started this interpreter, for tine//tests:host-python."""

import json
import os
import sys

json.dump(
    {
        "executable": os.path.realpath(sys.executable),
        "ignore_environment": sys.flags.ignore_environment,
        "no_site": sys.flags.no_site,
        "no_user_site": sys.flags.no_user_site,
        "xoptions": sys._xoptions,
    },
    sys.stdout,
)
