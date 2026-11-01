# Trusted diary-lock audit

`diary-auditd` is the AUD-01 root-owned writer and report reader. Its systemd
unit uses `LogNamespace=diary-lock`, which creates a dedicated native journal
socket and persistent journal files outside `agent` and `diary-pipeline` write
access. The namespace has `MaxRetentionSec=30day` and daily files so old data is
actually vacuumed promptly. It is operational isolation, not append-only or
cryptographic tamper evidence; off-host export is LATER-04.

## Trust boundary

The daemon is installed as a root-owned stdlib-only file. It never runs from a
worktree or the uv environment. Client access and native journal access use
separate groups. `/run/diary-audit/socket` is `root:diary-audit` mode `0660`;
each connection also requires kernel peer UID 0 or an account in `diary-audit`,
with `agent` and `diary-pipeline` explicitly denied even if a group is
misconfigured. The native journal and Varlink sockets are instead
`root:diary-audit-journal` mode `0660`. Only the daemon has that primary group;
trusted API clients, including `kek`, have no direct native journal access.

The installer removes every supplemental member from the daemon-only group and
makes removal failures fatal. Because account-database changes do not alter
existing process credentials, it also checks `/proc` before changing sockets.
If any process with nonzero effective UID still holds
`diary-audit-journal` as its effective primary or supplementary group,
installation stops and names the PID/effective UID. This also catches processes
with real UID 0 that dropped to a nonzero effective UID. The owner must stop or
restart only those services in a controlled window and rerun the installer; it
never kills unrelated services.
The API additionally checks account identity on every connection, so retained
client-group credentials cannot make `agent` or `diary-pipeline` trusted. A
fixed `ping` request emits a metadata-only `audit.heartbeat` with a unique
emission ID and requires journald read-back of that exact record. This checks
the `JournalWriter`, journal socket, journald, and read path rather than merely
answering from the daemon. Missing/unwritable sockets and accepted-but-unstored
records fail the health check. Heartbeats bypass duplicate counters and are
excluded from baseline/review groups. `controller.break_glass` events are
warning priority even when their decision is `allow`, so local recovery
overrides are prominent.

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
`event_id`, `base_oid`, `session_id`, `tool`, `unit`, `destination_ip`,
`destination_port`, `protocol`, `dns_name`, and `caller`. The owner merge
console uses `event_id` for its reviewed queue OID and `base_oid` for the exact
approved-master base. Caller attribution may include trusted
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

## AUD-04 broker observation

`broker_audit.py` is the shared best-effort client used by Signal, `diary-sync`,
`agent-merge`, and the sudo broker. Each request produces one `broker.decision`
with `lock_state=locked`: this is a hypothetical locked-mode classification, not
a claim that the future controller is active. Safe operations use `allow`;
external actions use `would_deny`. Signal attachment reads and sync
`ping`/`status` are safe. Merge approval is `allow` only when the kernel caller
UID equals the merge service owner UID; other callers and missing owner identity
are `would_deny` with `reason=owner_required`. Signal sends, sync/start,
diary/scripts merge requests, and ordinary sudo are
`would_deny`. Unknown methods and unknown future brokers become
`operation=unknown`, `decision=would_deny`, and `reason=unknown_operation`.
There is no lock-safe sudo operation or break-glass path in this audit-only
phase.

Broker PID/UID/GID caller metadata comes from the request socket's
`SO_PEERCRED`; no client-supplied identity is accepted. Operation names come
from fixed policy tables, and commands, message bodies, branch names, and commit
OIDs are never included. The client makes one audit attempt and never queues or
replays the broker operation. Audit failure is deliberately non-fatal in AUD-04
so availability and responses remain unchanged; later enforcement must implement
its own fail-closed behavior. The merge socket front-end performs its audit once
before dispatch, including before its diary review fallback, and suppresses
helper stderr to preserve the socket protocol.

Installed brokers use the root-owned helper and reach the AUD-01 socket as root
or with `SupplementaryGroups=diary-audit`; general agents never receive that
group. Inspect parseable results with `diary-audit baseline --since 1h --json`
or the corresponding `DIARY_AUDIT_*` fields in the `diary-lock` journal.

## Duplicates and restart behavior

The SHA-256 fingerprint covers the validated event and trusted caller metadata,
not the journal writer's process ID. The first event is emitted immediately;
identical events are suppressed for 60 seconds. The next emitted record has:

- `DIARY_AUDIT_EMISSION_ID`: stable ID for idempotent delivery and report
  deduplication;
- `DIARY_AUDIT_COUNT`: cumulative occurrences for the fingerprint;
- `DIARY_AUDIT_NEW_COUNT`: occurrences since the previous emitted record;
- `DIARY_AUDIT_SUPPRESSED_COUNT`: suppressed occurrences in that interval;
- `DIARY_AUDIT_FIRST_SEEN_US` and `DIARY_AUDIT_LAST_SEEN_US` in Unix
  microseconds.

Counters and the complete pending emission are atomically persisted with fsync
in root-only `/var/lib/diary-audit/dedup.json` before every journal send. If the
process crashes after journald accepts a datagram but before the post-send
save, the next attempt reuses the same emission ID and counters. Reports count
each emission ID once while retaining the physical record and duplicate-record
counts, so this recovery cannot inflate occurrence totals.

State is bounded-line JSONL, not a whole-file JSON document. Startup streams
and validates one bounded record at a time, expires entries older than 31 days,
and atomically compacts the file; an oversized file of expired entries therefore
does not make the daemon permanently unloadable. At most 10,000 active
fingerprints are retained. A new fingerprint beyond that cap fails closed
rather than discarding active counters; malformed, oversized-line, duplicate
active-fingerprint, or over-capacity state also fails closed.

Reports sum `NEW_COUNT`, not cumulative snapshots, so repeated windows are not
overcounted. Journald's generic burst rate limit is disabled in this namespace
so it cannot silently discard distinct validated events; the daemon's durable
duplicate limiter is the audit rate boundary. Native journal delivery is
datagram-based: socket/write errors fail the request, but journald does not
return an fsync acknowledgement.

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

For AUD-05, `diary-production-baseline` consumes this fixed baseline API and
adds the production coverage checklist, duration evidence, expected/novel flow
classification, repeated-denial and suspicious-broker findings, and hash-bound
owner-review evidence. See [production-baseline.md](production-baseline.md).
That client does not collect live evidence or establish owner acceptance by
itself.

## Approved installation and live verification

After the diary-scripts and diary commits (including the submodule pointer) are
owner-approved and synchronized:

```bash
sudo-ask /home/kek/diary-approved/systemd/install-audit-root
```

The installer creates both groups, installs root-owned copies and configuration,
checks retained process credentials, reloads systemd, and restarts only the
audit namespace/daemon. The daemon uses `Type=notify`: it sends `READY=1` only
after state initialization and the listening socket succeed, so systemd orders
the self-test after actual readiness without polling. It writes and reads a
self-test as `kek`, verifies that
this trusted client has no direct native journal access, and verifies that
existing agent/pipeline identities can use neither path. General-agent
enforcement remains dark.

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
