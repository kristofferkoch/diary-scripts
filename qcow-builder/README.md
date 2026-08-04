# qcow-builder

Turn a restic whole-`/` snapshot into a **bootable qcow2** whose filesystem
UUIDs, GPT and btrfs subvolumes match the original machine — so fstab and GRUB
work unchanged and the image boots as a faithful replacement. The restore
side (first-boot reconstitution of excluded live state) is a later milestone
and deliberately NOT in this image.

## How it works

`build.sh` (inside the container as `qcow-build`) does:

1. `restic restore <snapshot>` into a staging dir (cached; re-runs skip it).
2. Parses the **disk-layout manifest** the backup wrote to
   `/var/lib/restic-backup/disk-layout.txt` (`sfdisk -d`, `blkid`, `btrfs
   subvolume list /`) plus the restored `/etc/fstab`.
3. Creates a qcow2 (virtual size = original disk size; sparse), attaches it
   via **qemu-nbd** (fallback: loop device + raw image, converted to qcow2 at
   the end), and replays the captured GPT verbatim.
4. `mkfs` each partition with the **original UUID** (`mkfs.vfat -i`,
   `mkfs.xfs -m uuid=`, `mkfs.btrfs -U`) and recreates the btrfs subvolumes.
5. `rsync -aHAX` the tree in; recreates the backup-excluded dirs as empty
   mountpoints (from a built-in pseudo-fs list + the restored
   `/etc/restic/excludes` + external fstab mountpoints); then **bakes SELinux
   labels in offline** with `setfiles` against the tree's own policy (the
   live initramfs has no selinux dracut module, so a `/.autorelabel`
   sentinel would never fire and an enforcing boot would freeze PID 1;
   the sentinel is still touched as a no-op safety net).
6. Bootloader: ESP content comes from the snapshot; ensures the removable
   `EFI/BOOT/BOOTAA64.EFI` path exists. Deliberately does **not** run
   `grub2-mkconfig` in a chroot — on Fedora (BLS) that rewrites the loader
   entries with the build container's root device and the image never boots;
   the restored entries already carry the matching `root=UUID=`.

## Build & run

```sh
docker build -t qcow-builder .

# scratch needs ~2x the restored tree (staging + image)
docker run --rm --privileged \
  -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
  -v /big/host/dir:/scratch \
  qcow-builder \
    --output /scratch/machine.qcow2 \
    [--snapshot latest] [--staging /scratch/staging] \
    [--backend auto|nbd|loop] [--zstd-level 15] \
    [--force-restore] [--skip-restore]
```

`--zstd-level N` mounts the btrfs root with `compress=zstd:N` during the
populate step (default 15; `0` = keep the fstab's own level). Only build-time
writes are denser — btrfs reads are level-transparent and the image's fstab
keeps its original `compress=` setting.

`--privileged` is required for nbd/loop + mkfs + mount. Iterating on the
assembly: the staging restore is done once and reused while the marker file
`staging/.qcow-builder-snapshot` matches `--snapshot`.

Secrets stay in env; nothing machine-specific is baked into the image or the
script — repo URL, snapshot, paths are all arguments.

## Assembly backend (probe result)

Probed 2026-08-04 on the build host (macOS Apple Silicon, Docker Desktop,
linuxkit kernel 6.12). Findings:

- **nbd is NOT available**: the linuxkit kernel ships no `nbd` module
  (`modprobe nbd` fails) and the `/dev/nbd0` node Docker pre-creates in
  privileged containers is a decoy — `sfdisk` on it fails with EINVAL.
- **loop works**, with one quirk: container `/dev` is a static tmpfs, so
  after writing the GPT the kernel learns the partitions (visible in sysfs)
  but no `/dev/loopNpM` nodes appear. `build.sh` runs `partx -u` and mknods
  the partition nodes from sysfs major:minor. Verified: mkfs.vfat + mount OK.

**Backend = loop + raw image**, converted to qcow2 at the end. `build.sh`
keeps the nbd path (`--backend nbd` / `auto` tries it first, now guarded by a
sysfs check) for hosts whose kernel does have the module.

## Definition of done (milestone 2)

A qcow2 that boots to a login prompt when pointed at the host hypervisor
(manual boot, isolated network). Live DB / docker / notmuch state is absent
by design — the first-boot reconstitute unit (milestone 3) rebuilds those.

**Never** boot a produced image on the live LAN alongside the real machine —
it's an identity-faithful clone (SSH host keys, tailscale node, …). Isolated
network only.
