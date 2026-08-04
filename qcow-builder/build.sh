#!/usr/bin/env bash
# qcow-build — assemble a bootable qcow2 from a restic whole-/ snapshot.
#
# Pipeline (QCOW-GENERATOR.md steps 1-6):
#   1. restic restore -> staging (skipped if staging marker matches)
#   2. parse disk-layout manifest + fstab from the restored tree
#   3. create qcow2, attach (qemu-nbd or loop+raw), replay the GPT
#   4. mkfs with the ORIGINAL filesystem UUIDs (from the manifest)
#   5. rsync the tree in, recreate excluded dirs empty, /.autorelabel
#   5b. bake in the firstboot-reconstitute handler + units (milestone 3)
#   6. bootloader: ESP copy + EFI/BOOT/BOOTAA64.EFI removable path
#
# No hostnames, snapshot ids, paths or credentials are baked in — everything
# comes from flags or the standard restic env vars.
#
# Usage:
#   qcow-build --output /scratch/box.qcow2 [--snapshot latest]
#              [--staging /scratch/staging] [--scratch /scratch]
#              [--backend auto|nbd|loop] [--force-restore] [--skip-restore]
#
# Env (for the restore step): RESTIC_REPOSITORY, RESTIC_PASSWORD or
# RESTIC_PASSWORD_FILE — the usual restic variables.

set -euo pipefail

SNAPSHOT=latest
OUTPUT=
STAGING=/scratch/staging
SCRATCH=/scratch
BACKEND=auto
ZSTD_LEVEL=15   # build-time btrfs compress level (image fstab stays zstd:5)
FORCE_RESTORE=0
SKIP_RESTORE=0

log()  { printf '[qcow-build] %s\n' "$*"; }
warn() { printf '[qcow-build] WARN: %s\n' "$*" >&2; }
die()  { printf '[qcow-build] ERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot)      SNAPSHOT=$2; shift 2;;
    --output)        OUTPUT=$2; shift 2;;
    --staging)       STAGING=$2; shift 2;;
    --scratch)       SCRATCH=$2; shift 2;;
    --backend)       BACKEND=$2; shift 2;;
    --zstd-level)    ZSTD_LEVEL=$2; shift 2;;
    --force-restore) FORCE_RESTORE=1; shift;;
    --skip-restore)  SKIP_RESTORE=1; shift;;
    -h|--help)       usage 0;;
    *) die "unknown arg: $1";;
  esac
done

[ -n "$OUTPUT" ] || die "--output is required"
[ -d "$SCRATCH" ] || die "scratch dir $SCRATCH does not exist (bind-mount a big host dir there)"
mkdir -p "$STAGING"

for t in restic qemu-img sfdisk mkfs.vfat mkfs.xfs mkfs.btrfs rsync awk; do
  command -v "$t" >/dev/null || die "missing tool: $t"
done

# --------------------------------------------------------------------------
# Step 1 — restore the snapshot into staging (idempotent via marker file)
# --------------------------------------------------------------------------
MARKER="$STAGING/.qcow-builder-snapshot"
if [ "$SKIP_RESTORE" = 1 ]; then
  [ -f "$MARKER" ] || die "--skip-restore but $MARKER is missing"
  log "step 1: skipping restore (marker: $(cat "$MARKER"))"
elif [ "$FORCE_RESTORE" = 0 ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$SNAPSHOT" ] \
     && [ -d "$STAGING/etc" ]; then
  log "step 1: staging already holds snapshot '$SNAPSHOT' — reusing"
else
  [ -n "${RESTIC_REPOSITORY:-}" ] || die "RESTIC_REPOSITORY not set"
  { [ -n "${RESTIC_PASSWORD:-}" ] || [ -n "${RESTIC_PASSWORD_FILE:-}" ]; } \
    || die "RESTIC_PASSWORD / RESTIC_PASSWORD_FILE not set"
  log "step 1: restic restore $SNAPSHOT -> $STAGING"
  # /var/lib/containerd is re-pullable container-layer storage (same class as
  # /var/lib/docker, which the backup excludes) — and its overlayfs whiteout
  # device nodes can't be restored onto non-Linux staging filesystems anyway.
  restic restore "$SNAPSHOT" --target "$STAGING" --exclude /var/lib/containerd
  # resolve the snapshot arg to a concrete id for the marker
  SNAP_ARGS=("$SNAPSHOT"); [ "$SNAPSHOT" = latest ] && SNAP_ARGS=(--latest 1)
  RESTIC_ID=$(restic snapshots --json "${SNAP_ARGS[@]}" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["id"])' 2>/dev/null \
    || echo "$SNAPSHOT")
  echo "$RESTIC_ID" > "$MARKER"
fi

# --------------------------------------------------------------------------
# Step 2 — parse the disk-layout manifest + fstab from the restored tree
# --------------------------------------------------------------------------
MANIFEST="$STAGING/var/lib/restic-backup/disk-layout.txt"
[ -f "$MANIFEST" ] || die "no disk-layout manifest at $MANIFEST (snapshot too old?)"
FSTAB="$STAGING/etc/fstab"
[ -f "$FSTAB" ] || die "no fstab in restored tree"

ROOT_DEV=$(awk -F= '/^root_dev=/{print $2}' "$MANIFEST")
[ -n "$ROOT_DEV" ] || die "manifest has no root_dev="
# root_dev is the root *partition* (/dev/vda3, /dev/nvme0n1p3) — strip the
# partition suffix to get the whole disk (vda, nvme0n1)
DISK_NAME=$(printf '%s' "${ROOT_DEV#/dev/}" | sed -E 's/[0-9]+$//; s/p$//')
[ -n "$DISK_NAME" ] || die "cannot derive disk name from $ROOT_DEV"
log "step 2: system disk is /dev/$DISK_NAME (root on $ROOT_DEV)"

WORK="$SCRATCH/.qcow-build-work"
mkdir -p "$WORK"
LAYOUT="$WORK/layout.sfdisk"
BLKID="$WORK/blkid.txt"
SUBVOLS="$WORK/subvols.txt"

# extract the manifest sections we need
awk -v d="### sfdisk -d /dev/$DISK_NAME" '
  $0==d {on=1; next} /^### / && on {exit} on' "$MANIFEST" > "$LAYOUT"
[ -s "$LAYOUT" ] || die "no sfdisk section for /dev/$DISK_NAME in manifest"
awk '/^### blkid/{on=1; next} /^### / && on{exit} on' "$MANIFEST" > "$BLKID"
awk '/^### btrfs subvolume list/{on=1; next} /^### / && on{exit} on' "$MANIFEST" > "$SUBVOLS"

SECTOR=$(awk -F': *' '/^sector-size:/{print $2}' "$LAYOUT"); SECTOR=${SECTOR:-512}
LAST_LBA=$(awk -F': *' '/^last-lba:/{print $2}' "$LAYOUT")
if [ -n "$LAST_LBA" ]; then
  SIZE_BYTES=$(( (LAST_LBA + 1) * SECTOR ))
else
  # fall back to the end of the last partition + 1 MiB of GPT slack
  END=$(awk -F'[=,]' '/start=/{for(i=1;i<=NF;i++){if($i~/start/)s=$(i+1); if($i~/size/)z=$(i+1)}; e=s+z; if(e>m)m=e} END{print m}' "$LAYOUT")
  [ -n "$END" ] || die "cannot determine disk size from sfdisk dump"
  SIZE_BYTES=$(( (END + 2048) * SECTOR ))
fi
SIZE_GIB=$(( (SIZE_BYTES + 1073741823) / 1073741824 + 1 ))
log "step 2: virtual disk size ${SIZE_GIB} GiB (sector=$SECTOR)"

# per-partition fs info keyed by partition number: UUID / TYPE
declare -A P_UUID P_TYPE
while read -r dev rest; do
  dev=${dev%:}; base=${dev#/dev/}
  num=${base#"$DISK_NAME"}          # strip the disk name prefix
  [ "$num" != "$base" ] || continue # not a partition of the system disk
  num=${num#p}                      # nvme/mmc-style separator
  case "$num" in ''|*[!0-9]*) continue;; esac
  u=$(printf ' %s' "$rest" | sed -n 's/.* UUID="\([^"]*\)".*/\1/p')
  t=$(printf ' %s' "$rest" | sed -n 's/.* TYPE="\([^"]*\)".*/\1/p')
  [ -n "$u" ] && P_UUID[$num]=$u
  [ -n "$t" ] && P_TYPE[$num]=$t
done < "$BLKID"
[ "${#P_TYPE[@]}" -gt 0 ] || die "no partitions of /dev/$DISK_NAME found in blkid section"
log "step 2: partitions: $(for n in $(printf '%s\n' "${!P_TYPE[@]}" | sort -n); do printf '%s=%s ' "$n" "${P_TYPE[$n]}"; done)"

# root mount options: subvol from fstab; compression bumped for the build
# (writes during rsync land denser; reads are level-transparent and the
# image's own fstab keeps its original compress= setting)
ROOT_OPTS=$(awk '$2=="/" && $1 !~ /^#/{print $4; exit}' "$FSTAB")
[ -n "$ROOT_OPTS" ] || die "no / entry in fstab"
ROOT_SUBVOL=$(printf '%s' "$ROOT_OPTS" | tr ',' '\n' | sed -n 's/^subvol=//p' | head -1)
[ -n "$ROOT_SUBVOL" ] || ROOT_SUBVOL=root
if [ "$ZSTD_LEVEL" -gt 0 ]; then
  MOUNT_OPTS="subvol=$ROOT_SUBVOL,compress=zstd:$ZSTD_LEVEL"
else
  MOUNT_OPTS=$(printf '%s' "$ROOT_OPTS" | tr ',' '\n' \
    | grep -E '^(subvol=|compress)' | paste -sd, -)
  [ -n "$MOUNT_OPTS" ] || MOUNT_OPTS="subvol=$ROOT_SUBVOL"
fi
log "step 2: root mount opts: $MOUNT_OPTS"

# --------------------------------------------------------------------------
# Step 3 — create the image and attach it
# --------------------------------------------------------------------------
MNT="$WORK/mnt"
mkdir -p "$MNT"
DEV=
ATTACH_KIND=
WORK_IMAGE=

cleanup() {
  set +e
  mountpoint -q "$MNT" && umount -R "$MNT"
  if [ "$ATTACH_KIND" = nbd ] && [ -n "$DEV" ]; then qemu-nbd --disconnect "$DEV" >/dev/null 2>&1; fi
  if [ "$ATTACH_KIND" = loop ] && [ -n "$DEV" ]; then losetup -d "$DEV" >/dev/null 2>&1; fi
}
trap cleanup EXIT

attach_nbd() {
  modprobe nbd max_part=16 2>/dev/null || true
  # /dev/nbd0 can be a decoy node (Docker pre-creates nodes for devices whose
  # driver isn't actually usable) — trust sysfs + a real size query, not /dev
  [ -e /sys/class/block/nbd0/dev ] || return 1
  [ -b /dev/nbd0 ] || mknod /dev/nbd0 b "$(cut -d: -f1 /sys/class/block/nbd0/dev)" "$(cut -d: -f2 /sys/class/block/nbd0/dev)"
  qemu-nbd --connect=/dev/nbd0 --format=qcow2 "$WORK_IMAGE" || return 1
  local sz
  sz=$(blockdev --getsize64 /dev/nbd0 2>/dev/null || echo 0)
  if [ "${sz:-0}" -le 0 ]; then
    # connect "succeeds" but the device is dead (seen: linuxkit built-in nbd
    # reporting size 0) — treat as unavailable
    qemu-nbd --disconnect /dev/nbd0 >/dev/null 2>&1 || true
    return 1
  fi
  DEV=/dev/nbd0; ATTACH_KIND=nbd
}

attach_loop() {
  # loop devices can't host qcow2: build raw sparse, convert at the end
  [ -z "$WORK_IMAGE" ] || rm -f "$WORK_IMAGE"   # drop any orphan qcow2 from auto mode
  WORK_IMAGE="$WORK/image.raw"
  qemu-img create -f raw "$WORK_IMAGE" "${SIZE_GIB}G" >/dev/null
  DEV=$(losetup -fP --show "$WORK_IMAGE") || return 1
  ATTACH_KIND=loop
}

pick_backend() {
  case "$BACKEND" in
    nbd)  attach_nbd || die "nbd requested but unavailable";;
    loop) attach_loop || die "loop requested but unavailable";;
    auto)
      if attach_nbd 2>/dev/null; then :;
      elif attach_loop; then warn "nbd unavailable — using loop+raw (will convert to qcow2 at the end)";
      else die "neither nbd nor loop available (needs --privileged)"; fi;;
    *) die "bad --backend $BACKEND";;
  esac
}

if [ "$BACKEND" != loop ]; then
  WORK_IMAGE="$WORK/image.qcow2"
  rm -f "$WORK_IMAGE"
  qemu-img create -f qcow2 "$WORK_IMAGE" "${SIZE_GIB}G" >/dev/null
fi
pick_backend
log "step 3: attached $WORK_IMAGE as $DEV ($ATTACH_KIND)"

# replay the captured GPT verbatim (type + unique GUIDs preserved)
sfdisk "$DEV" < "$LAYOUT" >/dev/null
partx -u "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV" 2>/dev/null || true

# container /dev is a static tmpfs: the kernel learns the partitions (sysfs)
# but no nodes appear — mknod them from sysfs major:minor
base=${DEV##*/}
for n in "${!P_TYPE[@]}"; do
  for cand in "${base}p${n}" "${base}${n}"; do
    sys=/sys/class/block/$cand/dev
    [ -f "$sys" ] || continue
    [ -b "/dev/$cand" ] || mknod "/dev/$cand" b "$(cut -d: -f1 "$sys")" "$(cut -d: -f2 "$sys")"
    break
  done
done

part_node() {  # part_node 2 -> /dev/nbd0p2 or /dev/loop0p2
  if [ -b "${DEV}p$1" ]; then printf '%s' "${DEV}p$1"; else printf '%s' "$DEV$1"; fi
}

# wait for any stragglers
for n in "${!P_TYPE[@]}"; do
  for _ in $(seq 1 20); do [ -b "$(part_node "$n")" ] && break; sleep 0.5; done
  [ -b "$(part_node "$n")" ] || die "partition node for part $n never appeared on $DEV"
done

# --------------------------------------------------------------------------
# Step 4 — mkfs with the original UUIDs; recreate btrfs subvolumes
# --------------------------------------------------------------------------
for n in $(printf '%s\n' "${!P_TYPE[@]}" | sort -n); do
  pn=$(part_node "$n")
  [ -b "$pn" ] || die "partition node $pn missing after GPT replay"
  case "${P_TYPE[$n]}" in
    vfat)
      mkfs.vfat -F 32 -i "${P_UUID[$n]//-/}" "$pn" >/dev/null;;
    xfs)
      mkfs.xfs -f -m "uuid=${P_UUID[$n]}" "$pn" >/dev/null;;
    btrfs)
      mkfs.btrfs -f -U "${P_UUID[$n]}" "$pn" >/dev/null;;
    *) die "unhandled fs type ${P_TYPE[$n]} on partition $n";;
  esac
  log "step 4: $pn -> ${P_TYPE[$n]} uuid=${P_UUID[$n]}"
done

# btrfs subvolumes, in manifest (ID) order: "ID 256 gen .. top level .. path root"
ROOT_PN=
for n in "${!P_TYPE[@]}"; do [ "${P_TYPE[$n]}" = btrfs ] && ROOT_PN=$(part_node "$n"); done
if [ -n "$ROOT_PN" ] && [ -s "$SUBVOLS" ]; then
  TOP="$WORK/btrfs-top"; mkdir -p "$TOP"
  mount -o subvolid=0 "$ROOT_PN" "$TOP"
  awk '{print $NF}' "$SUBVOLS" | while read -r sv; do
    [ -n "$sv" ] || continue
    [ "$sv" = "root" ] || mkdir -p "$TOP/$(dirname "$sv")"
    [ -d "$TOP/$sv" ] || btrfs subvolume create "$TOP/$sv" >/dev/null
    log "step 4: btrfs subvolume $sv"
  done
  umount "$TOP"
fi

# --------------------------------------------------------------------------
# Step 5 — populate: rsync the tree, recreate excluded dirs, .autorelabel
# --------------------------------------------------------------------------
mount -o "$MOUNT_OPTS" "$ROOT_PN" "$MNT" 2>/dev/null || {
  [ -n "$ROOT_PN" ] || die "no btrfs root partition found in manifest"
  die "failed to mount root ($ROOT_PN, opts: $MOUNT_OPTS)"
}
# mount the remaining partitions shallowest-first so nested mountpoints
# (/boot/efi under /boot) land on the right filesystem
{
  for n in $(printf '%s\n' "${!P_TYPE[@]}" | sort -n); do
    [ "${P_TYPE[$n]}" = btrfs ] && continue
    # mountpoint for this partition from fstab (by UUID)
    mp=$(awk -v u="${P_UUID[$n]}" '$1=="UUID="u {print $2; exit}' "$FSTAB")
    [ -n "$mp" ] || { warn "no fstab mountpoint for UUID ${P_UUID[$n]} — skipping mount"; continue; }
    printf '%s\t%s\n' "$mp" "$n"
  done
} | sort | while IFS=$'\t' read -r mp n; do
  mkdir -p "$MNT$mp"
  mount "$(part_node "$n")" "$MNT$mp"
  log "step 5: mounted partition $n at $mp"
done

log "step 5: rsync staging -> image (this is the long part)"
# the ESP is vfat: no perms/xattrs — copy it separately, exclude from the main pass
ESP_MP=$(awk '$3=="vfat" && $1 !~ /^#/{print $2; exit}' "$FSTAB")
RSYNC_EXCLUDES=(--exclude='/.qcow-builder-snapshot')
[ -n "$ESP_MP" ] && RSYNC_EXCLUDES+=(--exclude="$ESP_MP")
rsync -aHAX --numeric-ids "${RSYNC_EXCLUDES[@]}" "$STAGING/" "$MNT/"
if [ -n "$ESP_MP" ] && [ -d "$STAGING$ESP_MP" ]; then
  rsync -rt --no-perms --no-owner --no-group "$STAGING$ESP_MP/" "$MNT$ESP_MP/"
fi

# recreate excluded dirs as empty mountpoints / caches with sane perms.
# sources: built-in pseudo-fs list, the restored /etc/restic/excludes, and
# fstab mountpoints that aren't one of our partitions (NFS / scratch mounts).
EMPTY_DIRS=(/proc /sys /dev /run /tmp /var/tmp /var/cache /mnt /media /lost+found /var/lib/containerd)
EXCLUDES_FILE="$STAGING/etc/restic/excludes"
if [ -f "$EXCLUDES_FILE" ]; then
  while read -r line; do
    case "$line" in
      ''|'#'*) continue;;
      /home/\*/.cache|/root/.cache) continue;;  # handled per-user below
      *\**) continue;;                            # file globs (*.tmp)
      */swapfile|*/swap.img|*/var/swap) continue;; # files, not dirs
      /*) EMPTY_DIRS+=("$line");;
    esac
  done < "$EXCLUDES_FILE"
fi
# fstab mountpoints that aren't backed by our partitions (NFS, LABEL=, …)
while read -r src mp fs opts rest; do
  case "$src" in ''|'#'*) continue;; esac
  case "$fs" in proc|sysfs|tmpfs|devpts|devtmpfs|swap|none|cgroup*|overlay) continue;; esac
  case "$mp" in /|/boot|/boot/efi|swap) continue;; esac
  EMPTY_DIRS+=("$mp")
done < "$FSTAB"

for d in $(printf '%s\n' "${EMPTY_DIRS[@]}" | sort -u); do
  [ -e "$MNT$d" ] || mkdir -p "$MNT$d"
done
chmod 1777 "$MNT/tmp" "$MNT/var/tmp"
# per-user cache dirs, owned by the user
for home in "$MNT"/home/*/; do
  [ -d "$home" ] || continue
  mkdir -p "$home.cache"
  chown --reference="$home" "$home.cache"
  chmod 700 "$home.cache"
done
mkdir -p "$MNT/root/.cache" && chmod 700 "$MNT/root/.cache"
# anything under a /home/<user> tree that we created gets that user's ownership
for home in "$MNT"/home/*/; do
  [ -d "$home" ] || continue
  user=${home%/}; user=${user##*/}
  for d in $(printf '%s\n' "${EMPTY_DIRS[@]}" | sort -u); do
    case "$d" in
      /home/"$user"/*) [ -d "$MNT$d" ] && chown --reference="$home" "$MNT$d";;
    esac
  done
done

# --------------------------------------------------------------------------
# Step 5b — bake in the first-boot reconstitution units (milestone 3)
# --------------------------------------------------------------------------
# The handler + unit files ship inside the builder image (Dockerfile COPY).
# Installed here — after the excluded dirs exist, BEFORE the setfiles pass —
# so the offline relabel labels the script, the units and the enable-symlinks
# in the same run. Enablement is done with hand-created wants symlinks rather
# than chroot `systemctl enable` — equivalent output, zero chroot.
FIRSTBOOT_SRC=/usr/local/share/qcow-builder
[ -f "$FIRSTBOOT_SRC/firstboot-reconstitute.sh" ] \
  || die "firstboot files missing at $FIRSTBOOT_SRC (broken builder image?)"
install -D -m 755 "$FIRSTBOOT_SRC/firstboot-reconstitute.sh" \
  "$MNT/usr/local/sbin/firstboot-reconstitute"
install -D -m 644 "$FIRSTBOOT_SRC/firstboot-reconstitute-offline.service" \
  "$MNT/etc/systemd/system/firstboot-reconstitute-offline.service"
install -D -m 644 "$FIRSTBOOT_SRC/firstboot-reconstitute-online.service" \
  "$MNT/etc/systemd/system/firstboot-reconstitute-online.service"
mkdir -p "$MNT/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/firstboot-reconstitute-offline.service \
  "$MNT/etc/systemd/system/multi-user.target.wants/"
ln -sf /etc/systemd/system/firstboot-reconstitute-online.service \
  "$MNT/etc/systemd/system/multi-user.target.wants/"
log "step 5b: firstboot-reconstitute handler + offline/online units baked in (enabled via wants symlinks)"

touch "$MNT/.autorelabel"
log "step 5: excluded dirs recreated empty; /.autorelabel set"

# Offline SELinux relabel. The live box's initramfs has NO selinux dracut
# module, so the /.autorelabel sentinel above would never be consumed — and
# an enforcing boot against an unlabeled filesystem freezes PID 1 outright
# (verified 2026-08-04). Bake correct labels in here instead; the sentinel
# stays as a no-op safety net for trees whose initramfs does handle it.
ST=$(awk -F= '/^SELINUXTYPE=/{print $2}' "$MNT/etc/selinux/config" 2>/dev/null); ST=${ST:-targeted}
FC="$MNT/etc/selinux/$ST/contexts/files/file_contexts"
if [ -f "$FC" ]; then
  SETFILES_EXCL=(-e "$MNT/proc" -e "$MNT/sys" -e "$MNT/dev" -e "$MNT/run")
  [ -n "$ESP_MP" ] && SETFILES_EXCL+=(-e "$MNT$ESP_MP")   # vfat: no xattrs
  setfiles -F -r "$MNT" "${SETFILES_EXCL[@]}" "$FC" "$MNT" \
    && log "step 5: SELinux labels baked in ($ST policy)" \
    || warn "setfiles reported errors — inspect labels before booting"
else
  warn "no SELinux file_contexts in image — relying on /.autorelabel only"
fi

# --------------------------------------------------------------------------
# Step 6 — bootloader: removable-media path + grub2-mkconfig insurance
# --------------------------------------------------------------------------
if [ -n "$ESP_MP" ] && [ -d "$MNT$ESP_MP/EFI" ]; then
  ESP="$MNT$ESP_MP"
  mkdir -p "$ESP/EFI/BOOT"
  # aarch64 removable path: BOOTAA64.EFI = shim, grub next to it
  for cand in "$ESP"/EFI/*/shimaa64.efi; do
    if [ -f "$cand" ] && [ ! -f "$ESP/EFI/BOOT/BOOTAA64.EFI" ]; then
      cp "$cand" "$ESP/EFI/BOOT/BOOTAA64.EFI"
      gdir=$(dirname "$cand")
      [ -f "$gdir/grubaa64.efi" ] && cp "$gdir/grubaa64.efi" "$ESP/EFI/BOOT/"
      log "step 6: installed removable EFI/BOOT/BOOTAA64.EFI from $gdir"
    fi
  done
  [ -f "$ESP/EFI/BOOT/BOOTAA64.EFI" ] || warn "no shimaa64.efi found — removable boot path missing"
else
  warn "no ESP content — skipping removable-path install"
fi

# NOTE: no chroot grub2-mkconfig here. On Fedora (BLS), grub2-mkconfig
# rewrites /boot/loader/entries with the *build container's* root device
# (root=/dev/loop0p3) and the image then never boots. The restored grub.cfg +
# BLS entries already carry root=UUID=<the very UUIDs we mkfs'd> — leaving
# them untouched IS the correctness story.

sync
umount -R "$MNT"

# --------------------------------------------------------------------------
# finish: detach, (loop: convert raw -> qcow2), move to --output, check
# --------------------------------------------------------------------------
if [ "$ATTACH_KIND" = nbd ]; then
  qemu-nbd --disconnect "$DEV" >/dev/null; DEV=
  mv "$WORK_IMAGE" "$OUTPUT"
else
  losetup -d "$DEV"; DEV=
  log "converting raw -> qcow2"
  qemu-img convert -O qcow2 "$WORK_IMAGE" "$OUTPUT"
  rm -f "$WORK_IMAGE"
fi
ATTACH_KIND=
trap - EXIT

qemu-img check "$OUTPUT"
log "DONE: $OUTPUT ($(du -h "$OUTPUT" | cut -f1) allocated)"
