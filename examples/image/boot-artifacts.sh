#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# What the disk boots, read back out of its UKI. A wrong section goes unnoticed by every target, so
# assert by magic that each artifact is what it claims to be, and that the initrd is the one the UKI
# carries rather than the initrd image the composition was handed.
set -euo pipefail

kernel=$1 uki=$2 initrd=$3 composed=$4 arch=$5

case $arch in
# bzImage
x86_64) test "$(dd if="$kernel" bs=1 skip=514 count=4 status=none)" = HdrS ;;
# Fedora's arm64 is an EFI zboot image
arm64) test "$(dd if="$kernel" bs=1 skip=4 count=4 status=none)" = zimg ;;
*) echo "boot-artifacts: no kernel magic known for $arch" >&2; exit 1 ;;
esac
# A bzImage carries "HdrS" at 0x202 and a PE binary "MZ" at 0.
test "$(dd if="$uki" bs=1 count=2 status=none)" = MZ

# The UKI appends the modules initrd to the one it was given, so the one it carries is larger.
test "$(stat -c%s "$initrd")" -gt "$(stat -c%s "$composed")"
