# sudo-broker — per-command approval from a terminal, as a named user

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
sudo-ask --as kek 'systemctl --user restart mail-sync'
```

Nothing runs until you type `y`. **There is no answer timeout** — a request
waits until you have read it and decided, because being hurried is how a wrong
command gets approved. Closing the terminal (or Ctrl-C, or 30 minutes with
nothing connected) removes the capability entirely.

## Running as another user

A request names the identity it needs, and the daemon drops to that uid/gid
for that one command:

```bash
sudo-ask systemctl restart caddy            # root, by default
sudo-ask --as kek git -C ~kek/diary status  # only kek's files: no root needed
```

`ALLOWED_USERS` in `sudo_broker.py` is a short hard-coded list — `root` and
`kek` as installed here — and nothing else resolves; a request for any other
name is refused before the human is asked, and the refusal is logged. It is a
constant rather than an environment knob on purpose: widening the set should
be a deliberate edit to a root-owned file, not something a caller can arrange.

The prompt and the notification show the identity (`│ as   : kek`), and the
audit line records it (`APPROVED (as kek)`), so "what did I actually approve?"
is answered by the terminal, not by memory. The point is that a careless `y`
should not buy more than the task needed: most requests only touch kek's
files or kek's user session, and those no longer run as root. Root stays in the
list because package installs, `/etc` and system units genuinely need it.

The child gets a small environment built for that user: `HOME`, `USER`,
`LOGNAME`, `SHELL`, a fixed `PATH`, `TERM=dumb`, and — when it exists —
`XDG_RUNTIME_DIR` plus `DBUS_SESSION_BUS_ADDRESS`, which is what
`systemctl --user` and every desktop tool need. Supplementary groups are the
user's own, so group-owned files stay writable.

What this does **not** buy: the daemon itself is still root, so a compromised
broker is still root, and the human is still the control. It only shrinks what
one approval hands over.

## Getting your attention

A request arrives in three layers, all optional except the prompt itself:

1. **A desktop notification** — the broker writes kitty's [OSC 99][osc99]
   notification escape with `a=focus` and `o=unfocused`, so kitty (and foot)
   show a native notification *only while the terminal is not focused*, and
   clicking it brings the window forward. The body names the requesting user
   and the exact command. One notification per request; a later request
   replaces the previous one instead of stacking.
2. **The terminal bell** (`BEL`, `\a`) right as the prompt appears, and again
   every 30 s while the prompt is unanswered. With kitty's `bell_on_tab` the
   tab gets marked too.
3. **The prompt box** in the terminal, which stays until answered.

Both the bell and the notification go to the first of stdout and stderr that
is a terminal. That matters if you keep an audit trail with
`sudo-broker 2>audit.log`: the prompt, bell and notification stay on your
terminal, and the log gets none of them. When neither stream is a tty, nothing
is written at all.

[osc99]: https://sw.kovidgoyal.net/kitty/desktop-notifications/

Other mechanisms, if you want to change it: OSC 9 (`\e]9;text\a`) is the
simpler iTerm2/GNOME-style notification, `notify-send` reaches the GNOME
notification centre directly but needs the session bus and cannot focus the
terminal from a root daemon, and `kitty @ focus-window` needs
`allow_remote_control` in `kitty.conf`. OSC 99 is the one that both shows up
and takes you to the window when clicked, without widening kitty's remote
control surface.

## Why this shape

- **No password ever travels.** The daemon *executes* approved commands; it
  never hands out credentials, and no askpass process passes a password
  through a path an agent could read.
- **No pre-authorisation.** There is no allowlist to widen, no token to
  replay: each request is shown with the requesting uid/pid/cwd and dies
  single-use.
- **The command cannot touch your terminal.** The child gets
  `stdin=/dev/null`, so an approved command can't read what you type next.
- **Fail-closed.** No socket → `sudo-ask` exits 2 with instructions. A denial
  or an empty answer exit 1 and run nothing. A request nobody answers stays
  open — it never turns into an approval by itself.
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
| 1 | denied, or a malformed request (an unanswered one waits) |
| 124 | approved, but killed after `EXEC_TIMEOUT` (600 s) |

`sudo-ask 'cmd with | pipes && logic'` takes the whole string as one argument;
argv is joined with spaces otherwise, so shell syntax needs quoting. `--as
USER` (or `--as=USER`) may lead, before the command.

## Tests

The prompt behaviour — bells, notification escapes, routing between stdout and
stderr, suppression into redirected streams, the refusals — runs **in process**
in the normal suite, with the timers shrunk and no root or pty involved:

```bash
uv run pytest scripts/test_sudo_broker.py
```

The daemon itself needs root and a terminal, so the end-to-end half drives a
real daemon on a pty and types `y`/`n` at it:

```bash
sudo /usr/bin/python3 scripts/test_sudo_broker.py
```

Covers: approval runs and streams the command's exit code, denial runs
nothing, an empty answer is a denial, the prompt rings the bell and keeps
ringing while unanswered (and stops once answered), there is no answer
timeout, a second request while one is pending is refused, the notification
escape carries the focus action and the command, bell/notification follow
whichever of stdout/stderr is the terminal without ever landing in a redirected
log, the requested identity is shown in the prompt and actually used, an
unlisted identity is refused before the human is asked, and the per-user
environment carries `HOME`/`USER`/`XDG_RUNTIME_DIR`. A missing broker fails
closed.

## Removing a standing NOPASSWD rule

```bash
sudo cp /etc/sudoers.d/<file> /root/<file>.bak
sudo rm /etc/sudoers.d/<file>
sudo visudo -c           # parses every file in sudoers.d
```

Keep `%wheel ALL=(ALL) ALL` so password sudo still works as a fallback.
