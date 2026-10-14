# Vigilo — `vigilo-sync` / `vigiloshown`

> Index: [scripts/](../README.md)

Read-only pipeline for the Vigilo parent app (SFO/AKS — and school, after
the Skolemelding migration): messages, oppslag/news (ukeplan-PDFs), samtykker
with deadlines and **hentemeldinger** — the standing permission for a child to
leave on their own, which has an expiry date nobody tells you about.
Unofficial, reverse-engineered API — the full spec and its watch-outs live in
`PLAN-VIGILO.md` in the diary repo; upstream repos are named in
`scripts/vigilo_api.py`'s docstring.

`uv run vigilo-sync --once` appends distilled records (never raw — SAS
attachment URLs are kept out of git) to `memory/vigilo/YYYY-MM-DD.jsonl`
and downloads message attachments to `memory/vigilo/attachments/`
(git-ignored in the diary repo). State lives in
`memory/vigilo-state.json`: the sync owns `last_successful_run` and the
seen-maps; the **triage cursor is a separate `cursor` key, advanced only
by `vigiloshown --bump`** — unlike Spond, a sync run never moves what the
next triage sees.

Endpoint quirks learned live (2026-10-11, checked again 2026-10-14):

- The consent-form list (`/api/consent-forms`) answers **404 with an empty
  body** when the child has no forms — that is "empty", not "wrong route"
  (a wrong param name gets a 404 *with* an ASP.NET "No HTTP resource was
  found" body). `vigilo_sync.fetch_consents` cross-checks the app gateway's
  `/api/consent-forms/counts` (verified 200) so a rotted list endpoint
  can't masquerade as "no forms".
- A failing web-kind endpoint (news/consent/check_out_note) no longer aborts
  the run: an API error there is logged and the records/state the other kinds
  produced still land. Inside the hentemelding kind the same isolation runs
  per **child** — one child's bad answer does not cost the other's notes. Auth
  expiry still propagates to the `auth_required` path; transport-level outages
  (DNS/TCP/timeouts) remain fatal to the run.

### Hentemeldinger — the `check_out_note` kind

The page is «Hentemeldinger», the API calls it **check-in notes**
(`/api/check-in-notes`, web gateway). It holds the rule that lets a child
leave AKS alone — invisible in the message threads and the news feed, so the
kind exists purely because nothing else surfaces it.

- Param is `childPersonId` (a third spelling beside `childIds` in the news
  feed and `childId` in consent-forms); an **unknown child answers 200 with an
  empty list**, so an empty result never distinguishes "no notes" from "wrong
  id" — trust the ids from `/api/children/my`.
- `fromDate`/`toDate` is an **overlap** test, not "starts inside": a note
  registered in August comes back for a window starting in November. The sync
  asks for today → +120 days.
- `toDate` is a **hard stop** — from the day after it, AKS releases the child
  only to an adult in person, and the app warns nobody. A consent carries a
  `status` that says *pending*; a hentemelding carries nothing that says *needs
  doing*, so this sync is the only reminder there is. That is why a note inside
  `CHECKOUT_EXPIRY_WARN_DAYS` (21) is **re-emitted on a weekly drumbeat**,
  `utløper om N dager`, rather than mentioned once and scrolled away — and why
  a note already past its date says `utløpt for N dager siden`, never a
  negative countdown (AKS keeps returning the note after it expires).
- A note that stops coming back (expired, deleted, switched off) gets one
  record, `returneres ikke lenger (utløpt eller slettet)` — the permission is
  gone, which is what the whole pickup plan rests on. Only a *successful*
  fetch may say that; an endpoint error must never read as "gone". And only for
  a child the web gateway actually named: since an unknown id answers 200 with
  an empty list, an empty answer from a child `/api/children/my` never listed
  proves nothing, so that announcement is gated on it (with a line to stderr).
- While `web_available: false` sits in `memory/vigilo-state.json`, this kind is
  skipped along with news and consent and the drumbeat goes silent — nothing
  announces the expiry, and nothing announces the expiry date passing. The skip
  line on stderr names the kinds, so a sjekk with no hentemelding track at all
  *is* the warning.
- `dateOccurrences` is the recurrence rule expanded to one entry **per day**
  (93 for a school term) and is dropped before writing: everything in it is
  derivable from `recurrencePattern` + `fromDate`, and the JSONL is
  git-tracked.
- Editing the note (`noteText`, `timeOfDay`, days, span, on/off) re-emits it as
  `endret` — a changed hentetid *is* a changed pickup plan.

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
   durable facts in `SCHOOL.md`. A `check_out_note` header carries the
   hentetid and `til <dato>`; `⚠` means it expires inside three weeks —
   re-new it in the app and update the hentemelding fact in `SCHOOL.md`.
   Replies to AKS are written **in the app by the user** — the pipeline
   never writes back.
3. **Detail when needed** — `uv run vigiloshown --since-cursor` for
   bodies + attachment paths, `--full` to also extract PDF/docx/text
   attachment content (news attachments are fetched over the API at
   show-time). Filters: `--kind
   message|message_update|news|consent|check_out_note`,
   `--child <label>`, `--all` for the weekly sweep.
4. **Advance the cursor** — `uv run vigiloshown --since-cursor --bump`
   once triaged.
5. **Daily note + commit** — actionable items go under `## Vigilo` in
   today's `memory/YYYY-MM-DD.md` (`MEM:`); commit
   `memory/vigilo-state.json` and `memory/vigilo/*.jsonl` alongside
   (generated — don't hand-edit).
