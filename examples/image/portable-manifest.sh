#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Check the manifests of the demo-portable and demo-portable-ext DDIs.
#
# Usage: portable-manifest.sh IMAGE-MANIFEST EXTENSION-MANIFEST
set -euo pipefail

image=$1 extension=$2
r=0

has() {
    grep -qF "\"name\":\"$2\"" "$1"
}

expect() {
    if ! has "$1" "$2"; then
        echo "$(basename "$1"): missing $2" >&2
        r=1
    fi
}

refuse() {
    if has "$1" "$2"; then
        echo "$(basename "$1"): unexpected $2" >&2
        r=1
    fi
}

expect "$image" usr/lib/os-release
expect "$image" usr/lib/systemd/system/demo-portable.service
expect "$image" usr/bin/bash
expect "$image" etc/machine-id
expect "$image" etc/resolv.conf
expect "$image" etc/tine/from-parent
refuse "$image" usr/lib/extension-release.d

expect "$extension" usr/lib/extension-release.d/extension-release.demo-portable-ext
expect "$extension" etc/extension-release.d/extension-release.demo-portable-ext
expect "$extension" usr/lib/tine/from-portable-ext
expect "$extension" etc/tine/from-portable-ext
refuse "$extension" usr/lib/os-release
refuse "$extension" etc/os-release
refuse "$extension" usr/bin/bash
refuse "$extension" var/lib/tine/from-portable-ext
refuse "$extension" etc/tine/from-parent
refuse "$extension" usr/lib/systemd/system/demo-portable.service

exit "$r"
