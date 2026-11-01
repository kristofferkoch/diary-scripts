# Legacy collector runtime state

Spond, Vigilo, and Skolemelding are **manual/on-demand collectors only**. Their
old periodic timers are removed and disabled by `sync/install`; manual
`diary-collect@target.service` runs and broker verbs remain available. Mail
collection and mail event export keep their schedules.

## Fixed roots

- `/var/lib/diary/legacy-state` — legacy cursors, daily JSONL, and attachments.
  The root is `kek:diary` `3775`; source/attachment directories are `2775` and
  files are `0664` for existing triage readers. These files are never committed
  and never belong in tracked `memory/`.
- `/var/lib/diary/collector-state` — trusted selection state, owner-only `0700`.
- `/var/lib/diary/events` — immutable trusted event spool.

All syncs, show tools, embedders, the Kindle Spond reader, and `sjekk-collect`
use the legacy root consistently. `DIARY_LEGACY_STATE_DIR` is a test seam only;
trusted collector state is independently fixed by broker/manual-service argv.

## Migration safety

`scripts.legacy_state_install` first calls `scripts.legacy_state_quiesce`, which:

1. uses `disable --now` on each old collector timer that exists;
2. stops any active manual collector instances and the old diary-sync broker;
3. verifies every old writer is stopped, treating missing units as no-ops and
   genuine systemctl errors as fatal.

Only then does migration begin. All source and destination I/O is relative to
retained dirfds with no-follow opens, so replacing a group-writable parent with
a symlink cannot redirect root writes. New files are published without
clobbering. Identical files are ignored, newer destinations are preserved, and
an older conflicting destination raises rather than risking event loss.

Attachment trees are migrated recursively. Legacy JSONL `path` fields under
both `/home/kek/diary/memory/...` and `/etc/diary/workspace/memory/...` are
converted to `/var/lib/diary/legacy-state/...`; nested content is preserved and
older absolute paths are normalized on repeat runs.

## Exact quiesced deployment sequence

The diary change deletes the old tracked files, so quiesce **before** the first
copy and keep the old broker stopped until the new installer is ready:

1. Approve the `diary-scripts` change, but do not synchronize the diary change
   that deletes the old tracked files yet.
2. Stop every old writer and perform the first migration:
   ```bash
   sudo-ask 'cd / && PYTHONPATH=/home/kek/diary-scripts-approved /usr/bin/python3 -P -m scripts.legacy_state_install'
   ```
   This command itself disables/stops old writers before copying. Do not start
   the broker or manual collectors afterward.
3. Review and synchronize the diary commit that deletes the old tracked files.
   The stopped broker/timers cannot create new source data during this step.
4. Perform a final migration to catch anything written before quiescing:
   ```bash
   sudo-ask 'cd / && PYTHONPATH=/home/kek/diary-scripts-approved /usr/bin/python3 -P -m scripts.legacy_state_install'
   ```
5. Install the new broker/manual-service configuration and runtime modes:
   ```bash
   sudo-ask /home/kek/diary-approved/sync/install
   sudo-ask --as kek /home/kek/diary-approved/systemd/install-user
   ```
   `sync/install` quiesces **before its own migration**, installs the new units,
   and only then restarts the new broker. It does not enable old timers or mask
   manual collector services.

Every migration and installer is safe to repeat. A missing timer is a no-op;
an active writer or genuine systemctl failure aborts the sequence.
