#!/bin/bash

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

# The full CI pipeline. Arguments select groups, e.g. `tools/ci.sh check build`
set -Eeuo pipefail
cd "$(dirname "$0")/.."
buck=(bin/tine buck)
only_groups=("$@")

# GitHub folds each group into a log group; an interactive terminal gets a bold banner; anything else
# (piped to a file, another CI) gets a bare `=== label ===` line with no escape sequences.
if [ "${GITHUB_ACTIONS:-}" = true ]; then mode=github
elif [ -t 1 ]; then
    mode=tty
    bold=$'\033[1m'
    dim=$'\033[2m'
    reset=$'\033[0m'
    # The rule's accent is an ANSI palette colour (not a fixed RGB), so the terminal keeps it legible
    # on its own light/dark theme. Not bold: that renders bright and washes out on light backgrounds.
    accent=''
    [ -z "${NO_COLOR:-}" ] && accent=$'\033[36m'
else mode=plain
fi

# Space left on the filesystem holding buck-out
free_space() {
    df -h --output=avail . | awk 'NR == 2 { print $1 }'
}

# A full-width ━ rule in the accent colour.
hrule() {
    local width fill
    width=$(tput cols 2>/dev/null || echo 72)
    printf -v fill '%*s' "$width" ''
    printf '%s%s%s\n' "$accent" "${fill// /━}" "$reset"
}

# A group banner: blank line; a rule opening `━━━ label ` with the label default-fg bold and the dashes
# accented; the dim command for copy-paste; then a closing rule.
banner() {
    local label=$1 width tail used
    shift
    width=$(tput cols 2>/dev/null || echo 72)
    used=$((4 + ${#label} + 1))  # "━━━ " + label + trailing space
    printf -v tail '%*s' "$((width > used ? width - used : 0))" ''
    printf '\n%s━━━ %s%s%s %s%s%s\n' "$accent" "$reset$bold" "$label" "$reset" "$accent" "${tail// /━}" "$reset"
    printf '%s%s%s\n' "$dim" "$*" "$reset"
    hrule
}

# group LABEL -- CMD...: run one labelled group, fail-fast.
#
# Never test the group's status (`"$@" || rc=$?`): bash then ignores errexit for it, and for every
# function it calls, so a group would run on past its own first failure and report the status of
# whatever ran last. The GitHub failure path goes through the ERR trap below instead, and `groupname` is
# global for the trap to name.
group() {
    groupname=$1
    shift 2
    # With group names as arguments, everything else is skipped.
    if [ "${#only_groups[@]}" -gt 0 ]; then
        case " ${only_groups[*]} " in
            *" $groupname "*) ;;
            *) return 0 ;;
        esac
    fi
    case $mode in
        # in the label rather than the group's own output, so that we see it in the collapsed GitHub log
        github) printf '::group::%s (%s free)\n' "$groupname" "$(free_space)" ;;
        tty) banner "$groupname" "$@" ;;
        plain) printf '\n=== %s ===\n' "$groupname" ;;
    esac
    "$@"
    if [ "$mode" = github ]; then printf '::endgroup::\n'; fi
}

# On GitHub, close the group and surface an ::error:: after it (a failure buried inside a collapsed
# group is invisible). errtrace (-E above) carries this into the functions groups run.
if [ "$mode" = github ]; then
    trap 'printf "::endgroup::\n::error::ci group %s failed\n" "$groupname"' ERR
fi

# TINE_TEST_DISTRO names the release whose boxes every group builds with, such as arch.rolling. Nested
# Buck runs, such as the ones `check` starts, take no command-line configuration, so the setting goes
# into the project's configuration for as long as this script runs.
box_config=.buckconfig.d/tine-box
box_cleanup() { rm -f "$box_config"; }
box_cleanup
if [ -n "${TINE_TEST_DISTRO:-}" ]; then
    mkdir -p .buckconfig.d
    printf '[tine]\nbox = %s\n' "$TINE_TEST_DISTRO" > "$box_config"
    trap box_cleanup EXIT
fi

# The catalog pins Arch repositories for x86_64 only. On another architecture, the Arch root box
# cannot be built and the catalog cannot be verified.
x86_only_catalog() { [ "$(uname -m)" = x86_64 ]; }

# A run builds and tests one distro release (TINE_TEST_DISTRO, or else the catalog's default release
# Fedora): its images, and tine's own targets with its boxes. Apart from secureboot_pkcs11 (which signs a
# Fedora image), it needs only that release's mirrors, so a release whose mirror is down fails its own
# job only.
release=$("${buck[@]}" run tine//tools:dev -- release)

# `universe` lists the build targets of the tine and catalog cells. tools/dev.py explains which targets
# it leaves out.
universe() { "${buck[@]}" run tine//tools:dev -- universe; }

# `build` and `test` take no query, so they get the targets of the release explicitly, as
# tools/release.bxl selects them. That leaves out the incompatible ones too.
whole_cell() {
    local verb=$1 listing targets
    shift
    listing=$("${buck[@]}" run tine//tools:dev -- targets --release "$release")
    mapfile -t targets <<< "$listing"
    "${buck[@]}" "$verb" "${targets[@]}" "$@"
}

# The boot smokes need the host's KVM. Some CI envs (like GitHub's arm64 runners) don't have that.
have_kvm() { [ -e /dev/kvm ]; }
vm_filter=()
if ! have_kvm; then
    # A smoke test contains `image` too, and a matching `--include` wins without `--always-exclude`.
    vm_filter=(--exclude vm --always-exclude)
    if [ "$mode" = github ]; then printf '::warning::'; fi
    printf 'no /dev/kvm: skipping the VM boot smokes\n'
fi

# Sign the Secure Boot example through PKCS#11 tokens, exercising the external-key path end to end
# with the production module: tools/signing-server serves one tpm2-pkcs11 token per key from a
# software TPM, and the same example builds against its socket. Every tool this needs comes from the
# swtpm-signing box, so a runner needs nothing beyond what the rest of the pipeline uses. The
# externally signed image must also boot, which reuses the vm-smoke checks in full: the image carries
# the token's certificates the same way a generated key's, so the checks reading them apply here too.
# Stopping the arrangement is one TERM, per tools/signing-server. The log surfaces only on failure;
# the daemons keep spamming it during the build (swtpm logs every client disconnect), so it stays
# out of the console on success.
pkcs11_cleanup() {
    local status=$?
    if [ "$status" -ne 0 ] && [ -e "$pkcs11_dir/log" ]; then cat "$pkcs11_dir/log" >&2; fi
    # Already gone when the server itself failed, which the socket wait below then reports.
    if kill -0 "$server_pid" 2> /dev/null; then kill "$server_pid"; fi
    rm -rf "$pkcs11_dir"
}

secureboot_pkcs11() {
    # On a fresh runner this resolves and installs the signing box, so run it in the foreground
    # where that shows progress, rather than sitting silently in the backgrounded server's log.
    "${buck[@]}" build tine//tools:swtpm-signing.box

    pkcs11_dir=$(mktemp -d)
    "${buck[@]}" run tine//tools:signing-server -- "$pkcs11_dir" > "$pkcs11_dir/log" 2>&1 &
    server_pid=$!
    trap 'pkcs11_cleanup; box_cleanup' EXIT
    until [ -S "$pkcs11_dir/sock/pkcs11" ]; do
        # Any setup failure exits the server before it gets to listen; its log then says why.
        kill -0 "$server_pid" 2> /dev/null || return 1
        sleep 0.5
    done

    # A prod build host materializes the signing coordinates in a file and points the build at it
    cat > "$pkcs11_dir/signing.bcfg" <<EOF
[secure-boot-signing]
token = SecureBoot
pin-file = $pkcs11_dir/pin
socket = $pkcs11_dir/sock/pkcs11

[pcr-signing]
token = PcrPolicy
pin-file = $pkcs11_dir/pin
socket = $pkcs11_dir/sock/pkcs11
EOF
    local config=(--config-file "$pkcs11_dir/signing.bcfg")

    # Token signing uses the PKCS#11 provider and the p11-kit client. Only the Fedora box contains them.
    "${buck[@]}" build "${config[@]}" tine//examples/image-secureboot:image.fedora
    "${buck[@]}" build "${config[@]}" 'tine//examples/image-secureboot:image.fedora[uki]' --out "$pkcs11_dir/ukis"

    # The public key the UKI hands the booted system must be the PcrPolicy token's, and not the
    # Secure Boot one. Read each side into a variable first: a command substitution inside a `test`
    # argument is exempt from errexit, so a certificate that cannot be read would yield an empty
    # string and satisfy the inequality below without comparing anything.
    local box=("${buck[@]}" run tine//tools:swtpm-signing.box --) uki=("$pkcs11_dir"/ukis/*.efi)
    local pcrpkey pcr_certificate secure_boot_certificate
    # ukify takes the file before the options, per `ukify inspect --help`.
    "${box[@]}" ukify inspect "${uki[0]}" --section ".pcrpkey:binary@$pkcs11_dir/pcrpkey"
    pcrpkey=$("${box[@]}" openssl pkey -pubin -in "$pkcs11_dir/pcrpkey" -pubout)
    pcr_certificate=$("${box[@]}" openssl x509 -in "$pkcs11_dir/PcrPolicy.crt" -pubkey -noout)
    secure_boot_certificate=$("${box[@]}" openssl x509 -in "$pkcs11_dir/SecureBoot.crt" -pubkey -noout)
    test "$pcrpkey" = "$pcr_certificate"
    test "$pcrpkey" != "$secure_boot_certificate"

    if have_kvm; then "${buck[@]}" run "${config[@]}" tine//examples/image-secureboot:vm-smoke.fedora; fi
}

# Nothing here needs a box, so a graph that does not analyze is reported in seconds rather than
# after two bootstraps. `check` runs it again, for anyone running that on its own.
pattern=$(universe)
group graph             -- "${buck[@]}" bxl tine//tools/graph.bxl:analyze -- --pattern "$pattern"
# A build for another architecture, or for one distribution, skips what cannot be built for it. A target
# that is compatible but depends on an incompatible one fails these analyses instead of being skipped.
# `graph` already analyzes the graph for the host's architecture.
case $(uname -m) in
    x86_64) other=arm64 ;;
    *) other=x86_64 ;;
esac
platforms=("catalog//:$release.distribution")
# The catalog's default.* aliases have no architecture constraint. With TINE_TEST_DISTRO=arch.rolling,
# the analysis for arm64 fails on catalog//:default.release, because Arch serves x86_64 only. The run
# with the default boxes analyzes the other architecture.
if [ -z "${TINE_TEST_DISTRO:-}" ]; then
    platforms=("tine//platform:$other" "${platforms[@]}")
fi
for platform in "${platforms[@]}"; do
    group "graph-${platform##*:}" -- "${buck[@]}" bxl --target-platforms "$platform" tine//tools/graph.bxl:analyze -- --pattern "$pattern"
done
# First invocation fetches buck's pinned tools and bootstraps the release's root box; kept its own
# group so bootstrap time stays visible.
group box               -- "${buck[@]}" build "catalog//:$release.box"
group check             -- "${buck[@]}" run tine//tools:check -- --release "$release"
# Every repository the catalog declares is pinned to a mirror serving immutable snapshots, so the whole
# catalog is verifiable rather than the boxes that happen to be pinned.
if x86_only_catalog; then
    group verify-catalog -- "${buck[@]}" run tine//tools:verify-catalog
fi
# Everything the cell declares for the release, rather than the handful of targets someone remembered to
# name here: every example image, the boxes, and the source-build demos.
group build             -- whole_cell build
# Everything `check` left out: the boot smokes, which take minutes each, and the assertions about what
# the images above produced. Adding one is declaring it, not naming it here as well.
group image-tests       -- whole_cell test --include image "${vm_filter[@]}"
group secureboot-pkcs11 -- secureboot_pkcs11
# Expensive and distro independent.
if [ -z "${TINE_TEST_DISTRO:-}" ]; then
    # A change to the box must not change the Secure Boot example. The test builds in its own isolation dir.
    # Builds Fedora regardless of $TINE_TEST_DISTRO.
    group rebuild-reproducibility -- "${buck[@]}" run tine//tests:rebuild-reproducibility
    # The shared cache with a real Buck on both ends; in its own isolation dir.
    group remote-cache -- "${buck[@]}" run tine//tests:cache-roundtrip
fi
