# dsh web token → Signal — `dsh-web-notify`

> Index: [scripts/](../README.md)

`dsh_web_notify.py` sends the dsh web UI's login URL (with its per-boot
`?token=`, printed to the journal at every start and impossible to pin) to
the user's Signal note-to-self, so a phone bookmark can be refreshed with
one tap after each restart. It polls the journal scoped to the service's
current `InvocationID` (no `--since` race with tokens printed before the
script is scheduled), rewrites the loopback authority to the public
Caddy/tailnet base (`hosts.dsh_web` in the private config, env override
`DSH_WEB_URL`), and sends via the signal-cli daemon's JSON-RPC socket with
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

Flags: `--unit` (default `dsh-web`), `--scope user|system` (system units after
the agent-uid split, where the journal and `systemctl show` need no `--user`),
`--base-url`, `--label`, `--timeout`, `--socket`.

Because kimi's token is stable, the message is a *liveness* signal rather
than a fresh credential — it confirms the service came back, and carries the
link you would otherwise have to dig out of the journal by hand.

Wire it up with `dsh/kimi-web-token.service` (`PartOf`/`WantedBy=kimi-web`),
exactly like the dsh one.
