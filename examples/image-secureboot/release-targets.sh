#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# Assert that the release holds only regular files, as systemd-sysupdate skips symlinks, and that its TUF
# targets are unsigned, list every published file but the sparse raw disk plus the sysupdate definitions,
# inline those unchanged, and carry quarry's marks as the description gives them. Nothing here runs
# the definitions.
set -euo pipefail

directory=$1 arch=$2 transfers=$3
description=$directory/artifacts_$arch.json
targets=$directory/targets_$arch.json
extension="demo-ext_0_$arch.sysext.raw"
failed=0
fail() {
    echo "release: $*" >&2
    failed=1
}

irregular=$(find "$directory" -mindepth 1 ! -type f -printf '%P\n')
[ -z "$irregular" ] || fail "these entries are not regular files: $irregular"

[ "$(jq -c .signatures "$targets")" = "[]" ] || fail "the targets are signed"

published='.files | to_entries[] | select(.value.kind != "disk" or .value.subkind != "raw") | .key'
want=$( (jq -r "$published" "$description"
    find "$transfers" -type f -printf '.quarry/sysupdate.d/%P\n') | LC_ALL=C sort)
got=$(jq -r '.signed.targets | keys[]' "$targets" | LC_ALL=C sort)
[ "$got" = "$want" ] || fail "the targets list $got, not $want"

for transfer in "$transfers"/*; do
    name=.quarry/sysupdate.d/${transfer##*/}
    inline='.signed.targets[$name]["x-quarry-inline-data"]'
    cmp -s "$transfer" <(jq -r --arg name "$name" "$inline" "$targets" | base64 -d) \
        || fail "$name does not hold $transfer"
done

versions=$(jq -c '[.signed.targets | to_entries[] | select(.key | startswith(".quarry/") | not) | .value.custom.quarry.version] | unique' "$targets")
[ "$versions" = '["0"]' ] || fail "the targets have versions $versions, not [\"0\"]"

marks() { jq -c --arg name "$1" ".signed.targets[\$name].custom.quarry | $2" "$targets"; }
[ "$(marks "$extension" '.update')" = "true" ] || fail "$extension is not an update"
want=$(jq -c --arg name "$extension" '.files[$name].root_hash' "$description")
[ "$(marks "$extension" '."verity-root-hash"')" = "$want" ] || fail "$extension has another verity root hash"
want='["demo-ext_0_'$arch'.cdx.json","demo-ext_0_'$arch'.spdx.json"]'
[ "$(marks "$extension" '."supplemented-by" | keys')" = "$want" ] || fail "$extension does not bring its SBOMs"
[ "$(marks "image_0_$arch.qcow2" '.update')" = "null" ] || fail "the qcow2 disk is an update"
exit "$failed"
