#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# The release directory is where every published name meets. Assert the set the example publishes,
# that each listing is named after what it describes, that the verity pair is named after the two
# halves of the root hash (which is what lets systemd-sysupdate give the partitions it writes the
# UUIDs dissection pairs them by), and that the ESP is not among them. Assert that the release's
# description lists every published file with its kind, image and version, and gives the verity root
# hash to the usr partition and the extension.
set -euo pipefail

directory=$1 roothash_file=$2 arch=$3 pkgdb_suffix=$4 pkgdb_system=$5
roothash=$(cat "$roothash_file")
names=$(ls "$directory")
description=$directory/artifacts_$arch.json

# Every image of the example has the default version, 0.
version=0
image="image_${version}_$arch" extension="demo-ext_${version}_$arch"
usr="$image.usr-$arch.${roothash:0:32}.raw" usr_verity="$image.usr-$arch-verity.${roothash:32:32}.raw"
failed=0
fail() {
    echo "release: $*" >&2
    failed=1
}

for expected in "$image.raw" "$image.qcow2" "$image.efi" "$image.vmlinuz" "$image.initrd" \
    "$extension.sysext.raw" "$image.Uapi16Manifest" "$image.esp.Uapi16Manifest" \
    "$image.usr.Uapi16Manifest" "$extension.sysext.Uapi16Manifest" \
    "$image.spdx.json" "$image.cdx.json" "$image.pkgdb.$pkgdb_suffix" \
    "$image.initrd.spdx.json" "$image.initrd.cdx.json" \
    "$extension.spdx.json" "$extension.cdx.json" "$extension.sysext.roothash" \
    "artifacts_$arch.json" "$usr" "$usr_verity"; do
    grep -qx "$expected" <<< "$names" || fail "$expected is missing"
done
# systemd-sysupdate ignores symlinks in a source directory. The release must contain only regular files.
irregular=$(find "$directory" -mindepth 1 ! -type f -printf '%P\n')
[ -z "$irregular" ] || fail "these entries are not regular files: $irregular"
# Its listing is published, because what it carries is on the disk; the partition itself is not.
if grep '\.raw$' <<< "$names" | grep -q esp; then
    fail "the ESP partition must not be published"
fi

[ "$(jq -r .arch "$description")" = "$arch" ] || fail "the description names another architecture"
if ! diff <(jq -r '.files | keys[]' "$description" | LC_ALL=C sort) \
    <(grep -vx "artifacts_$arch.json" <<< "$names" | LC_ALL=C sort) >&2; then
    fail "the description lists other files than the release holds"
fi

# Each line names a file and the [image, kind, subkind, version] the description gives it, null where
# it has none.
while read -r name want; do
    got=$(jq -c --arg name "$name" '.files[$name] | [.image, .kind, .subkind, .version]' "$description")
    [ "$got" = "$want" ] || fail "$name is described as $got, not $want"
done <<EOF
$image.raw ["$image","disk","raw","$version"]
$image.qcow2 ["$image","disk","qcow2","$version"]
$image.efi ["$image","uki",null,"$version"]
$image.vmlinuz ["$image","kernel",null,"$version"]
$image.initrd ["$image","initrd",null,"$version"]
$image.Uapi16Manifest ["$image","listing","uapi16manifest","$version"]
$image.usr.Uapi16Manifest ["$image","listing","uapi16manifest","$version"]
$image.spdx.json ["$image","sbom","spdx","$version"]
$image.cdx.json ["$image","sbom","cyclonedx","$version"]
$image.pkgdb.$pkgdb_suffix ["$image","pkgdb","$pkgdb_system","$version"]
$image.initrd.spdx.json ["$image.initrd","sbom","spdx","$version"]
$usr ["$image","partition","usr","$version"]
$usr_verity ["$image","partition","usr-verity","$version"]
$extension.sysext.raw ["$extension.sysext","extension","sysext","$version"]
$extension.sysext.Uapi16Manifest ["$extension.sysext","listing","uapi16manifest","$version"]
$extension.sysext.roothash ["$extension.sysext","roothash",null,"$version"]
$extension.spdx.json ["$extension.sysext","sbom","spdx","$version"]
EOF

# The list above leaves out some files, such as the ESP listing, so this check covers every file.
unversioned=$(jq -r --arg version "$version" \
    '.files | to_entries[] | select(.value.version != $version) | .key' "$description")
[ -z "$unversioned" ] || fail "these files do not have version $version: $unversioned"

# Only the usr partition and the extension carry a verity root hash.
extension_hash=$(cat "$directory/$extension.sysext.roothash")
want=$(printf '%s %s\n' "$usr" "$roothash" "$extension.sysext.raw" "$extension_hash" | LC_ALL=C sort)
hashed='.files | to_entries[] | select(.value.root_hash) | "\(.key) \(.value.root_hash)"'
got=$(jq -r "$hashed" "$description" | LC_ALL=C sort)
[ "$got" = "$want" ] || fail "the root hashes are $got, not $want"
exit "$failed"
