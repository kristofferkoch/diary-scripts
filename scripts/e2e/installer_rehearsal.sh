#!/usr/bin/env bash
# Layer 4 of the pre-production harness: rehearse the real installers in a VM.
#
# Layers 1-3 exercise the code against scratch state. They cannot see an
# installed layout, the system Python, systemd unit ordering, SELinux labels, or
# an installer preflight that passes while the thing it checks is broken. This
# layer runs the production installers, in the documented order, against the
# approved checkouts inside a Fedora guest — never against an agent worktree and
# never on this host.
#
# The guest is the substrate; see scripts/e2e/vm.sh. It boots with systemd,
# SELinux enforcing and the host's package set, so labels and a real kernel are
# exercised instead of a container's. Without /dev/kvm (no EL2 in this guest) it
# runs under TCG: same guest, same assertions, slower.
#
# It needs qemu on this host and read access to the approved checkouts, so run
# it as the owner account:
#
#   scripts/e2e/installer_rehearsal.sh \
#       --approved-diary  /home/kek/diary-approved \
#       --approved-scripts /home/kek/diary-scripts-approved
#
# It fails loudly: every check prints PASS or FAIL, the first fatal problem
# stops the run with an explanation, and the exit status is non-zero if any
# check failed or any installer refused.
set -euo pipefail

readonly TARGET_DIARY=/home/kek/diary-approved
readonly TARGET_SCRIPTS=/home/kek/diary-scripts-approved
readonly INSTALL_TIMEOUT=${L4_INSTALL_TIMEOUT:-7200}

# shellcheck source=scripts/e2e/vm.sh
source "$(dirname "$0")/vm.sh"

APPROVED_DIARY=
APPROVED_SCRIPTS=
CASES=both
SCRATCH=
HAPPY_VM=
NEGATIVE_VM=
declare -a RESULTS=()
FAILED=0

usage() {
    cat <<'USAGE'
Usage: installer_rehearsal.sh --approved-diary DIR --approved-scripts DIR [options]

  --approved-diary DIR     approved diary checkout (clean, with .git)
  --approved-scripts DIR   approved diary-scripts checkout (clean, with .git)
  --accel MODE             auto (default), kvm, or tcg; auto uses kvm when this
                           host exposes /dev/kvm
  --cache-dir DIR          guest image cache (default: $XDG_CACHE_HOME/diary-e2e)
  --case WHICH             both (default), happy, or negative; the negative case
                           needs no Authelia build, so it is the cheap one to run
                           while debugging
  --keep                   keep the guest disks, console logs and scratch directory
  -h, --help               this text

Needs qemu-system-aarch64, qemu-img, cloud-utils and network access (the
fresh-authorization installer downloads and builds pinned Authelia). Nothing on
this host is modified: the guest is a throwaway overlay of a cached prepared
image, the approved checkouts are mounted read-only, and all state lives in the
scratch directory.
USAGE
}

log() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

record() {
    RESULTS+=("$1")
}

pass() {
    log "PASS  $1"
    record "PASS  $1"
}

fail_check() {
    log "FAIL  $1"
    record "FAIL  $1"
    FAILED=1
}

require_clean_checkout() {
    local path=$1 label=$2

    [[ -n $path ]] || die "missing $label path (see --help)"
    [[ -d $path ]] || die "$label is not a directory: $path"
    # A linked worktree has .git as a file, a plain checkout as a directory.
    [[ -d $path/.git || -f $path/.git ]] \
        || die "$label is not a Git checkout with private metadata: $path"
    [[ -r $path/.git ]] || die "$label metadata is not readable by $(id -un): $path"
    if [[ -n $(git -c safe.directory="$path" -C "$path" status --porcelain) ]]; then
        die "$label has uncommitted changes; a rehearsal must run against approved content: $path"
    fi
}

parse_args() {
    while (($#)); do
        case $1 in
            --approved-diary) APPROVED_DIARY=${2:?}; shift 2 ;;
            --approved-scripts) APPROVED_SCRIPTS=${2:?}; shift 2 ;;
            --accel) DIARY_E2E_ACCEL=${2:?}; shift 2 ;;
            --case)
                CASES=${2:?}
                case $CASES in
                    both | happy | negative) ;;
                    *) die "--case must be both, happy or negative: $CASES" ;;
                esac
                shift 2
                ;;
            --cache-dir) DIARY_E2E_CACHE=${2:?}; shift 2 ;;
            --keep) VM_KEEP=1; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "unknown argument: $1 (see --help)" ;;
        esac
    done
    export DIARY_E2E_ACCEL DIARY_E2E_CACHE VM_KEEP
}

cleanup() {
    local status=$?

    # The guests are always powered off; --keep only preserves their state.
    [[ -z $HAPPY_VM ]] || vm_stop "$HAPPY_VM" >/dev/null 2>&1 || true
    [[ -z $NEGATIVE_VM ]] || vm_stop "$NEGATIVE_VM" >/dev/null 2>&1 || true
    if ((VM_KEEP == 0)); then
        if [[ -n $SCRATCH && -d $SCRATCH ]]; then
            rm -rf -- "$SCRATCH"
        fi
    else
        log "kept guests: ${HAPPY_VM:-none} ${NEGATIVE_VM:-none}"
        log "kept scratch: ${SCRATCH:-none}"
    fi
    exit "$status"
}

trap cleanup EXIT INT TERM

check_substrate() {
    command -v qemu-system-aarch64 >/dev/null 2>&1 \
        || die "qemu-system-aarch64 is not installed; this layer needs a VM substrate"
    vm_init
    vm_ensure_base
}

start_guest() {
    local name=$1 scripts_mount=$2 diary_mount=$3

    # shellcheck disable=SC2034  # read by vm.sh
    VM_SHARES=()
    vm_share scripts "$scripts_mount"
    vm_share diary "$diary_mount"
    vm_start "$name"
    vm_mount_shares "$name" "diary=$TARGET_DIARY" "scripts=$TARGET_SCRIPTS"
}

# assert_substrate NAME — the properties the container could not provide.
assert_substrate() {
    local name=$1 enforcing=

    enforcing=$(vm_ssh "$name" getenforce 2>/dev/null || true)
    if [[ $enforcing == Enforcing ]]; then
        pass "guest SELinux enforcing"
    else
        fail_check "guest SELinux is not enforcing (found: ${enforcing:-none})"
    fi
    if vm_ssh "$name" test -b /dev/vda >/dev/null 2>&1; then
        pass "guest runs a real kernel over virtio disks"
    else
        fail_check "guest does not expose a virtio block device"
    fi
    if vm_ssh "$name" nft list tables >/dev/null 2>&1; then
        pass "guest nftables usable"
    else
        fail_check "guest nftables is not usable"
    fi
}

run_installer() {
    local name=$1 label=$2
    shift 2
    local logfile="${SCRATCH}/${label}.log"

    log "running ${label} ..."
    local status=0
    # The timeout runs in the guest: `timeout` cannot wrap a shell function, and
    # a guest-side timeout also survives losing the ssh connection.
    vm_ssh "$name" timeout "$INSTALL_TIMEOUT" "$@" >"$logfile" 2>&1 || status=$?
    if ((status == 0)); then
        pass "installer ${label}"
    elif ((status == 124)); then
        tail -25 "$logfile" >&2 || true
        fail_check "installer ${label} timed out after ${INSTALL_TIMEOUT}s (see ${logfile})"
    else
        tail -25 "$logfile" >&2 || true
        fail_check "installer ${label} (exit ${status}, see ${logfile})"
    fi
}

expect_in_guest() {
    local name=$1 label=$2
    shift 2
    if vm_ssh "$name" "$@" >/dev/null 2>&1; then
        pass "$label"
    else
        fail_check "$label"
    fi
}

expect_status() {
    local name=$1 label=$2 expected=$3 url=$4
    local actual
    actual=$(vm_ssh "$name" curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)
    if [[ $actual == "$expected" ]]; then
        pass "$label (HTTP $actual)"
    else
        fail_check "$label (expected HTTP $expected, got ${actual:-none})"
    fi
}

positive_case() {
    HAPPY_VM="diary-l4-happy-$$"
    log "== happy path: the three installers, in the documented order"
    start_guest "$HAPPY_VM" "$APPROVED_SCRIPTS" "$APPROVED_DIARY"
    pass "guest booted with the approved checkouts mounted read-only"
    assert_substrate "$HAPPY_VM"
    # Only denials from the rehearsal window count: the cloud image's own boot
    # is not what this layer is judging.
    local avc_since
    avc_since=$(vm_ssh "$HAPPY_VM" date --iso-8601=seconds)

    run_installer "$HAPPY_VM" audit \
        "$TARGET_DIARY/systemd/install-audit-root"
    # CTL-03's failsafe verifies the NFLOG forwarder when the controller starts,
    # and a missing unit answers exit 4 ("unknown"), not 3 ("inactive"), so AUD-02
    # has to be installed before CTL-01.
    run_installer "$HAPPY_VM" network-audit \
        "$TARGET_DIARY/systemd/install-network-audit-root"
    # The console installer restarts and checks diary-locked, which belongs to
    # the controller installer; a fresh guest needs it before the console.
    run_installer "$HAPPY_VM" controller \
        "$TARGET_DIARY/systemd/install-controller-root"
    run_installer "$HAPPY_VM" fresh \
        "$TARGET_DIARY/authelia/install"
    # dsh/agent-uid/install normally provides the root-owned approval client and
    # its helper module; the rehearsal runs only the installers, so lay down the
    # same pair. Installing only the entry point leaves the console unable to
    # import agent_merge_pii and the owner-UI installer refuses.
    vm_ssh "$HAPPY_VM" install -o root -g root -m 0755 \
        "$TARGET_SCRIPTS/scripts/agent_merge_queue.py" /usr/local/libexec/agent-merge-queue \
        >>"${SCRATCH}/approval-client.log" 2>&1 \
        || die "could not install the approval client in $HAPPY_VM"
    vm_ssh "$HAPPY_VM" install -o root -g root -m 0644 \
        "$TARGET_SCRIPTS/scripts/agent_merge_pii.py" /usr/local/libexec/agent_merge_pii.py \
        >>"${SCRATCH}/approval-client.log" 2>&1 \
        || die "could not install the approval client helper in $HAPPY_VM"
    run_installer "$HAPPY_VM" console \
        "$TARGET_DIARY/systemd/install-owner-ui-root"

    expect_in_guest "$HAPPY_VM" "audit daemon active" systemctl is-active diary-auditd
    expect_in_guest "$HAPPY_VM" "NFLOG forwarder active" \
        systemctl is-active diary-network-auditd
    expect_in_guest "$HAPPY_VM" "controller active and locked" \
        systemctl is-active diary-lockd diary-locked
    expect_in_guest "$HAPPY_VM" "console host active" systemctl is-active diary-lock-ui
    expect_status "$HAPPY_VM" "console health" 200 http://localhost:9093/lock/health
    expect_status "$HAPPY_VM" "no unlock route on the console" 404 \
        http://localhost:9093/lock/api/lock
    expect_in_guest "$HAPPY_VM" "UI enablement file absent" \
        test '!' -e /etc/diary/lock-ui-actions-enabled
    expect_in_guest "$HAPPY_VM" "enforcement arm file absent" \
        test '!' -e /etc/diary/network-policy-enabled
    expect_in_guest "$HAPPY_VM" "audit preflight passes" \
        diary-audit preflight
    expect_in_guest "$HAPPY_VM" "installed client matches approved" \
        /usr/local/libexec/agent-merge-queue doctor --approved \
        "$TARGET_SCRIPTS/scripts/agent_merge_queue.py"
    expect_in_guest "$HAPPY_VM" "no SELinux AVC denials during the rehearsal" \
        bash -c "! journalctl -b --no-pager --quiet --since '$avc_since' -g 'avc:  *denied'"
}

write_prefix_scripts() {
    local target=$1

    # rsync rather than cp: the approved tree carries setgid worktree
    # directories, and cp cannot recreate that mode without privileges. The
    # fixture only needs content — no .git, no ownership, no SELinux context.
    install -d -- "$target"
    rsync -rlt --no-owner --no-group --no-perms --exclude=.git \
        "$APPROVED_SCRIPTS"/ "$target"/
    # Keep the daemon's validator and the preflight subcommand, remove only the
    # journal field mapping: this is the production failure the functional
    # preflight exists to catch.
    sed -i '/"repository": "DIARY_AUDIT_REPOSITORY",/d' "$target/scripts/diary_audit.py"
    if grep -q 'DIARY_AUDIT_REPOSITORY' "$target/scripts/diary_audit.py"; then
        die "could not build the pre-fix daemon fixture (mapping still present)"
    fi
    python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' \
        "$target/scripts/diary_audit.py" \
        || die "pre-fix daemon fixture does not compile"
}

negative_case() {
    local prefix="$SCRATCH/pre-fix-scripts"

    NEGATIVE_VM="diary-l4-negative-$$"
    log "== negative case: a daemon that accepts the field but cannot journal it"
    write_prefix_scripts "$prefix"
    pass "pre-fix daemon fixture built (validator intact, journal mapping removed)"

    start_guest "$NEGATIVE_VM" "$prefix" "$APPROVED_DIARY"
    pass "guest booted with the pre-fix tree mounted read-only"

    run_installer "$NEGATIVE_VM" pre-fix-audit \
        "$TARGET_DIARY/systemd/install-audit-root"
    run_installer "$NEGATIVE_VM" pre-fix-network-audit \
        "$TARGET_DIARY/systemd/install-network-audit-root"
    run_installer "$NEGATIVE_VM" pre-fix-controller \
        "$TARGET_DIARY/systemd/install-controller-root"

    local logfile="${SCRATCH}/console-refused.log"
    if vm_ssh "$NEGATIVE_VM" "$TARGET_DIARY/systemd/install-owner-ui-root" \
        >"$logfile" 2>&1; then
        fail_check "console installer refuses a daemon that drops the repository field (it installed anyway)"
    elif grep -q 'does not journal the repository field' "$logfile"; then
        pass "console installer refuses the broken daemon with the preflight message"
    else
        tail -20 "$logfile" >&2 || true
        fail_check "console installer refused, but not because of the audit preflight (see ${logfile})"
    fi

    if vm_ssh "$NEGATIVE_VM" systemctl is-enabled diary-lock-ui >/dev/null 2>&1; then
        fail_check "console unit must not be enabled after a refused install"
    else
        pass "console unit not enabled after the refused install"
    fi
}

summarise() {
    log
    log "== rehearsal summary"
    local line
    for line in "${RESULTS[@]}"; do
        log "  $line"
    done
    log
    if ((FAILED)); then
        log "RESULT: FAILED (${#RESULTS[@]} checks, at least one failed)"
        return 1
    fi
    log "RESULT: PASSED (${#RESULTS[@]} checks)"
    return 0
}

main() {
    parse_args "$@"
    require_clean_checkout "$APPROVED_SCRIPTS" "approved scripts checkout"
    require_clean_checkout "$APPROVED_DIARY" "approved diary checkout"
    SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/diary-l4.XXXXXX")
    log "scratch: $SCRATCH"
    check_substrate
    # shellcheck disable=SC2154  # set by vm_init in vm.sh
    record "SUBSTRATE accel=${vm_accel} cpus=${VM_CPUS} mem=${VM_MEMORY}MiB disk=${VM_DISK_SIZE} release=${VM_RELEASE}"
    case $CASES in
        happy) positive_case ;;
        negative) negative_case ;;
        both) positive_case; negative_case ;;
    esac
    summarise
}

main "$@"
