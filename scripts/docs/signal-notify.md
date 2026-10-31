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
| only `send` and `getAttachment` | no `receive`, no `subscribe`, no `listAccounts` |
| `send` requires `params.noteToSelf: true` | the only legitimate recipient |
| any addressing field is **rejected**, not stripped | a caller trying to address someone else gets a hard error instead of a silent redirect |
| message ≤ 4000 chars; 20 sends/minute | bounded payload and anti-spam |
| send request is **reconstructed** | nothing caller-supplied is passed through verbatim |
| attachment ID is a basename of `[A-Za-z0-9_.-]` | no paths or traversal |
| attachment must be a regular file, opened without following symlinks | no reading arbitrary files through links |
| attachment ≤ 64 MiB; at most two concurrent reads | bounded memory and response size |

`getAttachment` reads directly from the attachment store and never calls the
upstream daemon. Rejections are logged to the journal.

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
sudo-ask 'install -o root -g root -m 0755 /path/to/diary-scripts-approved/scripts/signal_notify_proxy.py /usr/local/bin/signal-notify-proxy'
sudo-ask 'install -m 0644 /path/to/diary-approved/signal/signal-notify.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now signal-notify'
```

Runs as the account owner with `Group=diary`, so the `diary` group can reach
`/run/signal-notify/socket`. The existing `ProtectHome=read-only` confinement
already permits attachment reads.

## Consumers

Point senders at the proxy instead of the daemon:

```ini
Environment=SIGNAL_CLI_SOCKET=/run/signal-notify/socket
```

`dsh-web-notify` and `kimi-web-token` do exactly this. `signal-capture` must
keep using the **raw** socket — it is the receiver.

## Tests

```bash
uv run --project diary-scripts pytest scripts/test_signal_notify_proxy.py scripts/test_signal_attachment.py
```

Covers send validation, attachment traversal and symlink rejection, an
end-to-end proxy read, and the download CLI.
