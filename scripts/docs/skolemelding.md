# Skolemelding (Skoleplattform Oslo)

Unofficial, read-only reader for the Oslo schools' parent portal. First slice
only: **auth + an API probe**. A sync/show pipeline (like `vigilo-sync` /
`vigiloshown`) is a later step and does not exist yet.

Endpoint shapes and the auth chain follow the upstream
[kristoffervp/skolemelding-mcp](https://github.com/kristoffervp/skolemelding-mcp)
(0BSD) — see the `scripts/skolemelding_api.py` docstring.

## Auth — `skolemelding-login`

One interactive login, BankID via ID-porten. The OAuth client is the public
parent-app client (PKCE, no secret), and its registered redirect is the custom
scheme `com.cgi.oslomelding.foresatte://oauth` — no browser can follow it, so
the code is captured by pasting the failed-redirect URL back:

```bash
uv run skolemelding-login          # prints a URL, waits for the paste
uv run skolemelding-login --print-url   # two-step variant; finish with --paste
uv run skolemelding-login --paste 'com.cgi.oslomelding.foresatte://oauth?code=…&state=…'
```

Do this from a machine **without** the Skolemelding app installed — otherwise
the OS hands the redirect to the app and it redeems the single-use code.

Chain: ID-porten code → OIDC token → portal `authCode`
(`/api/exchange-app-token`) → app token pair (`/token`, `grant_type=password`
with the literal password `code`) → one `/api/deviceinfo/save`. The login is
then verified with a real refresh + one inbox page (skip with `--no-verify`).

Result is written 0600 to `~/.config/diary/skolemelding-auth.json` (dir 0700,
atomic mkstemp+rename): `{accessToken, refreshToken, userName, deviceId,
expiresAt}` — `expiresAt` in **seconds**. Refresh is pure HTTP
(`grant_type=refresh_token` on the portal `/token`) under a cross-process file
lock (stale lock > 90 s is broken); a refresh answers a new `refreshToken`
only sometimes, so an absent one never wipes the old. HTTP 400/401 on refresh
means the login is dead — every error tells you to re-run `skolemelding-login`.

## Probe — `skolemelding-api` (read-only)

```bash
uv run skolemelding-api list [--page N] [--page-size 15]   # recent headers
uv run skolemelding-api show <Id>                          # full message (JSON)
uv run skolemelding-api attachment /globalassets/attachments/…/x.pdf [-o out]
uv run skolemelding-api probe /api/…                       # raw GET, status+body
```

`list`/`show` go through `POST /api/messagesearchmobile/` (the inbox search;
there is no separate detail endpoint — the search rows already carry the full
`Body` HTML, `Attachments`, `Threads`, …). `attachment` is a Bearer-auth GET
of the portal-relative path; an HTML answer means the token died (login page),
and anything over 10 MB is refused.

## Config

`[skolemelding]` in the private `config/local.toml`, all optional:

| Key | Default | Meaning |
| --- | --- | --- |
| `token_file` | `~/.config/diary/skolemelding-auth.json` | persisted login (env `$SKOLEMELDING_TOKEN_FILE` wins) |
| `app_version` | `6.0.6` | version string sent to `/token` and `/api/deviceinfo/save` |
