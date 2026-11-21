# dsh web token → Signal — `dsh-web-notify`

> Index: [scripts/](../README.md)

`dsh_web_notify.py` sends the dsh web UI's login URL (with its per-boot
`?token=`, printed to the journal at every start and impossible to pin) to
the user's Signal note-to-self, so a phone bookmark can be refreshed with
one tap after each restart. It polls the journal scoped to the service's
current `InvocationID` (no `--since` race with tokens printed before the
script is scheduled), rewrites the loopback authority to the public
Caddy/tailnet base (`hosts.dsh_web` in root-owned
`/etc/diary/trusted.toml`, with no local-config or environment override), and
sends via the signal-cli daemon's JSON-RPC socket with
`send`/`noteToSelf` — a plain client request, no receive loop, so the
single-receive rule is not violated.

Fires automatically from `dsh-web-token.service` (`WantedBy=dsh-web.service`,
`PartOf=dsh-web.service`) on every start/restart of `dsh-web.service`; unit
+ install steps live in `dsh/` in the diary repo. Manual run (also the
end-to-end check, harmless to repeat — sends the current token to self):

```bash
uv run --project diary-scripts dsh-web-notify            # send
uv run --project diary-scripts dsh-web-notify --dry-run  # print only
```

## kimi-web (and any other unit)

The same tool serves `kimi-web`, which prints its login URL with a **URL
fragment** — `http://127.0.0.1:8803/#token=…` — and whose token is
*persistent* (stored under `~/.kimi-code`, so the link survives restarts).
The fragment is easy to drop when the authority is rewritten, which is why
`public_url()` carries it through and `test_dsh_web_notify.py` pins both
forms.

```bash
dsh-web-notify --unit kimi-web            # uses hosts.kimi_web, label "kimi web"
dsh-web-notify --unit kimi-web --dry-run  # print, don't send
```

Flags: `--unit` (default `dsh-web`), `--scope user|system`, `--base-url`,
`--label`, `--timeout`, `--socket`, and — for the Basic-auth mode below —
`--password-from`, `--password-key`, `--probe`. The live user units use the default `user`
scope. The prepared LATER-03 system units pass `--scope system`, so the journal
and `systemctl show` need no `--user`; they execute a root-owned Python closure
under `/usr/local/libexec/diary-agents`, not `uv` with a home project.

Because kimi's token is stable, the message is a *liveness* signal rather
than a fresh credential — it confirms the service came back, and carries the
link you would otherwise have to dig out of the journal by hand.

Kimi uses `kimi/kimi-web-token.service` with the same
`PartOf`/`WantedBy=kimi-web` pattern as DSH.

## opencode-web (HTTP Basic, no token)

`opencode-web` is the odd one out: it has **no token at all**. It authenticates
with HTTP Basic (`OPENCODE_SERVER_PASSWORD`, username fixed to `opencode`) and
prints nothing to the journal — the only startup line about auth is a warning
when the password is missing. So this mode skips the journal entirely and reads
the password out of the unit's own env file:

```bash
dsh-web-notify --unit opencode-web \
    --password-from /home/agent/.config/opencode/env \
    --probe http://localhost:8804/ --dry-run
```

The message carries the plain link plus the credentials, because there is no
clickable token to hand over:

```
opencode web (re)started — login from phone:
https://server.example:8447/
user: opencode  password: <password>
```

`--probe` polls an HTTP URL and treats **any** status as ready — a 401 is the
expected answer once the server is listening and enforcing the password, and it
is exactly what the message reports. Without `--probe` the message is sent
immediately; with it, a restart that fails to come up sends no misleading
"login here" note.

Consequences worth knowing:

- The Signal note-to-self chat now holds a credential. It already held dsh's
  per-boot token, which is equivalent in power, but this one does not expire —
  rotate by editing the env file and restarting `opencode-web.service`, which
  signs every browser session out.
- `hosts.opencode_web` supplies the public base (optional key in the root-owned
  hosts file, added to `ALLOWED_HOST_KEYS`/`HTTPS_URL_KEYS`); `--base-url`
  overrides it.
- Unit: `opencode/opencode-web-token.service` in the diary repo, same
  `PartOf`/`WantedBy=opencode-web.service` pattern as DSH and Kimi.
