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
manual-service, and fallback collection. Both system services bind that fixed
directory. The broker's lock path is independent of its
`XDG_RUNTIME_DIR=/run/diary-sync`, which belongs to the user-manager IPC mount;
the manual service uses `XDG_RUNTIME_DIR=/run/user/1000`. A plain kek fallback
uses the same fixed lock directory and creates it owner-writable if the broker
is down.

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
- Pending IDs for the whole run are persisted in one journal write before the
  first spool write, and a no-change schedule rewrites the journal at most
  twice per run rather than once per message.
- Timeout, `OSError`, and transient database/read failures stay pending for the
  next schedule. Only deterministic malformed/oversized content is quarantined.
- A pending ID that no longer resolves to any file at all (the message was
  deleted) is dropped from the journal instead of being re-queried on every
  schedule; nothing remains to export.
- Quarantined variants do not block other messages or get reread on every
  schedule. Entries remain local review state and require explicit owner action
  to clear.

`EventSpool.write()` returns `written` or `duplicate`. A duplicate with different
content raises `EventConflictError`; neither path overwrites the existing file.
Malformed events raise `EventValidationError`. Writes use a same-directory
temporary file, `fchmod` then file `fsync`, and an atomic hard-link-without-
replacement. The temporary name is unlinked **before** the directory `fsync` and
acknowledgement, so acknowledged entries are single-link readable files. A crash
between the final link and that unlink leaves a second link; the next write for
the same event removes a leftover temp that still shares the committed inode
(after the normal retry window, so a live writer is not raced) and repairs the
entry instead of failing on it forever. The repair is confined to entries named
`.<event_id>.<pid>.<hex>.tmp` whose inode is the committed event, so a symlink, a
foreign file under that name, or a legitimate extra hard link is left untouched
and the entry stays rejected. Readers reject links, multiple hard
links, oversized files, invalid UTF-8/JSON, duplicate JSON keys, unknown fields,
non-finite numbers, excessive nesting, and IDs that do not match their immutable
identity.

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
obsolete `mail-sync` `ExecStartPost` drop-in. Follow the exact quiesced,
two-migration sequence in [`legacy-state.md`](legacy-state.md) when the diary
change deletes tracked legacy files. Installation does not immediately run a
collector.

## Isolated readiness evidence

Run these checks against the exact approved trees. They use fabricated records,
temporary directories, and mocked privileged commands; they do not collect
external data, send notifications, install units, or change lock state.

```bash
cd /home/kek/diary-scripts-approved
DIARY_SYNC_INSTALLER=/home/kek/diary-approved/sync/install \
DIARY_PIPE01_ROOT=/home/kek/diary-approved \
uv run pytest \
  scripts/test_event_spool.py scripts/test_event_export.py \
  scripts/test_trusted_collection.py scripts/test_diary_sync_broker.py \
  scripts/test_diary_sync_install.py scripts/test_legacy_state_quiesce.py \
  scripts/test_legacy_state_install.py mail_reader/test_config.py

bash -n /home/kek/diary-approved/sync/install
bash -n /home/kek/diary-approved/systemd/install-user
systemd-analyze verify \
  /home/kek/diary-approved/sync/diary-sync-runtime.service \
  /home/kek/diary-approved/sync/diary-sync-broker.service \
  /home/kek/diary-approved/sync/diary-collect@.service
systemd-analyze verify --user \
  /home/kek/diary-approved/systemd/units/diary-mail-event-export.service \
  /home/kek/diary-approved/systemd/units/diary-mail-event-export.timer
```

Require every test to run without a skip or failure. The fixtures prove stable
IDs in fresh processes, immutable duplicate/conflict behavior, malformed input
rejection, crash recovery, archived pending-mail recovery, per-message
quarantine, legacy/trusted/spool separation, broker/manual cursor behavior,
fixed shared locks, config unreadability, and timer/install policy. Do not inject
malformed files into the production spool to recheck those cases.

## Exclusive locked-window collector test

**This is a prepared procedure, not live evidence.** Schedule it only after
review, exact-OID publication, deployment, and the isolated checks above. The
owner must authorize one exclusive window with no sibling agents, deployments,
publication, or other acceptance runs. Use the already-approved CTL controlled
window procedure to enter and leave general-agent lock; this card does not define
a second lock/unlock or cleanup path. Local recovery must be available. The only
permitted external operations are ordinary mail collection and the three manual
collectors—no Signal notification, model call, or prohibited periodic collector.

Create a root-owned mode-0700 evidence directory outside both repositories and
outside all collector state roots. Substitute one owner-selected run ID for
`<run-id>` throughout. Record only OIDs, unit states, timestamps, counts, modes,
and SHA-256 manifests, never credentials, message bodies, or collector payloads.

```bash
sudo-ask 'install -d -o root -g root -m 0700 \
  /var/lib/diary/pipe01-evidence \
  /var/lib/diary/pipe01-evidence/<run-id>'

git -C /home/kek/diary-approved rev-parse HEAD
git -C /home/kek/diary-scripts-approved rev-parse HEAD
sudo-ask 'stat -c "%a %U:%G %n" /etc/diary/trusted.toml \
  /var/lib/diary/collector-state /var/lib/diary/events'
sudo-ask 'find /var/lib/diary/events -maxdepth 1 -type f -name "evt_*.json" \
  -exec sha256sum {} + | sort -k2 \
  > /var/lib/diary/pipe01-evidence/<run-id>/spool-initial.sha256'
```

Require the two OIDs to equal the reviewed result and the modes to be `640
root:kek`, `700 kek:kek`, and `2750 kek:diary-pipeline`, respectively. Record the
loaded `FragmentPath` and `ExecStart` for the broker, manual template, and mail
export units; every trusted executable must resolve below
`/home/kek/diary-scripts-approved` or its root-owned installed path.

**Mandatory timer gate before any broker or manual collection.** No external
collector invocation has occurred yet. Run this preflight before step 1:

```bash
for source in spond vigilo skolemelding; do
    systemctl show "diary-collect-$source.timer" \
      -p UnitFileState -p ActiveState
done
sudo-ask --as kek 'XDG_RUNTIME_DIR=/run/user/1000 \
  systemctl --user is-enabled mail-sync.timer diary-mail-event-export.timer'
sudo-ask --as kek 'XDG_RUNTIME_DIR=/run/user/1000 \
  systemctl --user is-active mail-sync.timer diary-mail-event-export.timer'
```

Require all three prohibited timers to read `UnitFileState=disabled` and
`ActiveState=inactive`, and both mail timers to be enabled and active. Any other
result blocks the run before external collection; do not repair it by enabling,
masking, or starting a collector timer during this test.

1. **Broker-then-manual cursor check, before entering lock.** For each source in
   turn, run the broker as kek, snapshot the spool, run the manual system
   service, and verify that every broker-era event is still byte-identical.
   Repeat for `vigilo` and `skolemelding`.

   ```bash
   sudo-ask --as kek 'XDG_RUNTIME_DIR=/run/user/1000 \
     /home/kek/diary-scripts-approved/.venv/bin/diary-sync sync spond'
   sudo-ask 'find /var/lib/diary/events -maxdepth 1 -type f -name "evt_*.json" \
     -exec sha256sum {} + | sort -k2 \
     > /var/lib/diary/pipe01-evidence/<run-id>/spond-after-broker.sha256'
   sudo-ask 'systemctl start diary-collect@spond.service'
   sudo-ask 'sha256sum -c \
     /var/lib/diary/pipe01-evidence/<run-id>/spond-after-broker.sha256'
   sudo-ask 'systemctl show diary-collect@spond.service \
     -p Result -p ExecMainStatus -p ExecMainExitTimestamp'
   ```

   Require each broker invocation and manual start to succeed, every checksum to
   report `OK`, and `Result=success` plus `ExecMainStatus=0`. New manual events
   are allowed; disappearance or modification of an existing immutable event is
   a failure. Save the equivalent evidence for all three sources.

2. **Enter the exclusive locked window.** Immediately before lock, open an
   owner-authorized root shell with `sudo-ask 'bash'` and keep it open. All
   commands in steps 2–6 run in that existing shell; do **not** make a new
   ordinary `sudo-ask` request while locked, because CTL-05 must deny it. Follow
   the CTL owner procedure to lock, then record authoritative state and policy
   proof before any collector starts:

   ```bash
   runuser -u kek -- /usr/local/bin/diary-lock status
   /usr/local/sbin/diary-network-policy verify locked
   ```

   Require `LOCKED` and successful policy verification. If either is unavailable,
   stop here: the live card remains blocked, and the CTL procedure owns recovery.
   Snapshot `spool-before-lock.sha256` with the same manifest command, run
   directly in this root shell.

3. **Run all four trusted collectors while locked.** These starts are oneshots
   and wait for completion. Do not use an agent broker `sync` request here;
   locked broker rejection belongs to CTL-05, not to this trusted-service test.

   ```bash
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user start mail-sync.service
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user start diary-mail-event-export.service
   systemctl start diary-collect@spond.service
   systemctl start diary-collect@vigilo.service
   systemctl start diary-collect@skolemelding.service
   ```

   For each of the five starts, capture `Result`, `ExecMainStatus`, and
   `ExecMainExitTimestamp`. Require success/status zero and a completion timestamp
   inside the locked interval for mail collection, mail export, and each manual
   collector. An authentication failure or skipped collector is not a pass.

4. **Restart and check immutability/duplicates.** Restarting the broker must not
   itself collect. Restart the mail exporter and each manual oneshot to obtain
   fresh processes, then validate every pre-lock event and every spool entry.

   ```bash
   systemctl restart diary-sync-broker.service
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user restart diary-mail-event-export.service
   systemctl restart diary-collect@spond.service
   systemctl restart diary-collect@vigilo.service
   systemctl restart diary-collect@skolemelding.service
   sha256sum -c \
     /var/lib/diary/pipe01-evidence/<run-id>/spool-before-lock.sha256
   find /var/lib/diary/events -maxdepth 1 -type f ! -links 1 -print
   cd / && PYTHONPATH=/home/kek/diary-scripts-approved \
     /usr/bin/python3 -P -c 'from scripts.event_spool import EventSpool; \
     print("validated_events=" + str(len(list(EventSpool("/var/lib/diary/events")))))'
   ```

   Require all old hashes to remain `OK`, no multi-link file output, successful
   strict validation, and successful restart statuses. The mail export journal
   must report duplicates without conflict; additional fresh events are allowed.
   Stable IDs across fabricated process restarts and malformed/quarantine cases
   are evidenced by the isolated suite, not by damaging production events.

5. **Prove the credential/config and spool access matrix.** As root in the same
   window, use `nobody` with primary group `diary-pipeline` as the unprivileged
   future-pipeline proxy; do not create a user or change group membership.

   ```bash
   for path in /etc/diary/trusted.toml \
       /home/kek/.config/diary/vigilo-tokens.json \
       /home/kek/.config/diary/skolemelding-auth.json \
       /home/kek/.password-store/spond/user.gpg \
       /home/kek/.password-store/vigilo/client-id.gpg \
       /home/kek/.password-store/vigilo/client-secret.gpg; do
       test -f "$path" || exit 1
       runuser -u agent -- test ! -r "$path" || exit 1
       runuser -u agent -- test ! -w "$path" || exit 1
       runuser -u nobody -g diary-pipeline -- test ! -r "$path" || exit 1
       runuser -u nobody -g diary-pipeline -- test ! -w "$path" || exit 1
   done
   for permission in r w x; do
       runuser -u agent -- test ! -"$permission" /var/lib/diary/events || exit 1
       runuser -u agent -- test ! -"$permission" /var/lib/diary/collector-state || exit 1
       runuser -u nobody -g diary-pipeline -- \
         test ! -"$permission" /var/lib/diary/collector-state || exit 1
   done
   runuser -u nobody -g diary-pipeline -- test -r /var/lib/diary/events
   runuser -u nobody -g diary-pipeline -- test -x /var/lib/diary/events
   runuser -u nobody -g diary-pipeline -- test ! -w /var/lib/diary/events
   event=$(find /var/lib/diary/events -maxdepth 1 -type f -name 'evt_*.json' -print -quit)
   test -n "$event"
   stat -c '%h %a %U:%G %n' "$event"
   runuser -u nobody -g diary-pipeline -- test -r "$event"
   runuser -u nobody -g diary-pipeline -- test ! -w "$event"
   runuser -u agent -- test ! -r "$event"
   runuser -u agent -- test ! -w "$event"
   ```

   Require every command to succeed, and the selected event to show one link,
   mode `640`, and group `diary-pipeline`. Repeat this matrix with the real
   `diary-pipeline` user after PIPE-02 creates it.

6. **Read back state separation and timer policy.** Require no tracked legacy
   cursor/JSONL paths, owner-only trusted state, and only runtime legacy mirrors:

   ```bash
   runuser -u kek -- git -C /home/kek/diary ls-files \
     'memory/spond*' 'memory/vigilo*' 'memory/skolemelding*'
   find /var/lib/diary/collector-state -maxdepth 1 -type f \
     -printf "%m %u:%g %f\n"
   find /var/lib/diary/legacy-state -maxdepth 2 -type f \
     -printf "%m %u:%g %p\n"
   for source in spond vigilo skolemelding; do
       systemctl show "diary-collect-$source.timer" \
         -p UnitFileState -p ActiveState
   done
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user is-enabled mail-sync.timer diary-mail-event-export.timer
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user is-active mail-sync.timer diary-mail-event-export.timer
   runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user show diary-mail-event-export.timer \
       -p OnCalendar -p Persistent -p Unit
   ```

   Require empty `git ls-files` output, `0600` trusted files, legacy files only
   below `/var/lib/diary/legacy-state`, all three prohibited timers
   `UnitFileState=disabled` and `ActiveState=inactive`, both mail timers enabled
   and active, and the independent export timer's `OnCalendar=*:1/2`,
   `Persistent=yes`, and `Unit=diary-mail-event-export.service`. Do not enable,
   mask, or start a prohibited timer during cleanup.

**Mandatory timer read-back after CTL-owned cleanup.** Restore the expected dark
state with the same CTL procedure, then repeat the timer checks in the existing
owner root shell before closing it:

```bash
for source in spond vigilo skolemelding; do
    systemctl show "diary-collect-$source.timer" \
      -p UnitFileState -p ActiveState
done
runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
  systemctl --user is-enabled mail-sync.timer diary-mail-event-export.timer
runuser -u kek -- env XDG_RUNTIME_DIR=/run/user/1000 \
  systemctl --user is-active mail-sync.timer diary-mail-event-export.timer
```

Require the same prohibited disabled/inactive and mail enabled/active states as
in the pre-collection gate and step 6. A cleanup without this third passing
read-back is not closeout evidence.

## Closeout evidence checklist

PIPE-01 remains incomplete until the owner reviews all of this evidence and the
separate deployment/status record is committed:

- [ ] Reviewed diary/scripts OIDs equal the deployed OIDs and the diary gitlink.
- [ ] Complete isolated test and systemd verification logs pass with no skips.
- [ ] Exclusive-window authorization, start/end timestamps, authoritative
  `LOCKED` proof, and CTL-owned dark-state restoration are recorded.
- [ ] Broker-then-manual checksum evidence for all three manual sources shows no
  lost or changed event and no shared-cursor regression.
- [ ] Locked mail collection, mail export, Spond, Vigilo, and Skolemelding all
  have successful statuses and in-window completion timestamps.
- [ ] Pre/post-restart manifests and strict validation prove immutable IDs and
  single-link files; duplicate logs show no conflict or overwrite.
- [ ] Archived pending recovery, per-message quarantine, malformed input, and
  fresh-process ID stability are linked to the passing isolated fixture logs.
- [ ] The agent/future-pipeline credential, trusted-config, collector-state, and
  event access matrix passes, including event mode/group/link count.
- [ ] Trusted, legacy, and spool roots remain separate; no legacy runtime data is
  tracked; no periodic tracked-cursor commit reappears.
- [ ] Prohibited timers are disabled/inactive and both mail timers are
  enabled/active in all three required read-backs: before any broker/manual
  collection, at step 6, and after CTL-owned cleanup.
- [ ] No notification/model operation occurred, no rejected operation was
  replayed, and evidence contains no credentials or private event payloads.
- [ ] Any missing check is recorded as a blocker, not waived or converted to a
  pass. PIPE-02's real identity and permanent containment remain separate gates.
