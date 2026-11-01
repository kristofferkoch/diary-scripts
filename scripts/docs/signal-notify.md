# signal-notify — restricted note-to-self and attachment proxy

The signal-cli daemon owns one JSON-RPC socket. Whoever can connect to it can
subscribe to **every incoming message**, list accounts, and `send` to **anyone**
as the user. Handing that socket to an agent-facing service hands over both the
data and the impersonation/exfiltration leg of the lethal trifecta.

The proxy exposes only note-to-self sends and read-only attachment downloads.

```
agent service ──▶ /run/signal-notify/socket ──▶ signal-notify-proxy ──▶ signal-cli daemon
                   (0770 kek:diary)              (as kek, validates)          sends only
```

## Rules it enforces

| Rule | Why |
| --- | --- |
| only `send` and `getAttachment` in legacy mode | no `receive`, no `subscribe`, no `listAccounts` |
| `send` requires `params.noteToSelf: true` | the only legitimate recipient |
| any addressing field is **rejected**, not stripped | a caller trying to address someone else gets a hard error instead of a silent redirect |
| message ≤ 4000 chars; 20 sends/minute | bounded payload and anti-spam |
| send request is **reconstructed** | nothing caller-supplied is passed through verbatim |
| attachment ID is a basename of `[A-Za-z0-9_.-]` | no paths or traversal |
| attachment must be a regular file, opened without following symlinks | no reading arbitrary files through links |
| attachment ≤ 64 MiB; at most two concurrent reads | bounded memory and response size |

`getAttachment` reads directly from the attachment store and never calls the
upstream daemon. Rejections are logged to the journal.

## PIPE-05 locked pipeline notifications

The default remains the existing audit-only behavior. A future approved,
controlled installation may add `--enforce-lockdown`; until then the proxy does
not query lock state and existing attended/agent send behavior is unchanged.
Read-only attachment behavior is unchanged in both modes.

With enforcement enabled, every send requires a successful trusted audit write
and a valid controller status. `LOCKING`, `LOCKED`, unknown state, missing audit,
and missing identity all fail closed. Ordinary `send` is allowed only in
`ATTENDED`; it is rejected and audited in `LOCKING`/`LOCKED` and is never queued
or replayed on unlock.

The trusted pipeline instead uses `sendPipelineNotification`. Authorization
uses the kernel `SO_PEERCRED` UID of the `diary-pipeline` account, never a
request field. The request has exactly this shape:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "sendPipelineNotification",
  "params": {
    "notification": {
      "event_id": "mail:event-123",
      "urgency": "normal",
      "summary": "Sanitized one-line summary",
      "deadline": null,
      "review_reference": "local/review/event-123"
    }
  }
}
```

This is the PIPE-04 rendered-notification contract: PIPE-04 sanitizes the five
model-result fields and emits no recipient, final message, or envelope. PIPE-05
revalidates the exact schema, rejects unknown fields, URLs, control/formatting
characters, invalid urgency/deadlines, and excessive lengths, then constructs
the fixed `Diary triage notification` note-to-self envelope itself. Urgency is
`low`, `normal`, or `high`; deadline is null or ISO 8601. Summaries are capped
at 1000 characters and review references at 256.

Pipeline sends are limited to one per event per five minutes and six per minute
in aggregate. Rate-limited sends are audited and dropped without retry state.
Audit records contain decision metadata and event ID, never summary or message
content.

## Download an attachment

Take the `id` from the attachment metadata in `memory/signal/YYYY-MM-DD.jsonl`,
then write a new local file through the proxy:

```bash
uv run signal-attachment '<id>' --output memory/signal/<id>
```

The CLI refuses to overwrite an existing file. Use `--output -` for binary
stdout. No sudo or direct access to the account owner's attachment directory is
needed.

## Install

```bash
sudo-ask 'install -o root -g root -m 0644 /path/to/diary-scripts-approved/scripts/attachment_store.py /usr/local/bin/attachment_store.py'
sudo-ask 'install -o root -g root -m 0644 /path/to/diary-scripts-approved/scripts/broker_audit.py /usr/local/bin/broker_audit.py'
sudo-ask 'install -o root -g root -m 0644 /path/to/diary-scripts-approved/scripts/diary_lock.py /usr/local/bin/diary_lock.py'
sudo-ask 'install -o root -g root -m 0644 /path/to/diary-scripts-approved/scripts/pipeline_notification.py /usr/local/bin/pipeline_notification.py'
sudo-ask 'install -o root -g root -m 0755 /path/to/diary-scripts-approved/scripts/signal_notify_proxy.py /usr/local/bin/signal-notify-proxy'
sudo-ask 'install -m 0644 /path/to/diary-approved/signal/signal-notify.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now signal-notify'
```

Runs as the account owner with `Group=diary`, so the `diary` group can reach
`/run/signal-notify/socket`. The existing `ProtectHome=read-only` confinement
already permits attachment reads.

Enabling enforcement is a separate reviewed deployment after PIPE-02 provides
the trusted account and PIPE-04 emits this contract. It requires a root-owned
service override that replaces `ExecStart` with the same command plus
`--enforce-lockdown`; the checked-in unit deliberately does not enable it.
Live acceptance must then prove trusted delivery, spoof/schema/rate rejection,
lock/audit failure denial, and no replay after unlock, and leave enforcement
dark after the controlled test until ACT-01.

## Consumers

Point senders at the proxy instead of the daemon:

```ini
Environment=SIGNAL_CLI_SOCKET=/run/signal-notify/socket
```

`dsh-web-notify` and `kimi-web-token` do exactly this. `signal-capture` must
keep using the **raw** socket — it is the receiver.

## Tests

```bash
uv run --project diary-scripts pytest scripts/test_attachment_store.py scripts/test_signal_notify_proxy.py scripts/test_signal_attachment.py scripts/test_pipeline_notification.py scripts/test_signal_lockdown.py
```

Covers send validation, attachment traversal and symlink rejection, an
end-to-end proxy read, the download CLI, the strict PIPE-04 contract, trusted
identity and spoof rejection, lock/audit fail-closed behavior, per-event and
aggregate rates, and no execution or replay of rejected sends.
