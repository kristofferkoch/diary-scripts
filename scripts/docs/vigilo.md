# Vigilo — `vigilo-sync` / `vigiloshown`

> Index: [scripts/](../README.md)

Read-only pipeline for the Vigilo parent app (SFO/AKS — and school, after
the Skolemelding migration): messages, oppslag/news (ukeplan-PDFs) and
samtykker with deadlines. Unofficial, reverse-engineered API — the full
spec and its watch-outs live in `PLAN-VIGILO.md` in the diary repo;
upstream repos are named in `scripts/vigilo_api.py`'s docstring.

`uv run vigilo-sync --once` appends distilled records (never raw — SAS
attachment URLs are kept out of git) to `memory/vigilo/YYYY-MM-DD.jsonl`
and downloads message attachments to `memory/vigilo/attachments/`
(git-ignored in the diary repo). State lives in
`memory/vigilo-state.json`: the sync owns `last_successful_run` and the
seen-maps; the **triage cursor is a separate `cursor` key, advanced only
by `vigiloshown --bump`** — unlike Spond, a sync run never moves what the
next triage sees.

Endpoint quirks learned live (2026-10-11):

- The consent-form list (`/api/consent-forms`) answers **404 with an empty
  body** when the child has no forms — that is "empty", not "wrong route"
  (a wrong param name gets a 404 *with* an ASP.NET "No HTTP resource was
  found" body). `vigilo_sync.fetch_consents` cross-checks the app gateway's
  `/api/consent-forms/counts` (verified 200) so a rotted list endpoint
  can't masquerade as "no forms".
- A failing web-kind endpoint (news/consent) no longer aborts the run: an
  API error there is logged and the records/state the other kinds produced
  still land. Auth expiry still propagates to the `auth_required` path;
  transport-level outages (DNS/TCP/timeouts) remain fatal to the run.

### Auth

- One-time login: `uv run vigilo-login` (BankID in a desktop browser,
  paste the failed `app://…` redirect back — or `--from-curl` with a
  copied authorize request). Tokens land in
  `~/.config/diary/vigilo-tokens.json` (0600) and self-refresh under a
  file lock; refresh tokens rotate on every use.
- OAuth client id/secret come from `pass show vigilo/client-id` /
  `vigilo/client-secret` (or `$VIGILO_CLIENT_ID` / `$VIGILO_CLIENT_SECRET`).
- A dead refresh token is routine, not a crash: sync prints
  `!! vigilo auth expired — run vigilo-login`, sets `auth_required` in
  state and **exits 0** so the sjekk never breaks; `vigiloshown` shows a
  loud banner until re-login. Bad/missing client credentials are the
  opposite case: a loud config error, exit non-zero, state untouched.
- `uv run vigilo-api <path> [params_json]` (add `--web`) is the raw GET
  probe for endpoints not yet mapped.

### Processing new Vigilo items (SOP)

1. **Collect** — `sjekk-collect` runs `vigilo-sync --once` in Phase A and
   writes `memory/sjekk/latest/vigilo.txt` (the step-2 output) in Phase B.
2. **Triage** — read `vigilo.txt` (one header line per record: time ·
   kind · unit · sender · subject, `📎n` for attachments). Consents and
   deadlines are the actionable kind: put them in `CALENDAR.md` and
   durable facts in `SCHOOL.md`. Replies to AKS are written **in the app
   by the user** — the pipeline never writes back.
3. **Detail when needed** — `uv run vigiloshown --since-cursor` for
   bodies + attachment paths, `--full` to also extract PDF/docx/text
   attachment content (news attachments are fetched over the API at
   show-time). Filters: `--kind message|message_update|news|consent`,
   `--child <label>`, `--all` for the weekly sweep.
4. **Advance the cursor** — `uv run vigiloshown --since-cursor --bump`
   once triaged.
5. **Daily note + commit** — actionable items go under `## Vigilo` in
   today's `memory/YYYY-MM-DD.md` (`MEM:`); commit
   `memory/vigilo-state.json` and `memory/vigilo/*.jsonl` alongside
   (generated — don't hand-edit).
