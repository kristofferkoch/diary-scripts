# mail_reader webapp

> Index: [scripts/](../README.md)

Lives under `mail_reader/` (not `scripts/`). FastAPI + Jinja2 + HTMX behind
Caddy at `https://server.example.ts.net/mail/`; supervised by the
`mail-reader.service` user unit (`uv run python -m mail_reader.server`).
Design lives in `mail_reader/DESIGN.md`, parking-lot in `mail_reader/IDEAS.md`.

### Authentication

Every route except `/healthz` and static assets requires the persistent token in
`~/.config/diary/mail-reader-token` (`0600`, kek-only). The mail middleware never
creates a missing token; it fails closed with 503. `mail-reader-token.service`
creates the token if needed and sends a login link to Signal note-to-self through
the write-only proxy. The link uses `/mail/login#token=…`: the fragment is read
by browser JavaScript and never appears in HTTP requests or access logs. Login
sets a 30-day `Secure`, `HttpOnly`, `SameSite=Strict` cookie scoped to `/mail`.
API clients can use `Authorization: Bearer <token>` instead.

Resend the link with `systemctl --user start mail-reader-token.service`. Rotate
it with `mail-reader-token-notify --rotate` as kek, then restart
`mail-reader.service`; rotation invalidates existing cookies. Do not put the
token in a query string, branch, or agent prompt.

### End-to-end verification — `verify_browser.py`

After a change to routes, templates, CSS, or the tankekart pipeline, drive
the running webapp through headless Chromium:

```bash
uv run python -m mail_reader.verify_browser --clean
uv run python -m mail_reader.verify_browser --base http://other.host/mail --keep-going
```

Four independent checks (inbox + agenda dismiss, message-view entity chips +
`/e/{id}`, tankekart `chunks → emergent` switching, error-path
status codes) each write numbered screenshots to `/tmp/mr_shots/`. Exits
non-zero if any failed. Uses system `/usr/lib64/chromium-browser/headless_shell`
so playwright doesn't fetch its own browser.

Defaults to the tailnet Caddy URL — hitting `127.0.0.1:8800` directly bypasses
the `/mail/` prefix baked into every `url_for()` link by FastAPI's
`root_path` setting, so clicks 404. Restart the service after deploying new
code (`systemctl --user restart mail-reader.service`) — `uv run python -m
mail_reader.server` doesn't reload templates or Python modules on its own.

The agenda-dismiss probe writes a real `agenda_dismissed` row that persists,
so successive runs see one fewer card unless cleaned with
`DELETE FROM agenda_dismissed WHERE thread_id = '…';`.
