# Skolemelding (Skoleplattform Oslo)

Unofficial, read-only pipeline for the Oslo schools' parent portal:
`skolemelding-login` (auth), `skolemelding-sync` / `skolemeldingshow` (the
sjekk track), `skolemelding-api` (raw probe).

Endpoint shapes and the auth chain follow the upstream
[kristoffervp/skolemelding-mcp](https://github.com/kristoffervp/skolemelding-mcp)
(0BSD) — see the `scripts/skolemelding_api.py` docstring.

Unlike Vigilo there is **one account inbox** (no per-child fan-out), the
search rows already carry the full record (no detail fetch), and attachment
paths are stable and Bearer-gated (no expiring SAS URLs). Notification mails
("Ny Skolemelding" from the platform's noreply address) arrive separately on
the **mail** track and are content-free — the real content is here.

## Sync — `skolemelding-sync`

```bash
uv run skolemelding-sync --once            # one-shot fetch (only mode)
uv run skolemelding-sync --once --dry-run  # print records, write nothing
uv run skolemelding-sync --once --force    # ignore the 5-min poll floor
```

Pages the inbox newest-first (`POST /api/messagesearchmobile/`), **dedupes
by `Id`** (the search answers some messages twice), and keeps paging until
every message already in state has been re-seen or `TotalMatching` is
covered — the first run is a full backfill, and later runs still catch an
edit deep in the history.

Change detection is a sha256 signature over the school-controlled fields
only (`Header`, `Body` as text, `From`, `Timestamp`, `Attachments`,
`Threads`). Read-receipts (`IsMessageSeen`/`IsFavourite`) and the feedback
counters (`TotalYes`/`TotalNo`/`TotalNotAnswered` — they move when *other*
parents answer) are excluded, so a poll never re-emits a message nobody at
the school touched. New `Id` → `message`; moved signature →
`message_update`.

Records land in `memory/skolemelding/YYYY-MM-DD.jsonl` (git-tracked), one
`{"kind", "fetched_at", "data"}` per line; `data` is distilled (id, header,
sender, timestamp, plain-text body, threads, attachments as
name + local path + portal source path). Attachments download in the same
run to `memory/skolemelding/attachments/<Id>/<name>` (git-ignored), with a
3-attempt retry budget tracked in state — a message with a missing file is
re-emitted next run instead of being marked seen and losing the file.

State: `memory/skolemelding-state.json` (tracked). The sync owns `messages`,
`attachment_retries`, `last_successful_run` and `auth_required`; the
**triage cursor `cursor` is written only by `skolemeldingshow --bump`** —
the sync never moves what the next triage sees. Auth loss sets
`auth_required`, prints `!! skolemelding auth expired — run
skolemelding-login` and **exits 0**, so a dead login never breaks a sjekk.

## Reader — `skolemeldingshow`

```bash
uv run skolemeldingshow --since-cursor --headers-only   # what the bundle shows
uv run skolemeldingshow --since-cursor                  # bodies + attachment paths
uv run skolemeldingshow --since-cursor --full           # + extracted PDF/docx/text
uv run skolemeldingshow --id 12345678 --full            # one message, whole
uv run skolemeldingshow --all                           # weekly sweep, no cursor
uv run skolemeldingshow --since-cursor --bump           # triaged, advance cursor
```

Header line: timestamp · kind (`U` new / `↩` update) · sender · subject,
`📎n` per attachment, `❓` when the message is a feedback question that
wants an answer. `--full` extracts attachment text from the **local** files
(pypdf / python-docx — no network, works with a dead token). Exit 1 with
only `(cursor: …)` / `(no matching records)` lines is the quiet-track
signal sjekk-collect forgives; `auth_required` in state prints a loud
banner that deliberately breaks that pattern so a dead login surfaces as
FAILED in the bundle.

### Processing new Skolemelding items (SOP)

1. **Collect** — `sjekk-collect` runs `skolemelding-sync --once` in Phase A
   and writes `memory/sjekk/latest/skolemelding.txt` in Phase B.
2. **Triage** — read `skolemelding.txt`. Feedback questions (`❓`) and
   read-confirmations are the actionable kind: answer them **in the
   app/portal** (the pipeline never writes back); deadlines go to
   `CALENDAR.md`, durable facts to `SCHOOL.md`.
3. **Detail when needed** — `uv run skolemeldingshow --since-cursor` for
   bodies + attachment paths, `--full` to also extract attachment content,
   `--id <Id>` for a single message.
4. **Advance the cursor** — `uv run skolemeldingshow --since-cursor --bump`
   once triaged.
5. **Daily note + commit** — actionable items go under `## Skolemelding` in
   today's `memory/YYYY-MM-DD.md` (`MEM:`); commit
   `memory/skolemelding-state.json` and `memory/skolemelding/*.jsonl`
   alongside (generated — don't hand-edit).

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

The PKCE verifier and `state` must survive between printing the URL and
pasting the redirect, so they live in `skolemelding-pending.json` next to the
token file (0600, one-hour TTL) — `--print-url` and `--paste` can be separate
invocations, and a paste with a missing or stale pending file is refused.

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

The privileged broker reads optional `[skolemelding]` settings from root-owned
`/etc/diary/trusted.toml`. Direct interactive/debug runs may instead use the
private `config/local.toml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `token_file` | `~/.config/diary/skolemelding-auth.json` | persisted login (env `$SKOLEMELDING_TOKEN_FILE` wins) |
| `app_version` | `6.0.6` | version string sent to `/token` and `/api/deviceinfo/save` |
| `poll_floor_seconds` | `300` | minimum seconds between `skolemelding-sync` runs (`--force` ignores) |
