# scripts/

Local helper scripts for the workspace. Python deps live in a **uv project
rooted at `diary-scripts/`** (`pyproject.toml` + `uv.lock`). Scripts are
exposed as console entry points — invoke them with

```bash
# From inside the diary-scripts/ submodule:
uv run <entry-point> …

# From the diary repo root (passes --project so uv finds the right pyproject):
uv run --project diary-scripts <entry-point> …
```

Entry-point names: `mailshow`, `search-mail`, `embed-mail`, `archive-inbox`,
`notmuch-sync-tags`, `spond-sync`, `spondshow`, `finance-ingest`,
`retire-calendar`, `notes`, `shopping`, `kindle-dashboard`. Server modules are
invoked as `uv run python -m mail_reader.server` and
`uv run python -m scripts.kindle_dashboard.serve`.

`uv run` creates/syncs `.venv/` on demand; you never need to activate
anything. Tests: `uv run pytest scripts/` (doctests + `test_embed_mail.py`).

**Config split.** Real infra/PII values (hostnames, paths, home coordinates,
family names, Spond IDs, the important-senders list) live in a private
`config/local.toml` + `config/important_senders.txt` in the diary repo, read
via `mail_reader.config` (`$DIARY_CONFIG` env var or the
`~/.config/diary/config.toml` symlink). The submodule ships only placeholder
defaults. New code must resolve the workspace data root (where `CALENDAR.md`,
`memory/`, topic files live) via `mail_reader.config.workspace_root()` (from
config `paths.project`) — not by walking `__file__`.

> **`scripts/` is a stopgap name.** Once we add scripts that aren't
> mail-related, this whole folder will get split by subsystem (e.g.
> `mail/`, …). Don't write naming around the current layout.

---

## Mail (notmuch + Proton archive)

- **Location:** `~/Mail/Proton` (40 GB, ~205k indexed messages, going back to ~1982 via imported gmail)
- **Index:** `notmuch` 0.40, db at `~/Mail/Proton/.notmuch`, config `~/.notmuch-config`
- **Primary email:** user@example.com
- **Existing tags from Claude Code's digest pass:**
  - `digest::keep` — worth surfacing
  - `digest::list`, `digest::list-protected` — mailing list noise
  - `digest::newsletter` — newsletters
  - `digest::receipt` — order/payment receipts
  - `digest::transactional` — auth links, notifications, etc.
- **`tag:unread` is meaningless** for now — the import marked ~125k messages as unread. Use `date:` filters instead.
- **`tag:inbox` mirrors `folder:INBOX`** — kept in sync automatically by the `post-new` hook (`notmuch-sync-tags`). Same for `tag:spam`, `tag:archive`, `tag:sent`, `tag:draft`, `tag:trash`. The hook also clears stale `unread` from `Sent`. Either query form is fine now.
- **Before 2026-05-15 the `inbox` tag was unreliable** (the bulk import marked ~205 k messages as `inbox` regardless of folder). If you see old digests or notes that say "use `date:` filters because `tag:inbox` is meaningless" — that's no longer true. Backup of pre-reconciliation tag state is in `memory/notmuch-dumps/`.
- **State file:** `memory/mail-state.json` (when I build the digest cron).
- **Digests:** `memory/mail/YYYY-MM-DD.md`.

### Reading mail bodies — `mailshow`

Use `mailshow` — handles the boilerplate (raw fetch, html→text, encoding,
attachment summary). Examples:

```bash
uv run mailshow --limit=5 'tag:inbox and date:today..'
uv run mailshow thread:00000000000349df
uv run mailshow --headers-only --limit=20 'from:gonordic'
uv run mailshow --max-chars=8000 id:<message-id>

# Process-new-mail entrypoint: starts from memory/mail-state.json:last_successful_run.
uv run mailshow --since-cursor --headers-only
uv run mailshow --since-cursor 'from:astrid'   # narrow within the cursor window
```

Don't write fresh inline `python3 -c "..."` blocks for body extraction — extend this
tool instead so improvements stick.

### Processing new mail (SOP)

The cursor `memory/mail-state.json` → `last_successful_run` is the start
point. It's **hand-maintained** and easy to forget — the cursor has
drifted multiple times because earlier passes wrote the daily note but
never bumped the JSON.

0. **Check when mail last synced — FIRST.** Before reading the cursor,
   confirm the local index is current, otherwise you triage a stale
   snapshot and miss mail that's already on the server.
   `systemctl --user show mail-sync.service -p ExecMainExitTimestamp --value`
   (or `systemctl --user status mail-sync.service`) shows the last run;
   the timer fires every 15 min. If the last sync is older than that, or
   the user says mail "just arrived", force one and wait for it to finish
   before continuing: `systemctl --user start mail-sync.service` (blocks
   until `mbsync` + `notmuch new` complete — look for `No new mail` /
   `Processed N files` in the journal). Only then read the cursor. Lesson
   2026-05-29: triaged a booking, then more mail (date-change +
   payment link) landed seconds later — re-syncing first each pass caught
   the full thread instead of a half-state.
1. **Read the cursor** — `uv run mailshow --since-cursor --headers-only`
   (or add a filter: `--since-cursor 'from:astrid'`). The tool reads
   `memory/mail-state.json` itself and prints the cursor it used. Anything
   returned is candidate-new.
2. **Cross-check recent daily notes** (`memory/YYYY-MM-DD.md`) for
   "Mail-status" / "Inbox-gjennomgang" sections that already cover items
   in that window. If they do, the real high-water mark is the latest of
   those, not the JSON cursor.
3. **Triage** with `mailshow` / `search-mail`. Record durable facts
   per CLAUDE.md → "How to add a memory".
4. **Bump `last_successful_run`** in `memory/mail-state.json` to the
   timestamp of the newest message processed (use the message's `Date:`
   header in UTC), and commit it together with the memory updates under
   `MEM:`. Don't skip this step — that's what causes the cursor to lag
   real sweeps.

**Also check sent mail when summarising thread state.** The mail cursor
tracks `tag:inbox` only — user's own replies and forwards never enter the
inbox. A status summary built from cursor + topic-file scans will miss
things user already handled. When the user asks "are we good with X?" or
"what's left on Y?" for an ongoing correspondence, run
`notmuch search 'from:user@example.com and to:<counterparty> and date:<window>..'`
(or `uv run mailshow thread:<id>` for the full conversation) before
answering. Treat both directions as load-bearing. Lesson 2026-05-27 —
declared a VPS-årsoppgave outstanding for User Holding regnskapet that
user had already forwarded to the accountant the day before.

### Live pipeline services

`mail-sync.timer` runs every 15 min: `mbsync -a` → `notmuch new`
(`post-new` hook syncs tags) → `uv run --project diary-scripts embed-mail --tier 1 --quiet`.
Long-running
services: `proton-bridge`, `goimapnotify` (IDLE push), `mail-reader`
(FastAPI at 127.0.0.1:8800 behind Caddy `/mail/`). Wrapper:
`~/.local/bin/mail-sync.sh`. Don't manually re-sync to "see new mail" —
just query notmuch; if something genuinely looks stale, check
`systemctl --user status mail-sync.service` first. No digest cron yet
(planned alongside Signal).

(Per-mail LLM **summaries were retired 2026-08-02** — the chat endpoint
they depended on is gone. The webapp still renders summaries stored
before that date; nothing new is generated, and the `mail_reader.summarize_inbox`
hook + background workers were removed.)

### Semantic search — `search-mail` (pgvector + qwen3-embedding)

Use `search-mail` for fuzzy / cross-language / topical queries —
useful when the exact word probably isn't in the mail (e.g. "byggematerialer"
finds renovation threads even when none contain that word). For exact-text
matching prefer plain `notmuch search`; semantic search is the right tool when
you don't know the keyword.

```bash
# basic
uv run search-mail "examplefund utbetaling 2025"

# filter by tier (see "tier definitions" below), sender, date
uv run search-mail --tier 1 --since 2025-01-01 "fakturaer fra strøm"
uv run search-mail --from astrid "ukeplan"

# exclude a sender (substring, repeatable)
uv run search-mail --not-from exampleconcrete "byggematerialer"

# semantic minus — subtract a concept (default --weight 0.3; 0.7+ breaks query)
uv run search-mail --minus mikrosement "byggematerialer"
uv run search-mail --weight 0.5 --minus dogs "animals"
```

Each hit prints distance, date, sender, subject, `id:<message-id>`, and a
240-char snippet. Pipe an `id:` into `uv run mailshow` for the full body.

**Caveat — snippets can stitch chunks across attachments.** A single mail
with multiple attachments produces chunks from each attachment under the
same `message_id`. Adjacent chunks in the result list look like one quote
but can come from unrelated documents (e.g. the homeowner's own søknad
plus a neighbouring property's søknad attached for context). When an
embedding hit is the *only* source for a durable claim (FDV, `CONTEXT.md`,
biographical fact), open the full attachment text — `uv run mailshow
--attachment-text id:…` — and verify in context before writing it as
fact. Especially risky: multi-attachment mails, neighbouring-property
references, søknader/byggesaker (often sent as bundles). Lesson from
2026-05-26 — embedded snippet attributed Brødrene Bisgaard A/S to
Eksempelveien 3B; the actual byggemelding showed Mesterhus Oslo og
Akershus A/L. Bisgaard was tied to a neighbouring søknad in the same
mail bundle.

**Backend:** Postgres `mailvec` (pg18) + `pgvector` HNSW index, embeddings
from Qwen3-Embedding served by an OpenAI-compatible llama.cpp `llama-server`
(`gpu-host:8081`, `/v1/embeddings`). The served model is natively 4096-dim
and stored at full width since 2026-08-03 (before that, truncated to 1024
dims MRL-style; pre-2026-08-02 this was Ollama `bge-m3`, also 1024d). Quote/signature stripping
via `mail-parser-reply` (`en`/`da`/`sv` — `da` catches Norwegian "skrev");
the regex post-pass also strips `-- ` signature blocks.

Schema lives in `migrations/`:
- `001_pgvector.sql` — `messages` + `chunks` (1024-d vector; widened to
  4096-d full model width by `015_full_width_vectors.sql`, 2026-08-03).
- `002_attachments.sql` — `attachments` (one row per MIME part with a filename
  or `Content-Disposition: attachment`) + `chunks.attachment_id` column.
  Body chunks have `attachment_id IS NULL`; attachment chunks reference the
  attachment.

**Tier definitions** (`messages.tier`, filterable in search):

| tier | query | rationale |
|---:|---|---|
| 1 | `date:1y..` | recent mail — high recall for now |
| 2 | `thread:"{from:me}"` | every thread I sent into |
| 3 | `from:<recipient_addrs>` | senders I've ever replied to |
| 4 | `tag:digest::keep` | mail Claude's digest pass marked worth keeping |
| 5 | `attachment` | every mail with attachments (PDF/ICS/DOCX/…) |
| 6 | `from:*@<NO institution>` | governmental + utilities (skatteetaten, oslo.kommune, altinn, digipost, nav, posten, vy, …) — defined as `TIER6_DOMAINS` in `embed_mail.py` |

Idempotency on `message_id` means tiers overlap freely: a mail in both
tier 1 and tier 4 gets embedded once (under whichever tier ran first) and
skipped by later tiers. `--all` order is `2,1,3,6,5,4` — smallest tier
first so failures surface fast.

### Embedding new mail — `embed-mail` (automated)

`uv run embed-mail --all` (idempotent on `message_id`, resumable). Runs
**automatically every 15 min** via the systemd user unit
`mail-sync.service` (chains `mbsync -a && notmuch new && uv run --frozen
--project diary-scripts embed-mail --all --quiet`). The tool is silent on
success; failures print `!! <mid>: <err>` to stderr and exit nonzero so
`chronic` surfaces them in the journal.

Per message the script extracts:
- **Body**: text/plain preferred, text/html fallback (HTML→text + quote/sig strip).
- **Attachments** (any part with a filename or `Content-Disposition: attachment`):
  - `application/pdf` → `pdftotext -layout - -`
  - `.docx` (OOXML) → stdlib `zipfile` + `word/document.xml` (no extra deps)
  - `.odt` (OpenDocument) → stdlib `zipfile` + `content.xml`
  - `text/html` → existing HTML→text
  - `text/plain`, `text/csv`, `text/markdown`, `text/calendar`,
    `application/ics`, `application/json` → decoded as text
  - Images / `application/octet-stream` / unsupported → metadata-only row
    (`text_chars=0`). A future VLM pass (`embed_images.py`, TBD) will
    walk these and describe with `qwen2.5vl:7b`.

Embedding calls the OpenAI-compatible `/v1/embeddings` endpoint
(`{"model": ..., "input": [...]}`), accumulating up to 32 chunks across
messages per HTTP call, **and** capping each call at `EMBED_BATCH_CHARS`
(6000) total chars: oversized batched requests (32 × ~2000 chars ≈ 16k
tokens) corrupt llama-server's embedding state — subsequent calls return
null vectors / hang until the server idles for several minutes (2026-08-03
incident; slot KV desync in the server log). Null vectors are detected and
retried (batch, then per-text) before failing loudly; a *shorter* vector
fails immediately (wrong model served — never poison the store).
Responses wider than `EMBED_DIMS` (4096) are truncated MRL-style and
L2-renormalized. `chunk_text` strips invisible joiner/format chars
(CGJ/ZWSP/ZWJ/WJ/BOM) — newsletter anti-truncation padding makes the model
emit NaN.

**Queries need qwen3's instruction prefix.** Without
`Instruct: …\nQuery: …` on the query side, junk micro-chunks (PDF
extraction artifacts) outrank genuinely relevant content; with it the
ranking inverts (verified 2026-08-03). `search_mail` uses
`scripts.embed_mail.embed_query` (documents are indexed plain, and
chunk-vs-chunk similarity in `related.py` stays plain too).

A full chunks/attachments rebuild (e.g. after an embedding-model switch)
uses `--reembed`, which walks `messages` rows that have no chunks/
attachments and inserts only those tables:

```bash
psql -d mailvec -c "DROP INDEX chunks_embed_hnsw; TRUNCATE chunks, attachments;"
uv run embed-mail --reembed            # resumable; re-run if interrupted
# 4096-d storage exceeds the HNSW dim caps (2000 vector / 4000 halfvec) —
# index an MRL-truncated 4000-d prefix at half precision instead:
psql -d mailvec -c "CREATE INDEX chunks_embed_hnsw ON chunks USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops);"
```

Re-run manually after a big mail import. After a large batch, drop+rebuild
the HNSW index for best recall (same SQL as above).

Env vars (defaults usually fine, real values from `config/local.toml` via
`mail_reader.config`): `PG_DSN=dbname=mailvec`,
`EMBED_URL=http://gpu-host:8081` (config `hosts.embed`), `EMBED_MODEL=qwen3-embedding`,
`EMBED_DIMS=4096`, `EMBED_BATCH=32`, `EMBED_BATCH_CHARS=6000`,
`EMBED_QUERY_INSTRUCTION` (retrieval prefix; `ME_ADDRS=user@example.com`). Do **not** try to
install `talon` — won't build on Python 3.14 (`cchardet` needs
`longintrepr.h`, removed in 3.12+).

### Typechecking + tests

`uv run pytest scripts/` runs doctests + unit tests + integration tests
against the local mail store (skipped if absent). `uv run ty check scripts/`
runs Astral's `ty` typechecker. Both should pass clean before commit.

### Auto-archiving the inbox — `archive-inbox`

For "archive after N days" workflows, use `archive-inbox`. Rules
live in `scripts/archive_inbox_rules.json`. **Not currently scheduled** — run
manually (`uv run archive-inbox`) or wire up a user timer if you
want it daily; only `mail-sync.timer` is installed today.

### notmuch post-new hook

`notmuch new` runs `~/Mail/Proton/.notmuch/hooks/post-new` after indexing,
which calls `uv run --project diary-scripts notmuch-sync-tags --apply --quiet`.
Without this, folder moves on the Proton side (e.g. archiving a mail) don't
update `tag:inbox` and the mail-reader inbox view goes stale.

The hooks dir lives **inside** `.notmuch/`, which isn't git-tracked. If
the xapian DB is ever rebuilt the hook vanishes — reinstall with:

```bash
scripts/install_notmuch_hooks
```

Canonical copy lives at `scripts/notmuch-post-new`.

### Maildir gotchas (mbsync + manual moves)

If writing custom maildir moves: **always strip the `,U=<n>` suffix** from the
filename when moving across folders. That suffix is mbsync's IMAP-UID tracker
and is scoped per folder — leaving it in place causes
`Maildir error: duplicate UID N in /<folder>`, which **aborts the entire
mbsync run**. Other related gotchas:

- `notmuch search --output=files` returns one path per maildir copy of the
  message (Proton's `All Mail/` mirrors every message). Filter to
  `/INBOX/` (or whichever folder) before acting.
- `find -mmin -N` won't catch just-moved files — `mv` preserves mtime. Find
  by path/name pattern instead.
- After moving files, `notmuch new` detects them as renames (content hash),
  no re-index needed. The `post-new` hook then reconciles tags.
- Lesson learned 2026-05-15 archiving the Filter newsletters.

### Extracting attachments — `mailshow --attachment-text` / `--attachments`

`mailshow` handles attachments via two independent flags (reuses
`embed_mail.iter_attachments`, so PDF/DOCX/ODT/text extraction matches what
the embedder sees):

```bash
# inline extracted text (PDF/DOCX/ODT/text) after the body — combine freely
# with --headers-only when the body is junk and you only want the PDF
uv run mailshow --headers-only --attachment-text id:<message-id>

# save raw bytes to disk (filenames sanitised; collisions get .1, .2 …)
uv run mailshow --attachments=/tmp/out id:<message-id>
```

`--max-chars` truncates each attachment's extracted text the same way it
truncates the body. Binary / unsupported MIME types print a marker line
instead of text. `mpack` (`munpack`) is **not** required — and not in
Fedora 44 anyway.

Always pin to the exact `id:` (not a thread or search) to avoid concatenated
streams. Lesson from 2026-04-27 (`MEMORY.md`).

### Ingest cadence

- **Cron-driven**, not heartbeat. Schedule TBD once Signal is wired up.
- Mail is seldom urgent — default to digesting, not pinging.
- Only interrupt for genuinely time-sensitive things (school/kindergarten,
  doctors, real bank fraud, calendar conflicts).
- Don't translate between Norwegian and English unnecessarily; never mix them
  mid-sentence.

---

## Spond — `spond-sync` / `spondshow`

Evaluation phase, no timer yet. `uv run spond-sync --once` pulls
chats + events + posts via the Olen/Spond library and appends raw JSONL
to `memory/spond/YYYY-MM-DD.jsonl`. State cursor lives at
`memory/spond-state.json` (auto-bumped by the tool — unlike the mail
cursor). Use `uv run spondshow --since-cursor [--headers-only]` to
triage new items; `--kind event --future` filters to upcoming events.
Run **manually** for now; we'll add a systemd timer once we trust it.

Upstream library does not expose full per-chat message history nor Spond
Pay; the pipeline captures "chat X has new activity" signals + event /
post bodies, while payment receipts come in over mail.

### Auth

- `SPOND_USERNAME` is exported from user's shell environment — call
  `spond-sync` / `spondshow` bare under `uv run`, **no inline
  `SPOND_USERNAME=…` prefix** (a guessed override silently routes to the
  wrong account).
- Password: `pass show spond/user` (override with `$SPOND_PASSWORD_CMD`).
- 2FA must be **off** on the Spond account — Olen/Spond doesn't implement
  the OTP-verify flow
  ([issue #205](https://github.com/Olen/Spond/issues/205)).
- Set `$SPOND_RSVP_MEMBER_ID` to surface accept/decline/unanswered
  markers in event headers; Robin' member-id in Eksempel-IL G-lag is
  `0123456789ABCDEF0123456789ABCDEF`.

### Processing new Spond items (SOP)

Unlike the mail cursor, `memory/spond-state.json` is auto-bumped —
running `spond-sync --once` updates `last_successful_run` and the
per-chat / per-event / per-post seen-sets on every successful run. The
cursor reflects "the last time `spond-sync` ran cleanly", not "the
last time we wrote a digest".

1. **Fetch new activity** — `uv run spond-sync --once`.
   Prints a one-line `new records: total=N, by_kind={...}` summary on
   stderr; appends new records to `memory/spond/YYYY-MM-DD.jsonl`.
   Idempotent — re-running with no new activity is a no-op.
2. **Triage new records** — `uv run spondshow --since-cursor
   --headers-only` for a one-line summary per record; drop
   `--headers-only` to see full JSON bodies. Filter with `--kind
   chat|event|post` or pin to `--chat <id>` / `--event <id>`.
   Events are tracked by a **content key** (`event_activity_key`), so an
   RSVP change, reschedule, or cancellation on an already-seen event
   re-emits and shows up here — set `$SPOND_RSVP_MEMBER_ID` (exported in
   user's shell) so the key folds in the tracked member's own response.
   If it's unset, `spond-sync` prints a `!!` warning and only
   reschedule/cancellation are caught, not RSVP. (Before 2026-05-29
   events used a flat id-only seen-set that swallowed RSVP changes
   entirely — see `test_spond_sync.py` for the regression.)
2b. **Belt-and-suspenders: list upcoming events each pass** — `uv run
   spondshow --kind event --future --headers-only` and
   reconcile every `[?]`/`[✓]`/`[✗]` RSVP marker against `CALENDAR.md`. A
   marker that disagrees with the calendar (or a calendar line still
   saying "RSVP: svar?" for an event user has since answered) is the
   signal to update. Catches anything the cursor logic misses.
3. **Distil into the daily note** — for *actionable* items
   (kampendringer, oppmøtefrister, betalingskrav, foreldredugnad), add a
   `## Spond` section to today's `memory/YYYY-MM-DD.md`. Posts from the
   klubb-feed are deliberately low-signal — capture in JSONL, only
   surface to the daily note if there's a real follow-up.
4. **Commit** — daily-note updates under `MEM:`; pipeline changes under
   `TOOLS:`. The state file (`memory/spond-state.json`) and the JSONL
   files (`memory/spond/*.jsonl`) are generated — commit them alongside,
   but don't hand-edit.

---

## Signal messages — `signal-capture` + `signalshow`

Part of the **sjekk-flow**. Reads the linked Signal device's traffic in a
structured way, replacing the old `journalctl -u signal-mirror | grep`
sweep (the journal is the daemon's *human* stdout — variable lines per
message, sender/body split across lines, bounded by journald retention; it
lost a whole conversation on 2026-06-07).

**Architecture** (mirrors the Spond sink→JSONL→cursor→`*show` shape):

- **`signal-capture`** — an always-on `systemd --user` service
  (`signal-capture.service`) that connects to the `signal-mirror` daemon's
  JSON-RPC socket (`$XDG_RUNTIME_DIR/signal-cli/socket`) as a client and
  appends one normalised record per *conversation* message (incoming +
  the user's own sent, mirrored via `syncMessage`) to
  `memory/signal/YYYY-MM-DD.jsonl`. Typing/receipt/reaction-only/empty
  envelopes are dropped; attachments are recorded as **metadata only**
  (no blobs). The full envelope is kept under `raw` so nothing is lost.
  - Connecting to the *existing* daemon's socket is the sanctioned IPC —
    it is **not** a second receiver and does not ACK independently, so it
    does not violate the "never run a second `signal-cli receive`" rule.
  - **Push model:** a client only sees messages that arrive while
    connected (signal-cli does not replay history). So the service must run
    continuously (`Restart=always`); messages received while it is down
    survive only in the journal. To backfill a gap, read the journal for
    that window.
  - **Privacy:** `memory/signal/` is git-ignored — conversation content
    stays local beside the repo, like the maildir. Only the cursor
    (`memory/signal-state.json`, a timestamp) is committed.

- **`signalshow`** — the reader. Cursor is `memory/signal-state.json`'s
  `cursor`, **hand-advanced like the mail cursor** (not auto-bumped like
  spond): `signalshow` is the only writer of the state file; `signal-capture`
  never touches it.

**SOP (per sjekk):**

1. `uv run signalshow --since-cursor --headers-only` — one line per new
   message (`ISO  ←/→  peer  sender: text`). Filter with `--from <name>`,
   `--with <peer>` (whole thread incl. your replies), `--group`/`--no-group`.
2. Triage substantive messages as first-class sjekk input (same weight as
   mail/Spond) — surface anything actionable, file durable facts to the
   right topic file. The threshold is fuzzy; the line is roughly **"would
   the user want this remembered or acted on later?"** Above the line, and
   worth capturing:
   - **Scheduling / logistics** — dates, times, plans, changes, who's
     where when.
   - **Gift signals** — anyone expressing they want / wish for a thing
     (e.g. a partner mentioning something they'd like), or a question that
     implies gift-planning. Worth a durable note even when phrased
     casually.
   - **Explicit "remember to" / "must do" items** — anything framed as a
     todo, deadline, or thing-not-to-forget.

   **Note to Self is a first-class capture channel.** The user's own
   Signal *Note to Self* messages (peer == self) are deliberate captured
   reminders — treat them exactly like a `/notes/` queue item: act on each
   one, then file/route it. The user finds Note to Self easier than the
   custom notes page, so expect real reminders to arrive here; never skip
   a self-note as "noise."

   **Do NOT mention insignificant messages at all** — real-time
   coordination ("kom hit litt", "ja"), greetings, acknowledgements,
   group-chat thanks/reactions and the like are noise: leave them out of
   the sjekk summary *and* the daily note entirely. Read them only to
   confirm there's nothing actionable, then move on. Surface a Signal
   message only when it carries a fact worth keeping or an action to take.
3. `uv run signalshow --since-cursor --bump` (or just `--bump`) to advance
   the cursor to the newest message shown. Commit `memory/signal-state.json`
   under `MEM:` — the JSONL itself is git-ignored.

Tests: `uv run pytest scripts/test_signal_capture.py scripts/test_signalshow.py`
plus doctests (`uv run python -m doctest scripts/signal_capture.py
scripts/signalshow.py`). Service health: `systemctl --user status
signal-capture`. Setup/rebuild of the underlying mirror:
[../../network/docs/signal-cli-mirror.md](../../network/docs/signal-cli-mirror.md).

---

## Notes queue — `notes.py`

Part of the daily **sjekk-flow**. The `/notes/` page on server
(`https://server.example.ts.net/mail/notes/`, reachable from the
"Notater" topbar link) is a capture inbox: the user types free-text
reminders — "things I don't want to forget" — into a textarea and they
land in the `notes_queue` Postgres table (migration
`migrations/011_notes_queue.sql`). The page lists pending notes
newest-first with inline **edit** and **delete**. The webapp side is
`mail_reader/notes.py` (CRUD) + routes in `mail_reader/server.py`.

**Photo notes.** The capture form also takes a picture (`<input
type=file accept=image/* capture>` — on a phone it offers "take photo"
directly). The image is processed by `mail_reader/note_images.py` (Pillow:
EXIF-orient → downscale to ≤1600px → re-encode JPEG, preserving GPS/EXIF
as a signal — see "GPS as a signal" in `mail_reader/IDEAS.md`)
and stored as web + thumbnail BYTEA in the `note_attachments` table
(migration `migrations/013_note_attachments.sql`). A note may carry a
photo, text, or both — an image-only note (blank text) is valid. The list
renders the thumbnail; tapping opens the full image. If the photo's EXIF
carries a location it's parsed at upload time into the `gps_lat`/`gps_lon`
columns (migration `migrations/014_note_attachment_gps.sql`): the page
shows a "📍 sted" chip linking to an OpenStreetMap pin and the CLI listing
prints the coordinates + map URL. Location is optional — screenshots /
location-off / share-sheet-stripped photos just leave both columns NULL.
The table also has
`description` / `description_model` / `described_at` columns reserved for
a future vision-model pass (e.g. qwen2.5vl) to describe each photo (the
"image attachment summaries" item in `mail_reader/IDEAS.md`).

The check round **digests** the queue:

```bash
uv run notes            # list pending notes (default)
uv run notes list --all # include already-done notes
uv run notes add "text" # add a note from the terminal
uv run notes done 42    # mark note 42 handled — drops off the page
uv run notes rm 42      # hard-delete note 42
uv run notes image 7    # dump attachment #7's image to a file to view
```

SOP: run `uv run notes` with no args. For each pending note, do the real
work it implies — file a `CALENDAR.md` entry, update a topic file, add a
daily-note section, open a Spond reply, etc. — then `done <id>` it so it
stops showing on the page but stays in the table as a record of what was
captured. A note flagged `📎 bilde (vedlegg #N)` carries a photo: until a
description exists, `uv run notes image N <file>` writes the image out so
you can open/read it before acting. Use `rm` only for genuine junk (test
rows, duplicates). If a note is ambiguous, surface it to the user rather
than guessing. Notes are free-text from a phone on the go: expect terse,
lower-case, half-sentences.

Tests: `uv run pytest mail_reader/test_notes.py mail_reader/test_note_images.py`
(the notes CRUD tests skip if the mailvec DB is unreachable; the image
tests are DB-free).

---

## Shopping list — `shopping.py`

Part of the **sjekk-flow**. The `/shopping/` page on server
(`https://server.example.ts.net/mail/shopping/`, "Handle" in the
topbar) is a standing, categorised checklist the user works through in the
store. Items live in the `shopping_items` Postgres table (migration
`migrations/012_shopping_list.sql`); the webapp side is
`mail_reader/shopping.py` (CRUD) + routes in `mail_reader/server.py` +
`templates/shopping*.html`.

**Categories** are a fixed, ordered list defined in `mail_reader/shopping.py`
(`CATEGORIES`): Frukt & grønt · Kjøl & meieri · Tørrvare & pålegg · Frys ·
Husholdning · Annet · **Netthandel** (always last, so the in-store
categories come first and online-order items sit at the bottom). The list
is **not** a DB CHECK constraint — reorder/extend it in Python without a
migration. The module validates category on every write.

**Checkbox lifecycle.** Ticking an item sets `checked` (it greys out and
stays in place on the page); checks **persist and are reversible** on the
web. They are **not** auto-removed — the **sjekk** garbage-collects bought
items:

```bash
uv run shopping                 # list, grouped by category
uv run shopping add "Bananer" --cat frukt   # category by prefix
uv run shopping check 42        # tick (bought); uncheck to undo
uv run shopping mv 42 frys      # move to another category
uv run shopping rename 42 "..."  # rename
uv run shopping rm 42           # hard-delete
uv run shopping uncheck-all     # clear all checks (fresh trip)
uv run shopping sweep           # delete checked items — THE SJEKK STEP
```

SOP each sjekk: run `uv run shopping sweep` to remove what the
user ticked off since the last pass (it prints what it removed). A notes-
queue item that's really a purchase ("kjøp gråblyanter") → `uv run shopping add`
it, then `done` the note. `--cat` accepts a case-insensitive prefix of any
canonical category (`frys`, `kjøl`, `nett`).

Tests: `uv run pytest mail_reader/test_shopping.py` (skips if the mailvec DB
is unreachable).

---

## Finance — `finance_ingest.py`

Summarise a Bulder Bank CSV export and cross-reference it with the embedded
mail archive.

Bulder is mobile-only; there's no API. The flow is: export from the iOS app
share-sheet → mail to `user@example.com` → ingest. The CSV is named
`eksporterte_transaksjoner.csv`, semicolon-separated with Norwegian comma
decimals.

```bash
# From a CSV on disk
uv run finance-ingest /tmp/bulder/eksporterte_transaksjoner.csv

# Auto-extract latest "Bulder bank eksport" mail and summarise
uv run finance-ingest --from-mail

# Same, plus cross-reference vs embedded mail
PG_DSN=dbname=mailvec uv run finance-ingest --from-mail --enrich
```

Output (markdown tables on stdout): per-month inn/ut, per-account outflows,
top-25 merchants by `Tekst`, recurring (≥2 months), large one-offs (≥20k NOK).

**Bulder's `Hovedkategori` / `Underkategori` columns are mostly garbage** — the
summary leans on `Tekst` (merchant) and `Dato` instead. Don't trust the auto-
categorization.

**`--enrich`:** for each "interesting" transaction (large / unlabelled /
Ukategorisert), finds matching mail by (a) amount-match against extracted
money entities in postgres, (b) notmuch search on date±3d + merchant keyword,
and lists the matches per transaction.

The matches displayed in the output are deliberately broad (any mail in the
date window with a merchant-keyword hit) — the value isn't precise correlation,
it's that the bank ledger marks those mails as worth a closer look.

Cadence: monthly, see `CALENDAR.md`. Always reuse the existing pipeline:
`mail_reader.db.connect()`, `scripts.embed_mail.embed_batch` — don't
reimplement DSN / embedding.

---

## Calendar — `retire_calendar.py`

Part of the daily **sjekk-flow** (mail + spond + retire). The user's
daily "sjekk" (typically phrased "les / sjekk mail og spond") includes
retiring expired one-off events from `CALENDAR.md` — without this step,
the calendar slowly fills with past entries and the top of the file stops
being a useful "what's next" view.

### Mechanical step — use the tool

```bash
uv run retire-calendar --dry-run            # preview
uv run retire-calendar                      # apply, today = date.today()
uv run retire-calendar --today 2026-06-01   # simulate a later date
```

Cuts every event line whose end-date is strictly before today's
`currentDate` out of `## One-off events by month` in `CALENDAR.md`,
inserts it into the matching `### <Month> <Year>` subheading in
`CALENDAR-PAST.md` (creating the subheading in chronological order if
missing), and drops any month subheading that ends up empty. Idempotent
— re-running with nothing to do leaves both files byte-identical.
Recurring weekly/monthly/quarterly sections are never touched.

### Manual step — enrichment

After running the script, look through the lines it moved (printed on
stderr). For any event the daily-note sweep gave new context to — an
action's outcome, who showed up, what was decided — open
`CALENDAR-PAST.md` and **append a short tail** to that moved line
referencing `[[memory/YYYY-MM-DD.md]]`. Past entries become more useful
as snapshots when they point back at the day's notes. This part is a
judgment call and stays manual.

Today's events (with end-date == today) stay in `CALENDAR.md` until the
day is over. Format / parser-contract is identical in both files; see
[../CALENDAR-RULES.md](../CALENDAR-RULES.md).

### Kindle refresh — don't poke it, let the device pull

**Do not try to refresh the wall Kindle at the end of a sjekk.** The
device sleeps, so the poke always fails — just commit and let it pull.

The wall Kindle renders the [kindle_dashboard](#kindle_dashboard--wall-display-png-generator)
PNG, whose calendar and spond blocks are scraped live from `CALENDAR.md`
and `memory/spond/*.jsonl`. The dashboard *generator* picks up edits on
its next request automatically — but the **device re-fetches only on its
own scheduled wake**.

**As of 2026-05-29 the device suspends to RAM between refreshes** (battery
fix — see [KINDLE.md](../KINDLE.md)). While suspended its WiFi is off, so
**`ssh kindle 'initctl restart dashboard'` reliably fails with "No route to
host" — the poke can't reach a sleeping device.** Don't run it. Two
things follow for the sjekk:

- **Just commit and let it poll.** A change to the today..+3 agenda
  appears on the next scheduled wake: **≤15 min** off-peak, **≤5 min**
  during the peak windows (06:00–09:00, 15:00–20:00). For a sjekk that's
  always fine — **no action needed, and no poke to attempt.**
- **Need it on the wall *now*?** That's a **human** job: press the
  Kindle's **power button** (a short press) — `snvs-powerkey` wakes it
  from suspend and the loop re-fetches + re-renders within seconds. That's
  the only reliable on-demand refresh, and it's the user's call — not an
  agent step. Deploying device changes from server has the same problem —
  see the "deploy on next wake" catcher pattern used on 2026-05-29.

**Only bother at all when the change is actually visible on the wall.** The agenda
block (`kindle_dashboard/data.py` → `calendar_block`, `days_ahead=3`)
renders **today + the next 3 days** — so refresh only when an
added/retired/edited CALENDAR.md line (or a Spond RSVP change) falls
within `today..today+3`. A change to an event further out is invisible on
the agenda and **does not warrant a poke**. (The month-grid busy-dots
cover the whole current month, but they're low-signal — don't refresh
just for a dot.) Skip the poke entirely for mail-only triage, finance,
or any pass that touched nothing the dashboard renders.

Lesson 2026-05-29: poked the Kindle mid-sjekk *before* adding the next
day's training to CALENDAR.md, so the wall display stayed stale until
re-poked. Same day, 2nd pass: poked after adding only far-future lines
(cruise 21.07, sommeravslutning 09.06) — unnecessary, nothing in the
today..+3 window changed.

**Pushing to the device is now largely obsolete — the device pulls.**
Earlier plans here were to have a git/file hook poke the Kindle on
`CALENDAR.md` / `memory/spond/*.jsonl` change (the watcher's
`_poke_kindle()` pattern). Suspend kills that: you can't push to a
sleeping device. The device's own ≤15 min / ≤5 min poll is the mechanism
now, and the power button covers the rare "show it this instant" case.
The only push still worth wiring would be a wake-capable channel (BLE,
WoWLAN) — not worth it for a wall calendar. Leave the LLM out of it:
commit the change and let the device poll.

### Script behavior reference

- Operates only inside `## One-off events by month`. Recurring weekly /
  monthly / quarterly sections are skipped.
- Decision is on **end-date** for date spans (so `2026-06-29 – 2026-07-03`
  retires under July when 2026-07-04+ arrives).
- Drops `### Month Year` subheadings that end up empty in `CALENDAR.md`.
- New month sections in `CALENDAR-PAST.md` are inserted chronologically.
- Idempotent: when nothing is expired, both files are left byte-identical
  (no spurious whitespace churn).
- The `[[memory/YYYY-MM-DD.md]]`-tail enrichments on moved lines are a
  manual judgment call; the script intentionally does not touch the
  line body.

Tests: `uv run pytest scripts/test_retire_calendar.py` (run from inside `diary-scripts/`).

---

## Per-mail PR composer — `pr_compose.py`

Auto-opens one GitHub PR per "significant" incoming mail, so durable facts
flow from inbox → memory archive without manual transcription. State via
notmuch tags (`pr::triaged`, `pr::significant`, `pr::skip`, `pr::filed`).
Two phases per run:

1. **Classify** new mail (notmuch query or `--since-cursor`). Triage filter
   (skip noise tags + GitHub-notification senders, see "Loop-break filter"
   below) → MLX classifier → tag the thread.
2. **File PRs** for `pr::significant` threads not yet `pr::filed`. Calls the
   writer model, drafts a memory section, opens a worktree, commits as the
   bot, pushes via embedded-PAT URL, opens PR via `gh pr create`.

```bash
# Just see what would happen on recent mail (no tags written, no PRs):
uv run python -m scripts.pr_compose --since-cursor

# Classify + tag, but don't open PRs:
uv run python -m scripts.pr_compose --apply --since-cursor

# Full pipeline — classify, tag, file PRs:
uv run python -m scripts.pr_compose --apply --file-prs --since-cursor

# File PRs only (skip classify), capped at 2:
uv run python -m scripts.pr_compose --file-prs --apply --limit-prs 2 'id:none'
```

### Model server

`mlx_lm.server` on **`gpu-host:8080`** (OpenAI-compatible, runs on the
Mac Studio M3 Ultra). Models selected per request via the `model` field;
**switching is expensive** (~30+ s cold load), so the pipeline batches all
classifier calls before any writer calls. Env vars: `MLX_BASE`,
`PR_COMPOSE_CLASSIFIER_MODEL`, `PR_COMPOSE_WRITER_MODEL`.

### Writer-tier — pivoting from Qwen3.6 to NuExtract (2026-05-28)

**Original plan**: dense writer (Qwen3.6-35B-A3B at 6-bit) drafts a memory
section + calendar candidates per significant mail, calling a
`get_calendar_events` tool via OpenAI tool-use protocol.

**What blocked it**: Qwen3 thinking-mode + tool-calling is a confirmed
upstream bug —
[Qwen3 #1817](https://github.com/QwenLM/Qwen3/issues/1817) (~60 % failure
rate on tool-call emission) and
[vllm #18819](https://github.com/vllm-project/vllm/issues/18819) (thinking-off
+ guided JSON breaks output). The specific failure mode on our prompts
correlates with **content thinness**: rich source mails (multi-date
construction plan) sometimes work; short mails (5-line invitation) always
loop. The model fixates on phantom inconsistencies in its own `<think>`
block and never terminates. Tested-and-failed mitigations: max_tokens up to
32 k (Qwen's recommended budget); Qwen-official sampling
(`presence_penalty=1.5`, `top_p=0.95`, `temperature=1.0`); JSON vs
lenient line-format output; trimmed vs richer system prompts; sandwich
prompting (instructions before AND after the input); typographic
canonicalization of source mail. Adding more context made it
*worse* — the model invents constraints from the additional vocabulary.

**Current pivot**: [NuMind NuExtract-2.0-8B](https://huggingface.co/numind/NuExtract-2.0-8B)
— purpose-built schema-guided extraction, QwenVL-based, multilingual
(Norwegian OK), no thinking mode, designed for "fill this JSON schema from
this document". NuMind claims it beats GPT-4.1 by 9 F1 on extraction with
very low hallucination ([blog](https://numind.ai/blog/outclassing-frontier-llms----nuextract-2-0-takes-the-lead-in-information-extraction)).
In evaluation as of 2026-05-28.

**Planned architecture**:

1. **Classifier** — Qwen3.6-35B-A3B-4bit-DWQ (already deployed, fine at yes/no
   without thinking)
2. **Extractor** — NuExtract-2.0-8B emits `{title, branch, heading, body,
   calendar_candidates}` against a fixed JSON schema
3. **Calendar dedup** — Python-side `_verify_candidates()` (deterministic,
   already implemented, stays as-is)
4. *(Optional)* small instruct model for body fluency if NuExtract's prose
   feels too templated

### Diagnostic harness — `mlx_tool_probe.py`

Standalone harness for evaluating a candidate model + chat-template config
against the OpenAI tool-use protocol. Built during the Qwen3.6 experiments;
useful for any future model evaluation. Scenarios: `calendar` (simple
single-tool agent loop), `writer` (full WRITER_SYSTEM with one mail thread),
`writer_lenient` (line-format output experiment). Always read the model card
on HuggingFace BEFORE picking sampling params — the Qwen3.6 32 k
recommended `max_tokens` was buried in "Best Practices" and we missed it
for half a day.

```bash
# Sanity-check tool-calling on a new model:
uv run python -m scripts.mlx_tool_probe --model <model-id>

# Real writer scenario on a notmuch thread:
uv run python -m scripts.mlx_tool_probe --model <id> --scenario writer_lenient \
    --mail-thread <thread-id-without-prefix> --max-tokens 4000
```

### GitHub bot

Bot account `exampleuser-bot` is a collaborator on `exampleuser/diary`
with Write. Classic PAT (`repo` scope only, 90 d, created 2026-05-27) at
`pass show github/mailbot-pat`. Rotation reminder at 2026-08-25 in
CALENDAR.md. Bot mail goes to `bot@example.com` and Proton filters
it to folder `Botmail` — user's own inbox never sees PR-creation notifications.

Branch protection on `master` is wanted but blocked by GitHub's paywall
($4/mo Pro). Defenses are layered: triage filter on PR-creation
notifications + bot-mail folder routing. Off-premise forge migration
(Codeberg / GitLab.com / self-hosted Forgejo) is the longer-term answer.

### Postgres read-only role

`mailvec_ro` (NOLOGIN base) + `llm_pr_composer` (LOGIN, password at
`pass show pg/llm_pr_composer`) provisioned via
`migrations/009_readonly_llm.sql`. `statement_timeout=15s`,
`idle_in_transaction=30s`. Same `mailvec_ro` base backs all future
LLM-driven SQL consumers — give each its own LOGIN user inheriting from
`mailvec_ro`.

### Loop-break filter — CRITICAL

Opening a PR generates GitHub notification mail. Without filtering, the
next `mail-sync.timer` run sees it, the classifier may flag it as
significant, and the pipeline opens PRs about its own PRs. The triage
layer (`SKIP_SENDER_SUBSTRINGS` in `pr_compose.py`) filters
`notifications@github.com` and `noreply@github.com` BEFORE invoking the
model — the loop breaks regardless of how the model decides.
Belt-and-suspenders with the bot-mail folder routing.

### Future: relative-date resolution via tool calling

Real example we hit on 2026-05-28: Sommerskolen sent a reminder
saying "én uke før kursstart kan du logge inn på våre nettsider og lese
velkomstbrev". The course start is `2026-06-29` (already in
`CALENDAR.md`), so the implied date is `2026-06-22` — but **the
extractor only sees the mail body**, not `CALENDAR.md`, and cannot
resolve "kursstart" to a concrete date. Tier 3 has the same blindspot.

The unlock is a model that can reliably tool-call against a calendar-
lookup tool (the Python-side `_tool_get_calendar_events` we already
have is the right shape for this). The agent loop: extractor produces
calendar candidates + unresolved relative references; a resolution
tier with tool access looks up the anchor events, does the arithmetic,
and appends concrete dates. We attempted this with Qwen3.6 and hit the
upstream tool-calling reliability cliff (thinking-on loops, thinking-
off skips tools — Qwen3 #1817, vllm #18819).

The monthly model-check (`CALENDAR.md` → Recurring månedlig → "Sjekk
pipeline-modeller + server-software") is the explicit gate for re-
attempting this feature. Don't pick it up in isolation; the chance of
a usable candidate is highest right after a model that scores well on
the tool-calling-judgment benchmark below appears.

Worth tracking because this class of dates ("X dager før Y", "uken
etter Z", "før sommerferien") is common in school mail, contractor
mail, and event invites. Catching them automatically would turn the
calendar from "what the mail literally mentions" into "what the mail
implies given the existing schedule".

**Where to actually look for candidates** (curated 2026-05-28; the
landscape moves — review during the monthly check):

- [`huggingface.co/mlx-community`](https://huggingface.co/mlx-community)
  — anything that lands here can run on `gpu-host` immediately. Sort
  by "Recently Created", or subscribe via
  [`zernel/huggingface-trending-feed`](https://github.com/zernel/huggingface-trending-feed)
  RSS.
- [`huggingface.co/numind`](https://huggingface.co/numind) — for
  future NuExtract versions (4? 5?). Same chat-template-kwargs protocol
  as 2.0/3, already validated for our tier 2.
- [`huggingface.co/Qwen`](https://huggingface.co/Qwen) — for future
  Qwen versions. Qwen 3.7 or 4 with the tool-calling bug fixed is the
  most likely unlock for tier 2.5. Check Qwen3 issue #1817 for fix
  status before assuming a new release helps.
- [`lintware/tool-calling-benchmark`](https://github.com/lintware/tool-calling-benchmark)
  — fork of MikeVeerman's benchmark with **MLX + llama.cpp backends on
  Apple Silicon**. Measures *judgment* (when to call), not just
  *execution* (whether the JSON parses) — exactly the metric tier 2.5
  needs. Run this against any candidate before wiring it in. Surprise
  finding from earlier rounds: `qwen3:1.7b` topped the benchmark at
  0.960 Agent Score — small models can do this well.
- [Simon Willison's newsletter](https://simonw.substack.com/) — high-
  signal weekly digest of LLM releases. Covers local models, tool
  calling, multimodal — all relevant to our pipeline.
- [r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/) — community
  where benchmarks like lintware's go viral first. High noise, but
  also where "this new model is actually different" posts surface
  before aggregators catch them.

---

## mail_reader webapp

Lives under `mail_reader/` (not `scripts/`). FastAPI + Jinja2 + HTMX behind
Caddy at `https://server.example.ts.net/mail/`; supervised by the
`mail-reader.service` user unit (`uv run python -m mail_reader.server`).
Design lives in `mail_reader/DESIGN.md`, parking-lot in `mail_reader/IDEAS.md`.

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

## kindle_dashboard — wall display PNG generator

`scripts/kindle_dashboard/` is a FastAPI + Playwright service that renders a
single PNG for the wall-mounted Kindle Paperwhite. See
[KINDLE.md](../KINDLE.md) for the device-side jailbreak/kiosk state and
[scripts/kindle_dashboard/README.md](kindle_dashboard/README.md) for the
generator's architecture, content blocks, and customization hooks.

```bash
# Dev: launch with logs in the foreground (run from inside diary-scripts/)
uv run python -m scripts.kindle_dashboard.serve

# Production: systemd user unit
systemctl --user {start,stop,restart,status} kindle-dashboard
journalctl --user -u kindle-dashboard -f
```

Endpoints once running:
- `http://192.0.2.10:8801/dashboard.png` — what the Kindle on the LAN polls
- `https://server.example.ts.net/kindle/` — phone preview over the tailnet

The Kindle SSH key lives at `~/.ssh/kindle_ed25519` on server, with
the `kindle` host alias in `~/.ssh/config`. Standard `ssh kindle 'initctl
restart dashboard'` forces an immediate refresh.

When adding a new content block: collector in `data.py` (return empty/None
on failure, never raise), wire it into `view.build_context()` with a
try/except, add a template section that renders gracefully when empty.
PNGs are 1448×1072 landscape composed, rotated −90° → 1072×1448 portrait,
mode "L" — render.py's `_ensure_kindle_format()` enforces this. **Do not
bypass it**; raw RGBA out the door breaks `eips` on-device.

## Immich — `immich-recent`

`immich_recent.py` lists recently-added Immich assets via the API —
the fast replacement for crawling the NFS-mounted library with
`find -newermt` (multi-minute over RAID6) when the user says "I've
uploaded new photos" (innbo rounds, receipts, …). It searches
`POST /api/search/metadata` with `createdAfter` (upload time, indexed
in Postgres), ascending:

```bash
uv run immich-recent                              # added in the last 24 h
uv run immich-recent --since "2026-08-04 14:20"   # naive = local time
uv run immich-recent --limit 40
uv run immich-recent --thumbs /tmp/im-thumbs      # + preview JPEGs over HTTP
```

`--thumbs` writes `NN-<originalFileName>.jpg` in listing order — read
those instead of multi-MB originals over NFS. `originalPath` in the
listing is the container-side path (`/data/…`); the library root is
`UPLOAD_LOCATION` (NFS mount, see diary PHOTOS.md) when you need the
original.

Auth: API key from `$IMMICH_API_KEY`, else `immich.api_key_cmd` in the
private config (a `pass` reference, same pattern as `spond.password_cmd`
— the key is never committed). Create the key in Immich → user avatar →
Account Settings → API Keys; check exactly **`asset.read`** (search) +
**`asset.view`** (thumbnails) — keep it read-only. `asset.download` is
only needed if originals must ever be fetched via the API (they're
normally read over NFS).
Base URL: `$IMMICH_URL`, else `hosts.immich` in config.
