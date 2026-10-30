# signal-notify — write-only, note-to-self Signal proxy

The signal-cli daemon owns one JSON-RPC socket. Whoever can connect to it can
subscribe to **every incoming message**, list accounts, and `send` to **anyone**
as the user. Handing that socket to an agent-facing service hands over both the
data and the impersonation/exfiltration leg of the lethal trifecta.

The proxy narrows it to the one thing those services actually need: a note to
yourself.

```
agent service ──▶ /run/signal-notify/socket ──▶ signal-notify-proxy ──▶ signal-cli daemon
                   (0770 kek:diary)              (as kek, validates)
```

## Rules it enforces

| Rule | Why |
| --- | --- |
| only `method: "send"` | no `receive`, no `subscribe`, no `listAccounts` |
| `params.noteToSelf` must be `true` | the only legitimate use |
| any addressing field is **rejected**, not stripped | `recipient`, `recipients`, `groupId`, … — a caller trying to address someone else gets a hard error instead of a silent redirect |
| message ≤ 4000 chars | bounded payload |
| 20 messages/minute, single-use burst | a runaway agent cannot spam your phone |
| request is **reconstructed** before forwarding | nothing caller-supplied is passed through verbatim |

Rejections are logged to the journal (`[signal-notify] rejected: …`).

## Install

```bash
sudo-ask 'install -o root -g root -m 0755 /home/kek/diary/diary-scripts/scripts/signal_notify_proxy.py /usr/local/bin/signal-notify-proxy'
sudo-ask 'install -m 0644 /home/kek/diary/signal/signal-notify.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now signal-notify'
```

Runs as `kek` (it needs the upstream socket) with `Group=diary`, so the
`diary` group can reach `/run/signal-notify/socket`.

## Consumers

Point them at the proxy instead of the daemon:

```ini
Environment=SIGNAL_CLI_SOCKET=/run/signal-notify/socket
```

`dsh-web-notify` and `kimi-web-token` do exactly this. `signal-capture` must
keep using the **raw** socket — it is the receiver.

## Tests

```bash
uv run --project diary-scripts python -m pytest scripts/test_signal_notify_proxy.py
```

Covers the validation matrix and an end-to-end run against a fake upstream that
asserts a recipient-addressed send never reaches the daemon.
