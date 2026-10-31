# Trusted diary-lock audit

`diary-auditd` is the AUD-01 root-owned writer and report reader. Its systemd
unit uses `LogNamespace=diary-lock`, which creates a dedicated native journal
socket and persistent journal files outside `agent` and `diary-pipeline` write
access. The namespace has `MaxRetentionSec=30day` and daily files so old data is
actually vacuumed promptly. It is operational isolation, not append-only or
cryptographic tamper evidence; off-host export is LATER-04.

## Trust boundary

The daemon is installed as a root-owned stdlib-only file. It never runs from a
worktree or the uv environment. `/run/diary-audit/socket` is `root:diary-audit`
mode `0660`; each connection also requires kernel peer UID 0 or an account in
`diary-audit`, with `agent` and `diary-pipeline` explicitly denied even if a
group is misconfigured. The native journal namespace socket has its own
`systemd-journald@diary-lock.socket` drop-in because its upstream default is
world-writable `0666`; the deployed mode is also `0660 root:diary-audit`.
The separate journald Varlink control socket is restricted identically.
The installer adds only `kek`, and explicitly removes `agent` and
`diary-pipeline`. Both trusted writes and fixed reports use the daemon boundary;
untrusted identities cannot bypass it through the native journal socket.

Writer PID/UID/GID come from `SO_PEERCRED`. A broker may additionally supply a
`caller` object only when it obtained PID/UID/GID from its own peer credentials
(or equally trusted systemd metadata). The daemon resolves user names itself.
Agent-provided names, session IDs, or unit fields are never authoritative.
When no broker caller is present, caller identity is the kernel writer identity.

## Event convention

A write is one JSON object:

```bash
diary-audit write --json '{
  "event": "broker.decision",
  "component": "example-broker",
  "operation": "status",
  "decision": "would_allow",
  "lock_state": "locked",
  "reason": "read_only",
  "caller": {"pid": 123, "uid": 1001, "gid": 1001}
}'
```

Required fields are `event`, `component`, `operation`, `decision`, and
`lock_state`. Decisions are `allow`, `deny`, `would_allow`, `would_deny`,
`observed`, or `error`; lock states are `locking`, `locked`, `attended`,
`unknown`, or `not_applicable`. Optional metadata is limited to `reason`,
`event_id`, `session_id`, `tool`, `unit`, `destination_ip`, `destination_port`,
`protocol`, `dns_name`, and `caller`. Caller attribution may include trusted
`unit`, `session_id`, and `tool` metadata. Unknown fields fail closed.

Journal fields use the `DIARY_AUDIT_` prefix and schema version 1. Journald adds
its own receive timestamp, boot, PID, UID, GID, unit, and cgroup metadata. The
human `MESSAGE` is only a fixed event/decision label; structured consumers use
the custom fields.

Message bodies, URLs, complete search queries, commands, headers, and other
free-form content fields are omitted before fingerprinting or journaling. The
response and `DIARY_AUDIT_REDACTED` list omitted field names, never their
values. Free-form strings smuggled into metadata, unknown keys, invalid IP/DNS
values, control characters, and booleans used as integers are rejected. DNS
query *names* remain supported because the design explicitly requires them.

## Duplicates and restart behavior

The SHA-256 fingerprint covers the validated event and trusted caller metadata,
not the journal writer's process ID. The first event is emitted immediately;
identical events are suppressed for 60 seconds. The next emitted record has:

- `DIARY_AUDIT_COUNT`: cumulative occurrences for the fingerprint;
- `DIARY_AUDIT_NEW_COUNT`: occurrences since the previous emitted record;
- `DIARY_AUDIT_SUPPRESSED_COUNT`: suppressed occurrences in that interval;
- `DIARY_AUDIT_FIRST_SEEN_US` and `DIARY_AUDIT_LAST_SEEN_US` in Unix
  microseconds.

Counters are atomically persisted with fsync in root-only
`/var/lib/diary-audit/dedup.json` before suppression and after journal writes.
They therefore survive daemon restarts. State entries expire after 31 days,
just beyond journal retention. Reports sum `NEW_COUNT`, not cumulative
snapshots, so repeated windows are not overcounted. Journald's generic burst
rate limit is disabled in this namespace so it cannot silently discard distinct
validated events; the daemon's durable duplicate limiter is the audit rate
boundary. Native journal delivery is datagram-based: socket/write errors fail
the request, but journald does not return an fsync acknowledgement.

## Reports

```bash
diary-audit baseline --since 24h
diary-audit review --since 24h --json
```

`baseline` groups all validated records by fingerprint and reports occurrence
and stored-record counts. `review` includes only `deny`, `would_deny`, and
`error`, marks repeated groups, and is intended for the later independent UI.
Times are `now`, bounded durations (`30m`, `24h`, `7d`), or ISO-8601. Reports
accept no arbitrary matches, search strings, or journalctl flags. The daemon
uses a fixed argv and returns only allowlisted metadata, never raw `MESSAGE` or
unrecognized journal fields. At most 10,000 stored records are summarized;
`truncated` warns when the cap was reached.

## Approved installation and live verification

After the diary-scripts and diary commits (including the submodule pointer) are
owner-approved and synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-audit-root
```

The installer creates the group, installs root-owned copies and configuration,
reloads systemd, restarts only the audit namespace/daemon, writes and reads a
self-test as `kek`, and verifies that existing agent/pipeline identities cannot
use the socket. It leaves all general-agent enforcement dark.

Complete the card's live checks after installation:

1. Inspect the self-test with
   `journalctl --namespace=diary-lock DIARY_AUDIT_EVENT=audit.selftest -o json`.
   Confirm schema, event, decision, lock state, receive timestamp, kernel
   writer/caller fields, counters, and `DIARY_AUDIT_REDACTED=message`; confirm
   the test body is absent.
2. Submit the same event twice, then restart `diary-auditd.service`. After the
   60-second duplicate window submit it again and confirm the next record's
   cumulative and new counts include all three occurrences.
3. Reboot and repeat a report. Confirm the pre-reboot records and required
   fields remain, and inspect `/var/log/journal/` permissions to verify that
   agent/pipeline have no write path to namespace files or dedup state.
4. Confirm `systemd-analyze cat-config systemd/journald@diary-lock.conf` shows
   persistent storage and 30-day retention.

Only after those deployed checks should the deployment change delete AUD-01
from the board and update the source design's implementation record.
