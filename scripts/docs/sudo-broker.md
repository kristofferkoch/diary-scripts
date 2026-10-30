# sudo-broker — per-command root approval from a terminal

An agent that can reach private data *and* the internet should not hold
standing root. The broker replaces `NOPASSWD:ALL` with a request the human
sees and approves, one command at a time.

```bash
# 1. install (root-owned copies in /usr/local)
sudo diary/diary-scripts/scripts/install_sudo_broker

# 2. start it in a terminal you keep open — this *is* the capability
sudo /usr/local/sbin/sudo-broker

# 3. the agent asks; the prompt appears in your terminal
sudo-ask systemctl restart caddy
```

Nothing runs until you type `y`. Closing the terminal (or Ctrl-C, or 30
minutes of silence) removes the capability entirely.

## Why this shape

- **No password ever travels.** The daemon *executes* approved commands; it
  never hands out credentials, and no askpass process passes a password
  through a path an agent could read.
- **No pre-authorisation.** There is no allowlist to widen, no token to
  replay: each request is shown with the requesting uid/pid/cwd and dies
  single-use.
- **The command cannot touch your terminal.** The child gets
  `stdin=/dev/null`, so an approved command can't read what you type next.
- **Fail-closed.** No socket → `sudo-ask` exits 2 with instructions. A timeout,
  a denial, or an empty answer all exit 1 and run nothing.
- **Root executes a root-owned copy.** The daemon and client are stdlib-only
  and are installed to `/usr/local/sbin/sudo-broker` and
  `/usr/local/bin/sudo-ask`. Never run the daemon with `uv run` — that would
  execute code from a tree the agent can write, which is the whole thing we
  are trying to avoid.

## What it does not protect against

The human is the control. A command you approve runs as root, in full, with
`/bin/bash -c`. Read the line before typing `y` — it is shown escaped and
truncated at 4096 characters precisely so it stays readable. A request that
contains a newline or control characters is displayed escaped (`\n`, `\x1b`),
not executed verbatim, so what you see is what runs.

The socket is `0666` inside a `0755 root` directory: any local user may
*ask*, but only a human can *approve*. If you want to narrow who may ask,
change the socket mode and the peer check in `sudo_broker.py`.

## Client behaviour

| Exit | Meaning |
| --- | --- |
| command's own | approved and ran; stdout/stderr streamed through |
| 2 | no broker running, or the socket isn't root-owned |
| 1 | denied, no answer, or a malformed request |
| 124 | approved, but killed after `EXEC_TIMEOUT` (600 s) |

`sudo-ask 'cmd with | pipes && logic'` takes the whole string as one argument;
argv is joined with spaces otherwise, so shell syntax needs quoting.

## Tests

End-to-end, driving a real daemon on a pty (needs root and a tty):

```bash
sudo /usr/bin/python3 scripts/test_sudo_broker.py
```

Covers: approval runs and streams the command's exit code, denial runs
nothing, an empty answer is a denial, and a missing broker fails closed.

## Removing a standing NOPASSWD rule

```bash
sudo cp /etc/sudoers.d/<file> /root/<file>.bak
sudo rm /etc/sudoers.d/<file>
sudo visudo -c           # parses every file in sudoers.d
```

Keep `%wheel ALL=(ALL) ALL` so password sudo still works as a fallback.
