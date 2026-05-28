# scripts/

Local helper scripts for the workspace. Python deps live in a **uv project at
workspace root** (`pyproject.toml` + `uv.lock`). Invoke any script with

```bash
cd ~/diary
uv run scripts/<name>.py …
```

`uv run` creates/syncs `.venv/` on demand; you never need to activate
anything. Tests: `uv run pytest scripts/` (doctests + `test_embed_mail.py`).

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
- **`tag:inbox` mirrors `folder:INBOX`** — kept in sync automatically by the `post-new` hook (`scripts/notmuch_sync_tags.py`). Same for `tag:spam`, `tag:archive`, `tag:sent`, `tag:draft`, `tag:trash`. The hook also clears stale `unread` from `Sent`. Either query form is fine now.
- **Before 2026-05-15 the `inbox` tag was unreliable** (the bulk import marked ~205 k messages as `inbox` regardless of folder). If you see old digests or notes that say "use `date:` filters because `tag:inbox` is meaningless" — that's no longer true. Backup of pre-reconciliation tag state is in `memory/notmuch-dumps/`.
- **State file:** `memory/mail-state.json` (when I build the digest cron).
- **Digests:** `memory/mail/YYYY-MM-DD.md`.

### Reading mail bodies — `mailshow.py`

Use `scripts/mailshow.py` — handles the boilerplate (raw fetch, html→text, encoding,
attachment summary). Examples:

```bash
uv run scripts/mailshow.py --limit=5 'tag:inbox and date:today..'
uv run scripts/mailshow.py thread:00000000000349df
uv run scripts/mailshow.py --headers-only --limit=20 'from:gonordic'
uv run scripts/mailshow.py --max-chars=8000 id:<message-id>

# Process-new-mail entrypoint: starts from memory/mail-state.json:last_successful_run.
uv run scripts/mailshow.py --since-cursor --headers-only
uv run scripts/mailshow.py --since-cursor 'from:astrid'   # narrow within the cursor window
```

Don't write fresh inline `python3 -c "..."` blocks for body extraction — extend this
script instead so improvements stick.

### Semantic search — `search_mail.py` (pgvector + bge-m3)

Use `scripts/search_mail.py` for fuzzy / cross-language / topical queries —
useful when the exact word probably isn't in the mail (e.g. "byggematerialer"
finds renovation threads even when none contain that word). For exact-text
matching prefer plain `notmuch search`; semantic search is the right tool when
you don't know the keyword.

```bash
# basic
uv run scripts/search_mail.py "examplefund utbetaling 2025"

# filter by tier (see "tier definitions" below), sender, date
uv run scripts/search_mail.py --tier 1 --since 2025-01-01 "fakturaer fra strøm"
uv run scripts/search_mail.py --from astrid "ukeplan"

# exclude a sender (substring, repeatable)
uv run scripts/search_mail.py --not-from exampleconcrete "byggematerialer"

# semantic minus — subtract a concept (default --weight 0.3; 0.7+ breaks query)
uv run scripts/search_mail.py --minus mikrosement "byggematerialer"
uv run scripts/search_mail.py --weight 0.5 --minus dogs "animals"
```

Each hit prints distance, date, sender, subject, `id:<message-id>`, and a
240-char snippet. Pipe an `id:` into `scripts/mailshow.py` for the full body.

**Caveat — snippets can stitch chunks across attachments.** A single mail
with multiple attachments produces chunks from each attachment under the
same `message_id`. Adjacent chunks in the result list look like one quote
but can come from unrelated documents (e.g. the homeowner's own søknad
plus a neighbouring property's søknad attached for context). When an
embedding hit is the *only* source for a durable claim (FDV, `CONTEXT.md`,
biographical fact), open the full attachment text — `mailshow.py
--attachment-text id:…` — and verify in context before writing it as
fact. Especially risky: multi-attachment mails, neighbouring-property
references, søknader/byggesaker (often sent as bundles). Lesson from
2026-05-26 — embedded snippet attributed Brødrene Bisgaard A/S to
Eksempelveien 3B; the actual byggemelding showed Mesterhus Oslo og
Akershus A/L. Bisgaard was tied to a neighbouring søknad in the same
mail bundle.

**Backend:** Postgres `mailvec` (pg18) + `pgvector` HNSW index, embeddings
from Ollama `bge-m3:latest` (1024d, multilingual NO/EN) served by
`gpu-host:11434` (Mac Studio M3 Ultra, 256 GB — GPU-accelerated, much
faster than CPU-only Ollama on `server`). Quote/signature stripping
via `mail-parser-reply` (`en`/`da`/`sv` — `da` catches Norwegian "skrev");
the regex post-pass also strips `-- ` signature blocks.

Schema lives in `migrations/`:
- `001_pgvector.sql` — `messages` + `chunks` (1024-d vector).
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

### Embedding new mail — `embed_mail.py` (automated)

`scripts/embed_mail.py --all` (idempotent on `message_id`, resumable). Runs
**automatically every 15 min** via the systemd user unit
`mail-sync.service` (chains `mbsync -a && notmuch new && uv run --frozen
scripts/embed_mail.py --all --quiet`). The script is silent on success;
failures print `!! <mid>: <err>` to stderr and exit nonzero so `chronic`
surfaces them in the journal.

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

Embedding calls the batched `/api/embed` endpoint (`{"input": [...]}`),
accumulating ~32 chunks across messages per HTTP call. Ollama serialises
embedding requests per model, so concurrency from the client doesn't help —
**batch size is the only useful knob.** Measured throughput on gpu-host:
~27 ms/chunk batched 32 vs ~260 ms/chunk solo. Override the threshold with
`EMBED_BATCH=N`.

Re-run manually after a big mail import. After a large batch, drop+rebuild
the HNSW index for best recall:

```sql
DROP INDEX chunks_embed_hnsw;
CREATE INDEX chunks_embed_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
```

Env vars (defaults usually fine): `PG_DSN=dbname=mailvec`,
`OLLAMA_URL=http://gpu-host:11434`, `EMBED_MODEL=bge-m3:latest`,
`EMBED_BATCH=32`, `ME_ADDRS=user@example.com`. Do **not** try to
install `talon` — won't build on Python 3.14 (`cchardet` needs
`longintrepr.h`, removed in 3.12+).

### Typechecking + tests

`uv run pytest scripts/` runs doctests + unit tests + integration tests
against `~/Mail` (skipped if absent). `uv run ty check scripts/` runs
Astral's `ty` typechecker. Both should pass clean before commit.

### Auto-archiving the inbox — `archive_inbox.py`

For "archive after N days" workflows, use `scripts/archive_inbox.py`. Rules
live in `scripts/archive_inbox_rules.json`. **Not currently scheduled** — run
manually (`uv run scripts/archive_inbox.py`) or wire up a user timer if you
want it daily; only `mail-sync.timer` is installed today.

### notmuch post-new hook

`notmuch new` runs `~/Mail/Proton/.notmuch/hooks/post-new` after indexing,
which calls `scripts/notmuch_sync_tags.py --apply --quiet`. Without this,
folder moves on the Proton side (e.g. archiving a mail) don't update
`tag:inbox` and the mail-reader inbox view goes stale.

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

### Extracting attachments — `mailshow.py --attachment-text` / `--attachments`

`mailshow.py` handles attachments via two independent flags (reuses
`embed_mail.iter_attachments`, so PDF/DOCX/ODT/text extraction matches what
the embedder sees):

```bash
# inline extracted text (PDF/DOCX/ODT/text) after the body — combine freely
# with --headers-only when the body is junk and you only want the PDF
uv run scripts/mailshow.py --headers-only --attachment-text id:<message-id>

# save raw bytes to disk (filenames sanitised; collisions get .1, .2 …)
uv run scripts/mailshow.py --attachments=/tmp/out id:<message-id>
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

## Finance — `finance_ingest.py`

Summarise a Bulder Bank CSV export and cross-reference it with the embedded
mail archive.

Bulder is mobile-only; there's no API. The flow is: export from the iOS app
share-sheet → mail to `user@example.com` → ingest. The CSV is named
`eksporterte_transaksjoner.csv`, semicolon-separated with Norwegian comma
decimals.

```bash
# From a CSV on disk
uv run scripts/finance_ingest.py /tmp/bulder/eksporterte_transaksjoner.csv

# Auto-extract latest "Bulder bank eksport" mail and summarise
uv run scripts/finance_ingest.py --from-mail

# Same, plus cross-reference vs embedded mail and enqueue matches for tier-2
PG_DSN=dbname=mailvec uv run scripts/finance_ingest.py --from-mail --enrich
```

Output (markdown tables on stdout): per-month inn/ut, per-account outflows,
top-25 merchants by `Tekst`, recurring (≥2 months), large one-offs (≥20k NOK).

**Bulder's `Hovedkategori` / `Underkategori` columns are mostly garbage** — the
summary leans on `Tekst` (merchant) and `Dato` instead. Don't trust the auto-
categorization.

**`--enrich` does two things:**
1. For each "interesting" transaction (large / unlabelled / Ukategorisert),
   finds matching mail by (a) amount-match against extracted money entities
   in postgres, (b) notmuch search on date±3d + merchant keyword.
2. For every matched mail, calls
   `mail_reader.summarize.claim_for_generation(tier=2)` — enqueuing it for the
   qwen3.6 structured-extraction pipeline. Workers in `mail-reader.service`
   process the queue; results show up in the webapp at `/mail/`.

The matches displayed in the output are deliberately broad (any mail in the
date window with a merchant-keyword hit) — the value isn't precise correlation,
it's that the bank ledger marks those mails as worth deeper processing.

Cadence: monthly, see `CALENDAR.md`. Always reuse the existing pipeline:
`mail_reader.db.connect()`, `mail_reader.summarize.claim_for_generation`,
`scripts.embed_mail.embed_batch` — don't reimplement DSN / OLLAMA / embedding.

---

## Calendar — `retire_calendar.py`

Part of the daily **sjekk-flow** (mail + spond + retire). Cuts expired one-off
events out of `CALENDAR.md` and inserts them into `CALENDAR-PAST.md`. See
`CLAUDE.md` → "Sjekk-flow: retire past calendar entries" for the full procedure.

```bash
uv run scripts/retire_calendar.py --dry-run            # preview
uv run scripts/retire_calendar.py                      # apply, today = date.today()
uv run scripts/retire_calendar.py --today 2026-06-01   # simulate a later date
```

- Operates only inside `## One-off events by month`. Recurring weekly/monthly/
  quarterly sections are skipped.
- Decision is on **end-date** for date spans (so `2026-06-29 – 2026-07-03`
  retires under July when 2026-07-04+ arrives).
- Drops `### Month Year` subheadings that end up empty in `CALENDAR.md`.
- New month sections in `CALENDAR-PAST.md` are inserted chronologically.
- Idempotent: when nothing is expired, both files are left byte-identical
  (no spurious whitespace churn).
- The `[[memory/YYYY-MM-DD.md]]`-tail enrichments on moved lines are a manual
  judgment call; the script intentionally does not touch the line body.

Tests: `uv run pytest scripts/test_retire_calendar.py`.

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
uv run scripts/pr_compose.py --since-cursor

# Classify + tag, but don't open PRs:
uv run scripts/pr_compose.py --apply --since-cursor

# Full pipeline — classify, tag, file PRs:
uv run scripts/pr_compose.py --apply --file-prs --since-cursor

# File PRs only (skip classify), capped at 2:
uv run scripts/pr_compose.py --file-prs --apply --limit-prs 2 'id:none'
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
uv run scripts/mlx_tool_probe.py --model <model-id>

# Real writer scenario on a notmuch thread:
uv run scripts/mlx_tool_probe.py --model <id> --scenario writer_lenient \
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

---

## mail_reader webapp

Lives under `mail_reader/` (not `scripts/`). FastAPI + Jinja2 + HTMX behind
Caddy at `https://server.example.ts.net/mail/`; supervised by the
`mail-reader.service` user unit. Design lives in `mail_reader/DESIGN.md`,
parking-lot in `mail_reader/IDEAS.md`.

### End-to-end verification — `verify_browser.py`

After a change to routes, templates, CSS, or the tankekart pipeline, drive
the running webapp through headless Chromium:

```bash
uv run mail_reader/verify_browser.py --clean
uv run mail_reader/verify_browser.py --base http://other.host/mail --keep-going
```

Four independent checks (inbox + agenda dismiss, message-view entity chips +
`/e/{id}`, tankekart `chunks → themes → emergent` switching, error-path
status codes) each write numbered screenshots to `/tmp/mr_shots/`. Exits
non-zero if any failed. Uses system `/usr/lib64/chromium-browser/headless_shell`
so playwright doesn't fetch its own browser.

Defaults to the tailnet Caddy URL — hitting `127.0.0.1:8800` directly bypasses
the `/mail/` prefix baked into every `url_for()` link by FastAPI's
`root_path` setting, so clicks 404. Restart the service after deploying new
code (`systemctl --user restart mail-reader.service`) — `uv run` doesn't
reload templates or Python modules on its own.

The agenda-dismiss probe writes a real `agenda_dismissed` row that persists,
so successive runs see one fewer card unless cleaned with
`DELETE FROM agenda_dismissed WHERE thread_id = '…';`.
