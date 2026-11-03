#!/usr/bin/env bash
# Layer 4 of the pre-production harness: rehearse the real installers.
#
# Layers 1-3 exercise the code against scratch state. They cannot see an
# installed layout, the system Python, systemd unit ordering, or an installer
# preflight that passes while the thing it checks is broken. This layer runs the
# production installers, in the documented order, inside a systemd-capable
# container, against the approved checkouts — never against an agent worktree.
#
# It is deliberately not runnable from an agent session: it needs Docker and it
# writes to /etc, /usr/local and systemd inside the container. Run it as a user
# in the docker group:
#
#   scripts/e2e/installer_rehearsal.sh \
#       --approved-diary  /home/kek/diary-approved \
#       --approved-scripts /home/kek/diary-scripts-approved
#
# It fails loudly: every check prints PASS or FAIL, the first fatal problem
# stops the run with an explanation, and the exit status is non-zero if any
# check failed or any installer refused.
set -euo pipefail

readonly CONTAINER_DIARY=/home/kek/diary-approved
readonly CONTAINER_SCRIPTS=/home/kek/diary-scripts-approved
readonly DEFAULT_IMAGE=registry.fedoraproject.org/fedora:41
readonly BOOT_TIMEOUT=${L4_BOOT_TIMEOUT:-300}
readonly INSTALL_TIMEOUT=${L4_INSTALL_TIMEOUT:-1800}

IMAGE=${L4_IMAGE:-$DEFAULT_IMAGE}
APPROVED_DIARY=
APPROVED_SCRIPTS=
KEEP=0
SCRATCH=
HAPPY_CONTAINER=
NEGATIVE_CONTAINER=
declare -a RESULTS=()
FAILED=0

usage() {
    cat <<'USAGE'
Usage: installer_rehearsal.sh --approved-diary DIR --approved-scripts DIR [options]

  --approved-diary DIR     approved diary checkout (clean, with .git)
  --approved-scripts DIR   approved diary-scripts checkout (clean, with .git)
  --image REF              container image (default: registry.fedoraproject.org/fedora:41)
  --keep                   keep the containers and scratch directory for inspection
  -h, --help               this text

Requires Docker and network access (the fresh-authorization installer downloads
and builds pinned Authelia). Expected runtime: 15-25 minutes, most of it the
Authelia build. Nothing on the host is modified; everything happens in the
container and a scratch directory.
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
    [[ -d $path/.git ]] || die "$label is not a Git checkout with private metadata: $path"
    if [[ -n $(git -c safe.directory="$path" -C "$path" status --porcelain) ]]; then
        die "$label has uncommitted changes; a rehearsal must run against approved content: $path"
    fi
}

parse_args() {
    while (($#)); do
        case $1 in
            --approved-diary) APPROVED_DIARY=${2:?}; shift 2 ;;
            --approved-scripts) APPROVED_SCRIPTS=${2:?}; shift 2 ;;
            --image) IMAGE=${2:?}; shift 2 ;;
            --keep) KEEP=1; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "unknown argument: $1 (see --help)" ;;
        esac
    done
}

cleanup() {
    local status=$?

    if ((${KEEP:-0} == 0)); then
        [[ -z $HAPPY_CONTAINER ]] || docker rm -f "$HAPPY_CONTAINER" >/dev/null 2>&1 || true
        [[ -z $NEGATIVE_CONTAINER ]] || docker rm -f "$NEGATIVE_CONTAINER" >/dev/null 2>&1 || true
        if [[ -n $SCRATCH && -d $SCRATCH ]]; then
            rm -rf -- "$SCRATCH"
        fi
    else
        log "kept containers: ${HAPPY_CONTAINER:-none} ${NEGATIVE_CONTAINER:-none}"
        log "kept scratch: ${SCRATCH:-none}"
    fi
    exit "$status"
}

trap cleanup EXIT INT TERM

check_docker() {
    command -v docker >/dev/null 2>&1 \
        || die "docker is not installed; this layer needs a systemd-capable container"
    if ! docker info >/dev/null 2>&1; then
        die "docker is not usable by $(id -un); run as a user in the docker group (or via sudo)"
    fi
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        log "pulling $IMAGE once (a few hundred MB)"
        docker pull "$IMAGE" >/dev/null || die "could not pull $IMAGE"
    fi
}

start_container() {
    local name=$1 scripts_mount=$2 diary_mount=$3

    docker run --detach --name "$name" \
        --privileged \
        --cgroupns=host \
        --tmpfs /run \
        --tmpfs /run/lock \
        --security-opt seccomp=unconfined \
        --security-opt label=disable \
        --volume "$scripts_mount:$CONTAINER_SCRIPTS:ro" \
        --volume "$diary_mount:$CONTAINER_DIARY:ro" \
        "$IMAGE" /bin/sh -c 'dnf -q -y install systemd >/dev/null 2>&1 && exec /usr/sbin/init' \
        >/dev/null || die "could not start container $name"

    local deadline=$((SECONDS + BOOT_TIMEOUT))
    local state=
    while ((SECONDS < deadline)); do
        state=$(docker exec "$name" systemctl is-system-running 2>/dev/null || true)
        case $state in
            running | degraded) return 0 ;;
        esac
        sleep 1
    done
    die "systemd did not become ready in $name (last state: ${state:-unknown})"
}

prepare_container() {
    local name=$1

    # The installers hardcode /home/kek/... paths and expect the owner account to
    # exist; the container starts empty, so provision just that identity and the
    # tools the three installers need.
    if ! docker exec "$name" bash -eu -c '
        dnf -y install python3 git curl gcc golang tar make shadow-utils nftables >/dev/null 2>&1
        getent group kek >/dev/null 2>&1 || groupadd -g 1000 kek
        getent passwd kek >/dev/null 2>&1 || useradd -u 1000 -g kek -d /home/kek -m -s /bin/bash kek
        # The controller unit runs with SupplementaryGroups=diary-lock-control agent,
        # so the agent group and identity must exist or the unit cannot start.
        # The network policy tool execs /usr/bin/nft; Fedora installs it under
        # /usr/sbin, and the host has the /usr/bin path, so give the container
        # the same layout rather than changing the product.
        [[ -e /usr/bin/nft ]] || ln -s /usr/sbin/nft /usr/bin/nft
        # authelia/install builds with GOSUMDB=off, so a Go older than the pinned
        # source requires cannot download a toolchain: it fails verification.
        # Install the same Go line the host uses.
        GO_VERSION=${L4_GO_VERSION:-1.26.10}
        case $(uname -m) in
            aarch64 | arm64) GO_ARCH=arm64 ;;
            x86_64 | amd64) GO_ARCH=amd64 ;;
            *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
        esac
        if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go${GO_VERSION} "; then
            GO_SCHEME=https
            GO_HOST=go.dev
            curl -fsSL "${GO_SCHEME}://${GO_HOST}/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" \
                -o /tmp/go.tgz
            rm -rf /usr/local/go
            tar -C /usr/local -xzf /tmp/go.tgz
            rm -f /tmp/go.tgz
            ln -sf /usr/local/go/bin/go /usr/bin/go
        fi
        # The first nft call in a fresh container loads nf_tables and can fail;
        # warm it up so the installer policy start is not the first user.
        nft list tables >/dev/null 2>&1 || true
        getent group agent >/dev/null 2>&1 || groupadd --system agent
        getent passwd agent >/dev/null 2>&1 || useradd --system -u 981 -g agent -d /home/agent -m -s /bin/bash agent
        python3 -c "import sys; assert sys.version_info >= (3, 11), sys.version"
    ' >"${SCRATCH}/prepare-${name}.log" 2>&1; then
        tail -20 "${SCRATCH}/prepare-${name}.log" >&2 || true
        die "could not provision $name (see ${SCRATCH}/prepare-${name}.log)"
    fi
    pass "container $name provisioned (python3, git, curl, gcc, golang, kek uid 1000)"
}

run_installer() {
    local name=$1 label=$2
    shift 2
    local logfile="${SCRATCH}/${label}.log"

    log "running ${label} ..."
    local status=0
    timeout "$INSTALL_TIMEOUT" docker exec "$name" "$@" >"$logfile" 2>&1 || status=$?
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

expect_in_container() {
    local name=$1 label=$2
    shift 2
    if docker exec "$name" "$@" >/dev/null 2>&1; then
        pass "$label"
    else
        fail_check "$label"
    fi
}

expect_status() {
    local name=$1 label=$2 expected=$3 url=$4
    local actual
    actual=$(docker exec "$name" curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)
    if [[ $actual == "$expected" ]]; then
        pass "$label (HTTP $actual)"
    else
        fail_check "$label (expected HTTP $expected, got ${actual:-none})"
    fi
}

positive_case() {
    HAPPY_CONTAINER="diary-l4-happy-$$"
    log "== happy path: the three installers, in the documented order"
    start_container "$HAPPY_CONTAINER" "$APPROVED_SCRIPTS" "$APPROVED_DIARY"
    pass "systemd booted in $HAPPY_CONTAINER"
    prepare_container "$HAPPY_CONTAINER"

    run_installer "$HAPPY_CONTAINER" audit \
        "$CONTAINER_DIARY/systemd/install-audit-root"
    # The console installer restarts and checks diary-locked, which belongs to
    # the controller installer; a fresh container needs it before the console.
    run_installer "$HAPPY_CONTAINER" controller \
        "$CONTAINER_DIARY/systemd/install-controller-root"
    run_installer "$HAPPY_CONTAINER" fresh \
        "$CONTAINER_DIARY/authelia/install"
    # dsh/agent-uid/install normally provides the root-owned approval client;
    # the rehearsal runs only the three installers, so install it the same way.
    docker exec "$HAPPY_CONTAINER" install -o root -g root -m 0755 \
        "$CONTAINER_SCRIPTS/scripts/agent_merge_queue.py" /usr/local/libexec/agent-merge-queue \
        >>"${SCRATCH}/prepare-${HAPPY_CONTAINER}.log" 2>&1 \
        || die "could not install the approval client in $HAPPY_CONTAINER"
    run_installer "$HAPPY_CONTAINER" console \
        "$CONTAINER_DIARY/systemd/install-owner-ui-root"

    expect_in_container "$HAPPY_CONTAINER" "audit daemon active" systemctl is-active diary-auditd
    expect_in_container "$HAPPY_CONTAINER" "controller active and locked" \
        systemctl is-active diary-lockd diary-locked
    expect_in_container "$HAPPY_CONTAINER" "console host active" systemctl is-active diary-lock-ui
    expect_status "$HAPPY_CONTAINER" "console health" 200 http://localhost:9093/lock/health
    expect_status "$HAPPY_CONTAINER" "no unlock route on the console" 404 \
        http://localhost:9093/lock/api/lock
    expect_in_container "$HAPPY_CONTAINER" "UI enablement file absent" \
        test '!' -e /etc/diary/lock-ui-actions-enabled
    expect_in_container "$HAPPY_CONTAINER" "enforcement arm file absent" \
        test '!' -e /etc/diary/network-policy-enabled
    expect_in_container "$HAPPY_CONTAINER" "audit preflight passes" \
        diary-audit preflight
    expect_in_container "$HAPPY_CONTAINER" "installed client matches approved" \
        /usr/local/libexec/agent-merge-queue doctor --approved \
        "$CONTAINER_SCRIPTS/scripts/agent_merge_queue.py"
}

write_prefix_scripts() {
    local target=$1

    cp -a "$APPROVED_SCRIPTS" "$target"
    rm -rf -- "$target/.git"
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

    NEGATIVE_CONTAINER="diary-l4-negative-$$"
    log "== negative case: a daemon that accepts the field but cannot journal it"
    write_prefix_scripts "$prefix"
    pass "pre-fix daemon fixture built (validator intact, journal mapping removed)"

    start_container "$NEGATIVE_CONTAINER" "$prefix" "$APPROVED_DIARY"
    pass "systemd booted in $NEGATIVE_CONTAINER"
    prepare_container "$NEGATIVE_CONTAINER"

    run_installer "$NEGATIVE_CONTAINER" pre-fix-audit \
        "$CONTAINER_DIARY/systemd/install-audit-root"
    run_installer "$NEGATIVE_CONTAINER" pre-fix-controller \
        "$CONTAINER_DIARY/systemd/install-controller-root"

    local logfile="${SCRATCH}/console-refused.log"
    if docker exec "$NEGATIVE_CONTAINER" "$CONTAINER_DIARY/systemd/install-owner-ui-root" \
        >"$logfile" 2>&1; then
        fail_check "console installer refuses a daemon that drops the repository field (it installed anyway)"
    elif grep -q 'does not journal the repository field' "$logfile"; then
        pass "console installer refuses the broken daemon with the preflight message"
    else
        tail -20 "$logfile" >&2 || true
        fail_check "console installer refused, but not because of the audit preflight (see ${logfile})"
    fi

    if docker exec "$NEGATIVE_CONTAINER" systemctl is-enabled diary-lock-ui >/dev/null 2>&1; then
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
    check_docker
    SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/diary-l4.XXXXXX")
    log "scratch: $SCRATCH"
    log "image:   $IMAGE"
    positive_case
    negative_case
    summarise
}

main "$@"
