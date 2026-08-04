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

## First-boot reconstitution (milestone 3)

Step 5b of `build.sh` bakes a handler + two systemd units into the image
(after the excluded dirs are recreated, **before** `setfiles`, so the offline
relabel labels them in-pass; enabled via hand-created
`multi-user.target.wants` symlinks — no chroot):

- `/usr/local/sbin/firstboot-reconstitute` (from `firstboot-reconstitute.sh`)
  — the handler, `$1 = offline|online`. Everything machine-specific is
  **discovered from the restored tree at runtime** (PGDATA from the
  postgresql unit, maildir/tag-dumps/compose-file by scanning `/home`, NFS
  mountpoints from `/etc/fstab`, DB user/name from the Immich `.env`).
- `firstboot-reconstitute-offline.service` — oneshot,
  `ConditionPathExists=!/var/lib/firstboot.done`,
  `Before=postgresql.service docker.service`, no network deps. Host PG:
  major-version guard (dump vs installed), `postgresql-setup --initdb`,
  cluster started with `pg_ctl` directly (a `systemctl start` here would
  deadlock against the `Before=`), pg_dumpall load, row-count assert on the
  mail `messages` table, cluster stopped again for postgresql.service.
  notmuch: extract the M6 fast-restore artifact if present, else full
  `notmuch new` (the long pole — `TimeoutStartSec=0`), then `notmuch
  restore` from the newest tag dump.
- `firstboot-reconstitute-online.service` — oneshot,
  `ConditionPathExists=!/var/lib/firstboot-online.done`,
  `After=network-online.target docker.service firstboot-reconstitute-offline.service`.
  NFS + fscache (mkfs a blank second virtio disk as the cache disk if
  present), `docker compose pull && up -d`, `pg_dumpall` restore into the
  fresh `immich_postgres` container (skipped if `asset` already has rows),
  then the two live-only tooling gaps: `uv sync --frozen` for the gitignored
  diary-scripts `.venv` and `playwright install chromium-headless-shell`.

**Failure semantics:** sentinels are written **only on success**, and each
step is individually idempotent (skip-if-present), so a failed phase retries
on the next boot and a half-completed phase self-heals. On an isolated
verify boot the online unit is **expected to fail** (NFS unreachable) —
exclude it from any "no failed units" assertion; it completes on the real
production boot instead.

### Restore-gap coverage decision (2026-08-04)

The 2026-06-22 restore-gap log was checked against the qcow path. Most gaps
don't exist here because whole-`/` captures them: the kindle-dashboard
signing key, goimapnotify binary + config (`~/go` is not excluded),
firewalld zones, and the semanage port config all ride along in the image.
The two remaining live-only gaps — the gitignored `.venv` and
`~/.cache/ms-playwright` (excluded) — are folded into the online phase
(above). Note: the host PG cluster's `postgresql.conf`/`pg_hba.conf` live in
the excluded data dir, so the reconstituted cluster runs **stock config** —
re-apply any tuning manually if the box had any.

Two first-boot wrinkles found by the isolated verify (both handled in the
handler): `/var/lib/restic-backup` is mode 0700 root, so the dumps are fed to
psql via inherited stdin; and the maildir's `post-new` notmuch hook calls
into the not-yet-built `.venv`, so the hooks dir is parked for the
first-boot `notmuch new` and restored right after.

### Isolated-boot assertion matrix (verified 2026-08-04, snapshot 1cd677f4)

- reached `multi-user.target` (degraded only by the expected failures)
- `/var/lib/firstboot.done` present; `/var/lib/firstboot-online.done` absent
- `postgresql.service` active; `mailvec.messages` = 68,977 rows
- `notmuch count '*'` = 206,755; tags restored from the newest dump
- offline unit: ran once, self-disabled; `inactive (dead)` on the next boot
- online unit: failed **only** at the NFS mount (no route on the isolated
  net) — expected; no sentinel, retries next boot
- unit files labeled `systemd_unit_file_t`, SELinux `Enforcing`
- also expected-failed on an isolated boot: `caddy.service` (its Caddyfile
  binds the tailnet IP, which doesn't exist without tailscale)

### RTO expectation

Image boots to login in <2 min; the offline phase then runs before
`multi-user.target` completes — host PG + notmuch ready within ~15–45 min of
first boot (`notmuch new` against a cold ~800k-file maildir is the long
pole; the M6 Xapian artifact cuts it to ~1 min). Immich stack ready within
~5 min of the first boot with real network (docker pull + in-container
restore + vchord index build dominate).

**Never** boot a produced image on the live LAN alongside the real machine —
it's an identity-faithful clone (SSH host keys, tailscale node, …). Isolated
network only.
