#!/usr/bin/env bash
# shellcheck shell=bash
# VM substrate for layer 4 — the installer rehearsal.
#
# The rehearsal used to run in a systemd-capable container. A container cannot
# show SELinux labels, the exact package set, or a real kernel policy load, and
# on this host it also ran out of container-layer I/O during the Authelia
# build. This file boots a Fedora Cloud guest instead and drives it over ssh.
#
# Acceleration: KVM when the host exposes /dev/kvm — on arm64 that means the
# outer hypervisor put this guest at EL2, so `-accel kvm -cpu host` is the same
# flag it is on x86. Without EL2 the guest has no /dev/kvm and everything runs
# under TCG; the guest, the installers and the assertions are identical, only
# slower. The chosen accelerator is printed and recorded with the run.
#
# Content reaches the guest as read-only 9p shares (vm_share); the approved
# checkouts are never copied into, or written from, the guest.
#
# Usage from installer_rehearsal.sh:
#
#   source "$(dirname "$0")/vm.sh"
#   vm_init                       # tools, cache, key, accelerator
#   vm_ensure_base                # download + provision once, then cached
#   vm_share diary "$APPROVED_DIARY"
#   vm_share scripts "$APPROVED_SCRIPTS"
#   vm_start happy
#   vm_exec happy systemctl is-active diary-auditd
#   vm_stop happy
#
# Environment:
#   DIARY_E2E_CACHE    cache directory (default: $XDG_CACHE_HOME/diary-e2e)
#   DIARY_E2E_ACCEL    auto (default) | kvm | tcg
#   DIARY_E2E_RELEASE  Fedora cloud release, e.g. 44-1.7
#   DIARY_E2E_MEMORY   guest RAM in MiB (default 2048)
#   DIARY_E2E_CPUS     guest vCPUs (default 4)
#   DIARY_E2E_DISK     guest disk size (default 12G)
#   L4_BOOT_TIMEOUT    seconds to wait for systemd (default 900; TCG is slow)

VM_CACHE=${DIARY_E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/diary-e2e}
VM_RELEASE=${DIARY_E2E_RELEASE:-44-1.7}
VM_ARCH=aarch64
VM_ACCEL_REQUEST=${DIARY_E2E_ACCEL:-auto}
VM_MEMORY=${DIARY_E2E_MEMORY:-2048}
VM_CPUS=${DIARY_E2E_CPUS:-4}
VM_DISK_SIZE=${DIARY_E2E_DISK:-12G}
VM_BOOT_TIMEOUT=${L4_BOOT_TIMEOUT:-900}
VM_GO_VERSION=${L4_GO_VERSION:-1.26.10}

VM_REPO_RELEASE=${VM_RELEASE%-*}
VM_IMAGES_URL="https://dl.fedoraproject.org/pub/fedora/linux/releases/${VM_REPO_RELEASE}/Cloud/${VM_ARCH}/images"
VM_BASE_NAME="Fedora-Cloud-Base-Generic-${VM_RELEASE}.${VM_ARCH}.qcow2"
VM_CHECKSUM_NAME="Fedora-Cloud-${VM_RELEASE}-${VM_ARCH}-CHECKSUM"
VM_IMAGE=$VM_CACHE/$VM_BASE_NAME
VM_PREPARED=$VM_CACHE/prepared-${VM_RELEASE}.qcow2
VM_KEY=$VM_CACHE/id_ed25519
VM_EFI_CODE=${DIARY_E2E_EFI_CODE:-/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw}
VM_EFI_VARS=${DIARY_E2E_EFI_VARS:-/usr/share/edk2/aarch64/QEMU_EFI-qemuvars-pflash.raw}

declare -A VM_PORT=() VM_PID=() VM_DIR=()
declare -a VM_SHARES=()

VM_KEEP=${VM_KEEP:-0}
vm_accel=
vm_accel_args=
vm_cpu=
vm_runs=

vm_log() { printf '%s\n' "$*"; }

vm_die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 2
}

vm_resolve_accel() {
    case $VM_ACCEL_REQUEST in
        kvm)
            [[ -w /dev/kvm ]] || vm_die "--accel kvm requested but /dev/kvm is not present or not writable"
            printf 'kvm\n'
            ;;
        tcg) printf 'tcg\n' ;;
        auto)
            if [[ -w /dev/kvm ]]; then printf 'kvm\n'; else printf 'tcg\n'; fi
            ;;
        *) vm_die "unknown accelerator: $VM_ACCEL_REQUEST (auto|kvm|tcg)" ;;
    esac
}

vm_init() {
    # The caller parses --accel/--cache-dir after sourcing this file, so the
    # environment is authoritative here.
    VM_ACCEL_REQUEST=${DIARY_E2E_ACCEL:-$VM_ACCEL_REQUEST}
    VM_MEMORY=${DIARY_E2E_MEMORY:-$VM_MEMORY}
    VM_CPUS=${DIARY_E2E_CPUS:-$VM_CPUS}
    VM_DISK_SIZE=${DIARY_E2E_DISK:-$VM_DISK_SIZE}
    VM_CACHE=${DIARY_E2E_CACHE:-$VM_CACHE}
    VM_IMAGE=$VM_CACHE/$VM_BASE_NAME
    VM_PREPARED=$VM_CACHE/prepared-${VM_RELEASE}.qcow2
    VM_KEY=$VM_CACHE/id_ed25519

    local missing=() tool
    for tool in qemu-system-aarch64 qemu-img cloud-localds ssh ssh-keygen curl sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    ((${#missing[@]} == 0)) \
        || vm_die "the VM substrate needs: ${missing[*]} (dnf install qemu-system-aarch64 qemu-img cloud-utils openssh-clients)"
    [[ -r $VM_EFI_CODE ]] || vm_die "UEFI firmware not found: $VM_EFI_CODE (dnf install edk2-aarch64)"
    [[ -r $VM_EFI_VARS ]] || vm_die "UEFI variable store template not found: $VM_EFI_VARS"

    mkdir -p "$VM_CACHE" || vm_die "cannot create the cache directory: $VM_CACHE"
    chmod 0700 "$VM_CACHE"

    vm_accel=$(vm_resolve_accel)
    if [[ $vm_accel == kvm ]]; then
        vm_accel_args=kvm
        vm_cpu=host
    else
        # MTTCG: one host thread per guest vCPU — the only way TCG scales.
        vm_accel_args=tcg,thread=multi
        vm_cpu=max
    fi

    [[ -f $VM_KEY ]] || ssh-keygen -q -t ed25519 -N '' -C diary-e2e -f "$VM_KEY" \
        || vm_die "could not create $VM_KEY"
    chmod 0600 "$VM_KEY"

    vm_runs=$VM_CACHE/runs
    mkdir -p "$vm_runs"

    vm_log "substrate: qemu $(qemu-system-aarch64 --version | head -1 | awk '{print $4}') accel=${vm_accel} cpu=${vm_cpu} cpus=${VM_CPUS} mem=${VM_MEMORY}MiB disk=${VM_DISK_SIZE}"
    if [[ $vm_accel == tcg ]]; then
        vm_log "substrate: no /dev/kvm (arm64 needs EL2 from the outer hypervisor); TCG fallback — same guest, same assertions, much slower"
    fi
}

vm_free_port() {
    local port
    for _ in $(seq 1 50); do
        port=$(( 20000 + RANDOM % 10000 ))
        if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            printf '%s\n' "$port"
            return 0
        fi
    done
    vm_die "could not find a free loopback port for the guest ssh forward"
}

vm_ssh_options() {
    # -F /dev/null: the guest connection must not depend on this host's ssh
    # configuration (an unreadable /etc/ssh/ssh_config.d makes ssh refuse to run
    # at all, and host keys or proxies for other hosts must not leak in here).
    printf '%s\n' \
        -F /dev/null \
        -i "$VM_KEY" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=5 \
        -T
}

vm_ssh() {
    local name=$1
    shift
    local port=${VM_PORT[$name]:-}
    [[ -n $port ]] || vm_die "vm_ssh: no such guest: $name"
    local cmd='' arg
    for arg in "$@"; do cmd+=" $(printf '%q' "$arg")"; done
    local -a opts=()
    while IFS= read -r line; do opts+=("$line"); done < <(vm_ssh_options)
    ssh "${opts[@]}" -p "$port" root@127.0.0.1 "$cmd"
}

# vm_script NAME — run the bash script on stdin in the guest.
vm_script() {
    local name=$1
    local port=${VM_PORT[$name]:-}
    [[ -n $port ]] || vm_die "vm_script: no such guest: $name"
    local -a opts=()
    while IFS= read -r line; do opts+=("$line"); done < <(vm_ssh_options)
    ssh "${opts[@]}" -p "$port" root@127.0.0.1 'bash -euo pipefail -s'
}

vm_share() {
    local tag=$1 path=$2
    [[ -d $path ]] || vm_die "vm_share: not a directory: $path"
    VM_SHARES+=("$tag=$path")
}

# vm_mount_shares NAME TAG=PATH... — mount read-only 9p shares in the guest.
vm_mount_shares() {
    local name=$1
    shift
    local spec tag path
    for spec in "$@"; do
        tag=${spec%%=*}
        path=${spec#*=}
        vm_ssh "$name" mkdir -p "$path" || vm_die "could not create $path in $name"
        vm_ssh "$name" mount -t 9p -o trans=virtio,version=9p2000.L,ro "$tag" "$path" \
            || vm_die "could not mount the read-only share '$tag' at $path in $name"
    done
}

vm_boot() {
    local name=$1 disk=$2 seed=${3:-}
    local dir=$vm_runs/$name
    mkdir -p "$dir"
    # Only our own artifacts are replaced; the caller owns the disk image.
    rm -f -- "$dir/efivars.fd" "$dir/console.log" "$dir/qemu.log" "$dir/invocation"
    # The first boot writes the boot entry into the variable store. Reuse that
    # store afterwards so every guest boots the same way instead of relying on
    # the firmware's fallback path.
    local vars_source=$VM_EFI_VARS
    [[ -f $VM_CACHE/efivars.fd ]] && vars_source=$VM_CACHE/efivars.fd
    cp -- "$vars_source" "$dir/efivars.fd"
    VM_DIR[$name]=$dir
    VM_PORT[$name]=$(vm_free_port)

    # shellcheck disable=SC2054  # commas inside -drive/-machine values are not element separators
    local -a args=(
        -machine "virt,gic-version=3"
        -cpu "$vm_cpu"
        -accel "$vm_accel_args"
        -smp "$VM_CPUS"
        -m "$VM_MEMORY"
        -drive "if=pflash,format=raw,unit=0,readonly=on,file=$VM_EFI_CODE"
        -drive "if=pflash,format=raw,unit=1,file=$dir/efivars.fd"
        -drive "if=virtio,format=qcow2,file=$disk,cache=writeback"
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${VM_PORT[$name]}-:22"
        -device virtio-net-pci,netdev=net0
        -display none
        -monitor none
        -serial "file:$dir/console.log"
        -no-reboot
    )
    [[ -z $seed ]] || args+=(-drive "if=virtio,format=raw,file=$seed,readonly=on")
    local spec
    for spec in "${VM_SHARES[@]}"; do
        args+=(-virtfs "local,path=${spec#*=},mount_tag=${spec%%=*},security_model=none,readonly=on,multidevs=remap")
    done

    { printf '%q ' qemu-system-aarch64 "${args[@]}"; printf '\n'; } >"$dir/invocation"
    qemu-system-aarch64 "${args[@]}" >"$dir/qemu.log" 2>&1 &
    VM_PID[$name]=$!
}

vm_wait_ssh() {
    local name=$1 label=${2:-$1}
    local dir=${VM_DIR[$name]}
    local deadline=$((SECONDS + VM_BOOT_TIMEOUT))
    local state=
    while ((SECONDS < deadline)); do
        if ! kill -0 "${VM_PID[$name]}" 2>/dev/null; then
            tail -20 "$dir/console.log" >&2 2>/dev/null || true
            vm_die "$label: qemu exited before the guest answered (console: $dir/console.log)"
        fi
        state=$(vm_ssh "$name" systemctl is-system-running 2>/dev/null || true)
        case $state in
            running | degraded) return 0 ;;
        esac
        sleep 2
    done
    tail -20 "$dir/console.log" >&2 2>/dev/null || true
    vm_die "$label: systemd did not become ready within ${VM_BOOT_TIMEOUT}s (last state: ${state:-unreachable})"
}

vm_poweroff() {
    local name=$1
    vm_ssh "$name" systemctl poweroff >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 300))
    while kill -0 "${VM_PID[$name]}" 2>/dev/null; do
        if ((SECONDS >= deadline)); then
            vm_log "poweroff did not finish for $name; killing qemu"
            kill "${VM_PID[$name]}" 2>/dev/null || true
            break
        fi
        sleep 1
    done
    wait "${VM_PID[$name]}" 2>/dev/null || true
}

vm_start() {
    local name=$1
    [[ -f $VM_PREPARED ]] || vm_die "no prepared base image: $VM_PREPARED (run vm_ensure_base first)"
    local dir=$vm_runs/$name
    mkdir -p "$dir"
    local disk=$dir/disk.qcow2
    rm -f -- "$disk"
    qemu-img create -q -f qcow2 -b "$VM_PREPARED" -F qcow2 "$disk" \
        || vm_die "could not create the overlay for $name"
    vm_boot "$name" "$disk"
    vm_wait_ssh "$name"
    vm_log "guest $name: ssh 127.0.0.1:${VM_PORT[$name]} (console: ${VM_DIR[$name]}/console.log)"
}

vm_stop() {
    local name=$1
    [[ -n ${VM_PID[$name]:-} ]] || return 0
    vm_poweroff "$name"
    if ((VM_KEEP)); then
        vm_log "kept guest state: ${VM_DIR[$name]}"
    else
        rm -rf -- "${VM_DIR[$name]}"
    fi
    unset "VM_PID[$name]" "VM_PORT[$name]" "VM_DIR[$name]"
}

vm_download_base() {
    [[ -f $VM_IMAGE ]] && return 0
    local tmp=$VM_CACHE/.download.$$
    local sums=$VM_CACHE/$VM_CHECKSUM_NAME
    vm_log "downloading ${VM_BASE_NAME} (one-time, ~700 MB)"
    curl -fsSL --retry 3 -o "$tmp" "$VM_IMAGES_URL/$VM_BASE_NAME" \
        || { rm -f "$tmp"; vm_die "could not download $VM_IMAGES_URL/$VM_BASE_NAME"; }
    curl -fsSL --retry 3 -o "$sums.tmp" "$VM_IMAGES_URL/$VM_CHECKSUM_NAME" \
        || { rm -f "$tmp" "$sums.tmp"; vm_die "could not download the image checksum"; }
    mv "$sums.tmp" "$sums"
    local expected actual
    # Format: "SHA256 (name) = hash"
    expected=$(awk -v name="$VM_BASE_NAME" '$1 == "SHA256" && $2 == "(" name ")" { print $NF }' "$sums")
    actual=$(sha256sum "$tmp" | cut -d' ' -f1)
    [[ -n $expected && $expected == "$actual" ]] \
        || { rm -f "$tmp"; vm_die "checksum mismatch for $VM_BASE_NAME (expected ${expected:-none}, got $actual)"; }
    mv "$tmp" "$VM_IMAGE"
    vm_log "base image verified: $(basename "$VM_IMAGE")"
}

vm_write_seed() {
    local dir=$1
    mkdir -p "$dir"
    cat >"$dir/meta-data" <<EOF
instance-id: diary-e2e-base
local-hostname: diary-e2e-base
EOF
    cat >"$dir/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$VM_KEY.pub")
write_files:
  - path: /etc/ssh/sshd_config.d/99-e2e-rehearsal.conf
    permissions: '0644'
    content: |
      PermitRootLogin prohibit-password
      PasswordAuthentication no
runcmd:
  - [ systemctl, try-reload-or-restart, sshd.service ]
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
package_update: false
EOF
    cloud-localds "$dir/seed.iso" "$dir/user-data" "$dir/meta-data" \
        || vm_die "cloud-localds could not build the boot seed"
}

vm_provision_base() {
    local name=$1
    vm_script "$name" <<PROVISION
echo "provisioning: packages"
dnf -y --setopt=install_weak_deps=False install \
    python3 git curl gcc golang tar make shadow-utils nftables policycoreutils \
    >/tmp/dnf.log 2>&1 || { tail -30 /tmp/dnf.log; exit 1; }
echo "provisioning: identities"
getent group kek >/dev/null || groupadd -g 1000 kek
getent passwd kek >/dev/null || useradd -u 1000 -g kek -d /home/kek -m -s /bin/bash kek
getent group agent >/dev/null || groupadd --system agent
getent passwd agent >/dev/null || useradd --system -u 981 -g agent -d /home/agent -m -s /bin/bash agent
# The network policy tool execs /usr/bin/nft; Fedora installs it under
# /usr/sbin and the host has the /usr/bin path, so match the host layout.
[[ -e /usr/bin/nft ]] || ln -s /usr/sbin/nft /usr/bin/nft
# authelia/install builds with GOSUMDB=off, so an older Go than the pinned
# source requires cannot fetch a toolchain; install the host's Go line.
GO_VERSION=${VM_GO_VERSION}
case \$(uname -m) in
    aarch64 | arm64) GO_ARCH=arm64 ;;
    x86_64 | amd64) GO_ARCH=amd64 ;;
    *) echo "unsupported architecture: \$(uname -m)" >&2; exit 1 ;;
esac
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go\${GO_VERSION} "; then
    echo "provisioning: go \${GO_VERSION}"
    curl -fsSL "https://go.dev/dl/go\${GO_VERSION}.linux-\${GO_ARCH}.tar.gz" -o /tmp/go.tgz
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tgz
    rm -f /tmp/go.tgz
    ln -sf /usr/local/go/bin/go /usr/bin/go
fi
restorecon -R /usr/local >/dev/null 2>&1 || true
echo "provisioning: sanity"
python3 -c "import sys; assert sys.version_info >= (3, 11), sys.version"
getenforce
nft list tables >/dev/null 2>&1 || true
echo "provisioning: done"
PROVISION
}

vm_ensure_base() {
    if [[ -f $VM_PREPARED ]]; then
        vm_log "prepared base: $VM_PREPARED"
        return 0
    fi
    vm_download_base
    local dir=$vm_runs/base
    local disk=$dir/disk.qcow2
    rm -rf -- "$dir"
    mkdir -p "$dir"
    qemu-img create -q -f qcow2 -b "$VM_IMAGE" -F qcow2 "$disk" \
        || vm_die "could not create the base overlay"
    qemu-img resize -q "$disk" "$VM_DISK_SIZE" || vm_die "could not resize the base image"
    vm_write_seed "$dir"
    vm_log "building the prepared base (first run only: boot, dnf, Go toolchain)"
    vm_boot base "$disk" "$dir/seed.iso"
    vm_wait_ssh base "prepared base"
    vm_ssh base cloud-init status --wait >/dev/null 2>&1 || true
    vm_provision_base base
    vm_poweroff base
    local tmp=$VM_PREPARED.tmp
    rm -f -- "$tmp"
    qemu-img convert -O qcow2 "$disk" "$tmp" || vm_die "could not convert the prepared base"
    mv "$tmp" "$VM_PREPARED"
    rm -rf -- "$dir"
    vm_log "prepared base ready: $VM_PREPARED"
}

vm_console_log() {
    printf '%s/console.log\n' "${VM_DIR[$1]}"
}
