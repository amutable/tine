#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# The archive and the manifest of :replaced.layer must list what the child layer kept or created and
# none of the paths it removed. The manifest stores names as JSON strings, which escape a backslash.
#
# Usage: replaced-test.sh ARCHIVE MANIFEST
set -euo pipefail

archive=$(tar --quoting-style=literal -tvf "$1" | sed -E 's|^(.).* \.?/?([^ ]+)$|\1 \2|')
manifest=$(tr '\036' '\n' <"$2")
status=0

expect() {
    local present=$1 type=$2 name=$3 json=$4
    if grep -qxF -e "$type $name" <<<"$archive"; then found=yes; else found=no; fi
    if [ "$found" != "$present" ]; then
        echo "archive: $name ($type) present=$found, expected $present" >&2
        status=1
    fi
    if grep -qF -e "\"name\":\"$json\"" <<<"$manifest"; then found=yes; else found=no; fi
    if [ "$found" != "$present" ]; then
        echo "manifest: $name present=$found, expected $present" >&2
        status=1
    fi
}

expect yes - 'etc/tine/esc\x2dkept' 'etc/tine/esc\\x2dkept'
expect no - 'etc/tine/esc\x2dgone' 'etc/tine/esc\\x2dgone'
expect yes - etc/tine/sub/fresh etc/tine/sub/fresh
expect no - etc/tine/sub/old etc/tine/sub/old
expect yes - etc/tine/dir etc/tine/dir
expect no - etc/tine/dir/x etc/tine/dir/x
exit "$status"
