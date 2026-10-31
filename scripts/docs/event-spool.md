# Trusted collector event spool

PIPE-01 adds deterministic events for the future fixed-purpose pipeline. It does
not run model triage or send notifications.

## Trust and data flow

The three system timers `diary-collect-{spond,vigilo,skolemelding}.timer` start
fixed instances of `diary-collect@.service`. They execute the approved sync entry
points directly, not through `diary-sync`; the existing agent-facing broker and
its verbs remain available. A per-collector `flock` shared with the broker keeps
legacy JSONL/state writes serial. The existing kek `mail-sync.timer` remains the
independent mail schedule, and a `mail-sync.service` drop-in exports mail after
each successful sync.

Spond, Vigilo, and Skolemelding pass `--event-spool /var/lib/diary/events`.
Their fresh **in-memory** records are written to the spool before legacy JSONL
and collector state. The exporter deliberately does not ingest those legacy
files: their group-writable directories are agent-accessible and are not a
trusted event source. Mail export reads the owner-only notmuch store.

The spool is flat and contains one canonical JSON object per
`evt_<sha256>.json`. Each strict envelope has exactly `schema_version`,
`event_id`, `source`, `kind`, `native_id`, `revision`, and `payload`. Collection
timestamps are not part of event identity. For Spond/Vigilo/Skolemelding the
revision is the SHA-256 of canonical payload JSON, so unchanged records keep the
same ID and actual updates receive a new immutable event. Mail uses the stable
notmuch message ID and the `notmuch-v1` revision, allowing the exporter to skip
already-spooled messages without rereading every raw body.

`EventSpool.write()` returns `written` or `duplicate`. A duplicate with different
content raises `EventConflictError`; neither path overwrites the existing file.
Malformed events raise `EventValidationError`. Writes use a same-directory
temporary file, `fsync`, and an atomic hard-link-without-replacement, followed by
a directory `fsync`. Readers reject links, multiple hard links, oversized files,
invalid UTF-8/JSON, duplicate JSON keys, unknown fields, non-finite numbers, and
IDs that do not match their immutable identity.

## Ownership and configuration

`sync/install` creates the system group `diary-pipeline` and installs
`/var/lib/diary/events` as `kek:diary-pipeline` mode `2750`. Event files are mode
`0640`; the setgid directory gives them the pipeline group without granting that
group write access. The future pipeline runs with this group and read-only spool
access. The general `agent`/`diary` identities are not members and have no spool
access.

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

The first command installs/enables the three system timers and prepares the
spool and trusted-config modes. The second installs the mail export drop-in;
the next existing mail timer run uses it. No collector is triggered immediately
by installation.

Static checks on the approved files:

```bash
systemd-analyze verify \
  /home/kek/diary-approved/sync/diary-sync-runtime.service \
  /home/kek/diary-approved/sync/diary-collect@.service \
  /home/kek/diary-approved/sync/diary-collect-spond.timer \
  /home/kek/diary-approved/sync/diary-collect-vigilo.timer \
  /home/kek/diary-approved/sync/diary-collect-skolemelding.timer
```

Controlled verification must show all four collectors succeeding during a
controlled general-agent lock, identical IDs after service/process restart,
duplicate and malformed fixtures following the rules above, and `agent` plus an
unprivileged future-pipeline identity unable to read credentials or trusted
collector configuration. The pipeline identity must be able to read but not
write event files; the general agent must not be able to write them. PIPE-02
will create the actual pipeline service identity and its permanent containment.
