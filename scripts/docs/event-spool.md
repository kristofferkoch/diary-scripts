# Trusted collector event spool

PIPE-01 adds deterministic events for the future fixed-purpose pipeline. It does
not run model triage or send notifications.

## Trust and data flow

Spond, Vigilo, and Skolemelding use **manual/on-demand trusted collection only**;
there are no periodic probes or collector timers. Mail keeps its independent
schedule. **Every broker, manual-service, and local-fallback invocation passes
both** `--event-spool /var/lib/diary/events` and
`--state-dir /var/lib/diary/collector-state`. A broker run therefore cannot
advance a shared cursor past records that a later manual run still needs to
export.

A per-collector `flock` at `/run/user/1000/diary-collect/` serializes broker,
manual-service, and fallback collection. The system services bind only that
directory from the user runtime and pin `XDG_RUNTIME_DIR=/run/user/1000`; a plain
kek fallback creates the same owner-writable directory if the broker is down.

Spond, Vigilo, and Skolemelding write fresh **in-memory** records to the spool
before legacy JSONL and trusted collector state. The exporter deliberately does
not ingest those legacy files: their group-writable runtime directories are
agent-accessible and are not a trusted event source.

All seen markers, poll timestamps, attachment retry state, and Vigilo deletion
tombstones are read only from `/var/lib/diary/collector-state`, installed as
`kek:kek` mode `0700` with `0600` state files. Legacy cursors and JSONL in
`/var/lib/diary/legacy-state` are best-effort output mirrors for existing
readers; they are never trusted inputs and never enter tracked `memory/`.
Mirror failures do not move or invalidate trusted state. Future poll timestamps
are treated as invalid rather than as a reason to skip collection.

**First trusted run / migration policy:** Spond backfills when its trusted state
file is absent. Vigilo and Skolemelding backfill while their trusted state lacks
`last_successful_run`, including when an initial auth expiry already created the
file. They fetch with an empty trusted cursor and backfill the trusted spool, but
do not append that backfill to legacy JSONL. A successful export then saves and
mirrors the completion cursor. Existing agent-writable legacy seen markers are
never copied into trusted state and cannot suppress the spool backfill; existing
legacy output is not replayed or duplicated. Subsequent runs use the normal
fresh-record JSONL workflow.

The existing kek `mail-sync.timer` remains the independent mail collection
schedule. A separate `diary-mail-event-export.timer` and oneshot service export
mail, so an export failure cannot restart or repeat mail collection.

## Immutable events and crash handling

The spool is flat and contains one canonical JSON object per
`evt_<sha256>.json`. Each strict envelope has exactly `schema_version`,
`event_id`, `source`, `kind`, `native_id`, `revision`, and `payload`. Collection
timestamps are not part of event identity. Revisions are SHA-256 hashes of
canonical payload JSON, so unchanged records keep the same ID and actual updates
receive a new immutable event.

Mail uses the logical notmuch message ID as native identity plus a
content-derived revision. Initial discovery processes only canonical physical
`INBOX` files, excluding Proton `All Mail` copies. Exact copies deduplicate;
different bodies sharing an RFC/notmuch Message-ID remain distinct events. Raw
content is read directly from notmuch-provided filenames with bounded
`os.open`/`os.read`, regular-file/no-follow checks, and replacement/write race
detection—never through a `path:` query.

A versioned kek-only `mail-event-journal.json` records pending logical IDs,
completed event IDs, and quarantined message/file variants:

- Pending IDs are resolved through notmuch on every retry, so normal
  INBOX→Archive moves and tag reconciliation do not strand an old pathname.
- Completion is journaled only after the spool's final link and directory
  `fsync`; a crash before that journal commit is retried as a duplicate.
- Timeout, `OSError`, and transient database/read failures stay pending for the
  next schedule. Only deterministic malformed/oversized content is quarantined.
- Quarantined variants do not block other messages or get reread on every
  schedule. Entries remain local review state and require explicit owner action
  to clear.

`EventSpool.write()` returns `written` or `duplicate`. A duplicate with different
content raises `EventConflictError`; neither path overwrites the existing file.
Malformed events raise `EventValidationError`. Writes use a same-directory
temporary file, `fchmod` then file `fsync`, and an atomic hard-link-without-
replacement. The temporary name is unlinked **before** the directory `fsync` and
acknowledgement, so acknowledged entries are single-link readable files. Readers
reject links, multiple hard links, oversized files, invalid UTF-8/JSON, duplicate
JSON keys, unknown fields, non-finite numbers, excessive nesting, and IDs that do
not match their immutable identity.

## Ownership and configuration

`sync/install` creates the system group `diary-pipeline` and installs
`/var/lib/diary/events` as `kek:diary-pipeline` mode `2750`. Event files are mode
`0640`; the setgid directory gives them the pipeline group without granting that
group write access. The future pipeline runs with this group and read-only spool
access. The general `agent`/`diary` identities are not members and have no spool
or collector-state access.

The installer also changes `/etc/diary/trusted.toml` to `root:kek` mode `0640`.
Collector credentials stay in the existing fixed GPG/password-store and token
paths. Neither credentials nor the trusted collector TOML is in the spool or
pipeline-readable. Units execute only `/home/kek/diary-scripts-approved`; never
point them at a worktree or integration checkout.

## Owner-controlled deployment

After exact-OID approval of both repositories and the diary submodule pointer:

```bash
sudo-ask /home/kek/diary-approved/sync/install
sudo-ask --as kek /home/kek/diary-approved/systemd/install-user
```

The first command disables the three obsolete collector timers, migrates legacy
state to `/var/lib/diary/legacy-state`, and prepares the spool, trusted state,
and trusted-config modes without starting or masking a manual collector. The
second installs/enables the independent mail export timer and removes the
obsolete `mail-sync` `ExecStartPost` drop-in. Installation does not immediately
run a collector. Run `scripts.legacy_state_install` once from the approved
scripts checkout before synchronizing the diary commit that deletes the old
tracked files.

Static checks on the approved files:

```bash
systemd-analyze verify \
  /home/kek/diary-approved/sync/diary-sync-runtime.service \
  /home/kek/diary-approved/sync/diary-collect@.service
systemd-analyze verify --user \
  /home/kek/diary-approved/systemd/units/diary-mail-event-export.service \
  /home/kek/diary-approved/systemd/units/diary-mail-event-export.timer
```

Controlled verification must show all four collectors succeeding during a
controlled general-agent lock, broker-then-manual delivery without loss, identical
IDs after service/process restart, archived pending recovery, per-message
quarantine, duplicate and malformed fixtures following the rules above, and
`agent` plus an unprivileged future-pipeline identity unable to read credentials,
collector state, or trusted collector configuration. The pipeline identity must
be able to read but not write event files; the general agent must not be able to
write them. PIPE-02 will create the actual pipeline service identity and its
permanent containment.
