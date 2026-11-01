# Legacy collector runtime state

Spond, Vigilo, and Skolemelding are **manual/on-demand collectors only**. Their
old periodic timers are removed and disabled by `sync/install`; manual
`diary-collect@target.service` runs and broker verbs remain available. Mail
collection and mail event export keep their schedules.

## Fixed roots

- `/var/lib/diary/legacy-state` — legacy cursors, daily JSONL, and attachments,
  `kek:diary` `2775` directories and `0664` files for existing triage readers.
  These files are never committed and never belong in tracked `memory/`.
- `/var/lib/diary/collector-state` — trusted selection state, owner-only `0700`.
- `/var/lib/diary/events` — immutable trusted event spool.

All syncs, show tools, embedders, the Kindle Spond reader, and `sjekk-collect`
use the legacy root consistently. `DIARY_LEGACY_STATE_DIR` is a test seam only;
trusted collector state is independently fixed by broker/manual-service argv.

## Idempotent migration

`sync/install` invokes `scripts.legacy_state_install` from the approved scripts
checkout. It validates regular files with no-follow opens, creates the runtime
hierarchy, and publishes new files without replacement. Identical files are
ignored, newer destination data is preserved, and an older conflicting
destination raises rather than risking event loss. Source files are not deleted
by the helper, and repeat runs preserve runtime data.

Because the diary change removes old tracked generated files, run the approved
migration before synchronizing that commit, while those files still exist:

```bash
sudo-ask 'cd / && PYTHONPATH=/home/kek/diary-scripts-approved /usr/bin/python3 -P -m scripts.legacy_state_install'
```

Then run the approved installers normally:

```bash
sudo-ask /home/kek/diary-approved/sync/install
sudo-ask --as kek /home/kek/diary-approved/systemd/install-user
```

The migration and installers are safe to repeat. They do not start or mask a
manual collector service or enable any Spond/Vigilo/Skolemelding timer.
