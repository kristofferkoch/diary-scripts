# Mail (notmuch + Proton archive)

> Index: [scripts/](../README.md)

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

In a sjekk, `sjekk-collect` has already done step 0 (freshness check +
conditional sync) and produced `memory/sjekk/latest/mail.txt` (step 1's
output) — read that file instead of re-running.

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
   per `~/diary/AGENTS.md` §Memory.
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

# restrict to a source (repeatable; default searches mail + signal + spond
# + vigilo — everything embed-messages indexes)
uv run search-mail --source signal "henting fredag"
uv run search-mail --source mail --source spond "trening"

# semantic minus — subtract a concept (default --weight 0.3; 0.7+ breaks query)
uv run search-mail --minus mikrosement "byggematerialer"
uv run search-mail --weight 0.5 --minus dogs "animals"
```

Each hit prints distance, a `(source)` tag, date, sender, subject,
`id:<message-id>`, and a 240-char snippet. Pipe a mail `id:` into `uv run
mailshow` for the full body (non-mail ids are synthesized —
`signal:…`/`spond:…`/`vigilo:…` — and point back at the JSONL record, not
notmuch). Queries use the generic qwen3 instruction prefix ("retrieve
relevant message passages") since the store is cross-source.

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
- `016_sources.sql` — `messages.source` (`'mail'` for everything pre-016;
  `signal`/`spond`/`vigilo` for embed-messages rows) + `messages.content_hash`
  (embed-messages' upsert key; NULL for immutable mail).
- `017_vlm.sql` — `attachments.vlm_model` (NULL = pending; model id =
  described; `'skipped:tiny'`; `'error'` after 3 attempts) +
  `attachments.vlm_attempts` — embed-images' state markers.

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
    (`text_chars=0`). The VLM pass (`embed-images`, see below) walks these
    and stores image descriptions as attachment chunks.

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
`scripts.embed_mail.embed_query` with the cross-source
`QUERY_INSTRUCTION_GENERIC` prefix ("message passages" — the store holds
more than mail since 016); mail-only callers keep the default
`QUERY_INSTRUCTION`. Documents are indexed plain, and chunk-vs-chunk
similarity in `related.py` stays plain too.

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

### Embedding Signal/Spond/Vigilo — `embed-messages`

`uv run embed-messages` indexes the three non-mail JSONL sinks into the
same `mailvec` store (schema: `016_sources.sql`), so `search-mail` covers
conversations too. Sources under `memory/`:

- `signal/*.jsonl` — signal-capture records. `message_id =
  signal:{ts}:{hash12}`; subject is the conversation label (group name or
  peer); a reply quote folds in as a `> author: text` line. Attachment
  blobs are read from the signal-cli store
  (`~/.local/share/signal-cli/attachments/<id>`, override with
  `$SIGNAL_CLI_ATTACHMENTS`) when the MIME type is text-extractable;
  images/missing blobs stay metadata-only, same as mail.
- `spond/*.jsonl` — `message_id = spond:{kind}:{id}`. Chat → name + newest
  message; event → heading + description + location + start + RSVP counts
  (`Cancelled` marked); post → title + body.
- `vigilo/*.jsonl` — `message_id = vigilo:{kind}:{id}`
  (`message`/`message_update` deliberately share `vigilo:message:{uid}` —
  an update re-emits the whole thread and must upsert over the original,
  not duplicate it). Thread kinds embed title + per-message
  sender/time/body; news/consent embed title + body. Message attachments
  resolve via the local path vigilo-sync recorded; news attachments are
  metadata-only (they download over the API at show time).

All rows get `tier = 0` and a `content_hash` (sha1 of assembled text +
attachment texts). Upsert per record: no row → insert; same hash → skip
(the common case on the 15-min full-history rescan); hash changed →
`DELETE` (cascades chunks/attachments) + re-insert. Embedding batches
across records with embed_mail's `BATCH_CHUNKS`/`BATCH_CHARS` caps, one
transaction per record. Flags: `--source signal|spond|vigilo` (repeatable,
default all), `--limit N` (per source), `--quiet` (silent on success,
`!!` + exit 1 on failure — same contract as embed-mail).

Scheduling: ship-ready systemd user pair `scripts/embed-messages.service`
+ `.timer` (`OnCalendar=*:3/15`, offset from mail-sync's `*:0/15`,
`Persistent=true`). Install with
`install -m644 scripts/embed-messages.{service,timer} ~/.config/systemd/user/`
then `systemctl --user daemon-reload && systemctl --user enable --now
embed-messages.timer`. First manual run backfills full history; idempotent
by design.

Per-source freshness (the sjekk health query):

```sql
SELECT source, now() - max(embedded_at) FROM messages GROUP BY source;
```

signal should track the JSONL tail closely; spond/vigilo only advance
after their syncs run.

### Image descriptions — `embed-images`

`uv run embed-images` describes image attachments (mail + Signal + Vigilo)
with a vision-language model (OpenAI-compatible `/v1/chat/completions`,
`$VLM_URL` → config `hosts.vlm`; model `$VLM_MODEL` → config `vlm.model` →
`qwen3.8-flash-next`) and embeds the descriptions into the same `mailvec`
store — so receipts are findable by their printed text and photos by what's
in them, through `search-mail`'s normal path.

**Pending** = an `attachments` row with an image MIME
(jpeg/jpg/png/gif/webp), `text_chars = 0`, `vlm_model IS NULL`,
`vlm_attempts < 3` (schema: `017_vlm.sql`), newest message first —
idempotent and resumable by construction. `vlm_model` is the state marker:
model id = described, `'skipped:tiny'` = under 128 px on the long side
(icons/tracking pixels, no attempt spent), `'skipped:no-blob'` = no local
blob can ever exist (Vigilo news attachments download over the API at show
time — terminal mark, no attempts burned), `'error'` = gave up after 3
failed attempts. The failure/skip stamps are guarded `WHERE … AND
vlm_model IS NULL` so a manual run racing the timer can't re-pend a row
the other already described.

Pipeline per image: locate the blob (mail via `notmuch show --format=raw` +
walk-order occurrence matching; Signal via the signal-cli blob dir, mid
recomputed from the JSONL with embed-messages' builder; Vigilo via the path
vigilo-sync recorded) → Pillow downscale (long side ≤ 1568 px, GIF first
frame, JPEG q85, base64 data URL) → Norwegian prompt asking for a concise
description plus verbatim transcription of all readable text, with one
context line (attachment filename, message from/subject/date; thinking left
on — it materially improves small-text transcription) → store
`Vedlegg <filename> (bilde): <description>` as ordinary chunks with
`attachment_id` set, batched through embed-mail's `embed_batch` caps, one
transaction per attachment.

Known limitations / trade-offs:

- embed-messages' delete + re-insert upsert (Spond/Vigilo content changes)
  cascades away the VLM chunks and the stamp, so images on updated items
  get re-described. Accepted for now; carrying descriptions over an upsert
  is a possible future optimization.
- Duplicate same-key attachments within one Signal/Vigilo record (same
  filename + size) resolve to the first matching blob — rare in practice,
  and a mismatch just describes the wrong twin once, no retry loop.

Flags: `--source mail|signal|vigilo` (repeatable, default all), `--limit N`
(default 25), `--quiet` (silent on success, `!!` + exit 1 on failure —
same timer contract as embed-mail). Scheduling: `scripts/embed-images.service`
+ `.timer` (`OnCalendar=*:9/15`, offset from mail-sync's `*:0/15` and
embed-messages' `*:3/15`, `Persistent=true`), running
`embed-images --quiet --limit 12` — the ~15-min trickle doubles as the
backfill while bounding occupancy of the shared VLM slot. Install with
`install -m644 scripts/embed-images.{service,timer} ~/.config/systemd/user/`
then `systemctl --user daemon-reload && systemctl --user enable --now
embed-images.timer`.

Health query (pending images waiting on the VLM pass — scoped to the five
handled MIME types so the count converges to 0; a bare `LIKE 'image/%'`
would keep counting tiff/heic/etc. the pass never touches):

```sql
SELECT count(*) FROM attachments
WHERE mime_type = ANY(ARRAY['image/jpeg','image/jpg','image/png','image/gif','image/webp'])
  AND text_chars = 0 AND vlm_model IS NULL;
```

### Typechecking + tests

`uv run pytest scripts/` runs doctests + unit tests + integration tests
against the local mail store (skipped if absent). `uv run ty check scripts/`
runs Astral's `ty` typechecker. Both should pass clean before commit.

**Bug fixes must be red/green TDD.** Write the regression test **first** and
confirm it fails against the un-fixed code (red), then apply the fix and
confirm it passes (green). This proves the test actually pins the bug instead
of passing trivially. Practical way to show red against the current tree:
`git stash -- <impl>` (or `git stash push <impl>`), run the new test to see it
fail, then `git stash pop` and run again to see it pass. (Regression test
added for the weekday-parenthetical ordering bug in `kindle_dashboard/data.py`
was verified this way.)

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
- **`~/.mbsyncrc` must keep `Expunge None`** (changed from `Expunge Both`
  2026-09-13): Proton Bridge rejects expunge/CLOSE in `All Mail`
  ("operation not allowed"), so any deletion propagated there made every
  mbsync run exit 1 — which silently blocked the whole `mail-sync.sh`
  chain (`notmuch new` + `embed-mail`) for days. The residual
  "Notice: conflicting changes in (<far>,<near>)" line after such an
  episode is non-fatal.

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
