# diary-sync — allow-listed socket broker for the sjekk's privileged steps

Four steps of a sjekk need something the agent must not have: the Spond
password (`pass` + GPG), the Vigilo/Skolemelding BankID tokens
(`~/.config/diary`), and kek's `systemctl --user`. Before this broker the
agent could not run those steps at all — `spond-sync` died on
`pass show`, `vigilo-sync`/`skolemelding-sync` on a permission error, and
`services.txt` on `systemctl --user`.

The fix is the same shape as [signal-notify](signal-notify.md): **do not hand
over the secret, hand over the one operation.** A small broker runs as the
account owner in a systemd unit and exposes four verbs on a group-accessible
Unix socket.

```
agent (diary group) ──▶ /run/diary-sync/socket ──▶ diary-sync-broker (as kek)
                         0770 kek:diary             │
                                                    ├─ spond-sync --once
                                                    ├─ vigilo-sync --once
                                                    ├─ skolemelding-sync --once
                                                    └─ systemctl --user …
```

## Protocol

One JSON object per line, one reply per line.

| Request | Effect |
| --- | --- |
| `{"cmd": "sync", "target": "spond"}` | runs `spond-sync --once` |
| `{"cmd": "sync", "target": "vigilo"\|"skolemelding"\|"all"}` | same for those |
| `{"cmd": "start", "unit": "mail-sync.service"}` | `systemctl --user start` (the only startable unit) |
| `{"cmd": "status"}` | `is-active` + last run for the pipeline units |
| `{"cmd": "ping"}` | liveness/version |

Reply: `{"ok": bool, "cmd": …, "output": "<tail>", "exit_code": n, …}`.

## Rules it enforces

| Rule | Why |
| --- | --- |
| **only the documented request fields** | extra `argv`, `env`, `cwd`, `path`, and similar fields are rejected before a command runs; targets and units come from fixed tables |
| `start` only for `mail-sync.service` | the one unit the sjekk needs to poke (the timer runs it anyway) |
| `sync` 6/hour (burst 6), `status` 120/min | a runaway agent cannot hammer the upstreams |
| one sync at a time | the second concurrent request waits up to 240 s for the slot, then gets `busy` |
| 64 KiB request cap, 4000-char output tail | bounded payloads both ways |
| rejections logged to the journal | `[diary-sync] 'sync' -> rate limit exceeded` |

The socket is `0660` in a `0750` runtime dir owned by `kek:diary` — group
membership *is* the capability, and `diary` contains only `agent` and `kek`.
The service environment pins `DIARY_ROOT` and both token-file paths. The
explicit data-root environment takes precedence over `paths.project`, and each
token-file environment takes precedence over its TOML setting, so the ordinary
publishable configuration cannot redirect privileged reads or writes. The
host unit additionally hides unrelated home and user-runtime paths and bind
mounts only the fixed inputs and outputs. The installer pins those mount
sources against agent replacement, and all three JSONL sinks use dirfd-relative
no-follow appends so output symlinks cannot select another write target; see
the diary `sync/README.md`.

## Client

`diary-sync` is the client (console entry point, stdlib only):

```bash
uv run --project diary-scripts diary-sync sync spond
uv run --project diary-scripts diary-sync status
uv run --project diary-scripts diary-sync status --json
uv run --project diary-scripts diary-sync sync vigilo --fallback
```

`--fallback` runs the same work **in the caller's process** when the socket is
unreachable. That is what keeps a plain kek shell working during a broker
outage — and it is why `sjekk-collect` uses it everywhere: the same command is
correct for both identities.

## Install (as root; broker must not run from the venv)

```bash
sudo-ask /home/kek/diary/sync/install
```

The installed copy is stdlib-only on purpose: root must never execute code out
of an unreviewed checkout. The installer reads the broker only from the
human-approved standalone `diary-scripts` tree, and secret lookups use fixed
argv rather than configurable shell commands (same boundary rule as
`sudo-broker`).

## Verify

```bash
# as the agent — must work without any secret access:
uv run --project diary-scripts diary-sync ping
uv run --project diary-scripts diary-sync status
uv run --project diary-scripts diary-sync sync spond

# as kek — the broker's own log:
journalctl -u diary-sync-broker -n 20
```

## Consumers

| Consumer | Uses |
| --- | --- |
| `sjekk-collect` Phase A | `sync spond`, `sync vigilo`, `sync skolemelding`, `start mail-sync.service` |
| `sjekk-collect` `services.txt` | `status` (the track that used to fail with `systemctl --user`) |
| `_last_mail_sync()` freshness | `status --json` when `systemctl --user` is unavailable |

## What it does not solve

The agent still reads every byte the syncs write into `memory/` — that is the
point. What it no longer has is the ability to *become* the account: no GPG
key, no BankID token, no arbitrary command as kek. In trifecta terms this
removes the "run as the user" leg, not the data the sjekk legitimately reads.
