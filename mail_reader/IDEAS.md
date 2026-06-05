# IDEAS.md — mail-reader

Brain-dump scratch-pad. Both user ideas and Claude ideas. Not a spec — that's
`DESIGN.md`. Anything in here is **provisional**. When an idea hardens, promote
it to DESIGN.md and (optionally) leave a one-line gravestone here.

Format: `## YYYY-MM-DD — short title` headings, newest at top. Tag author with
`(user)` or `(claude)` if relevant.

---

## 2026-06-05 — sandboxed read-only notmuch access (user)

> "Another idea: sandboxed read-only access to notmuch. Could be useful in
> conjunction with the LLM-composed database search."

A third read corpus for an LLM search/escalation agent, alongside the
Postgres RO role ([[#2026-05-22-—-llm-composed-search-via-read-only-db-role]])
and the markdown store
([[#2026-05-23-—-tier-2-escalation-flag-→-second-level-agent]]). The
`summaries`/`chunks`/`entities` tables are a *derived* view of the mail;
notmuch holds things the DB doesn't fully replicate:

- the **tag taxonomy** — `tag:inbox`, `tag:digest::*`, `tag:important`,
  thread/flagged state — which is where triage decisions actually live;
- **Xapian full-text** with notmuch's own query language (`from:`,
  `subject:`, `date:..`, boolean) — different recall than pgvector
  semantic search, complementary to it;
- **thread structure** as notmuch sees it.

So an LLM that can compose *both* a SQL query (structured/semantic) *and*
a notmuch query (tag/full-text) covers more question shapes than either
alone — "unanswered mail from skolen tagged important in May", "threads I
flagged but never replied to".

**Why "sandboxed" is the crux.** notmuch is read-mostly, but the binary
mixes read and write subcommands. The danger isn't `search`/`show` —
it's `notmuch tag` / `new` / `insert` / `reindex` / `config set`
mutating the index (which the whole 15-min sync + triage pipeline
depends on), plus the usual shell-injection risk if the LLM's query
string reaches a shell. Sandbox layers:

1. **Subcommand allowlist** — only `search`, `show`, `count`, `address`.
   Never `tag`/`new`/`insert`/`reindex`/`config`/`dump`/`restore`.
   Args passed as an argv list (no shell), query string as a single
   positional arg so it can't smuggle flags (`--` terminator).
2. **OS-level RO** — run under a restricted process with the maildir +
   `.notmuch/xapian` bind-mounted read-only (bwrap / a dedicated unix
   user without write perms). Even a bug in (1) can't mutate the index.
3. **Bounded cost** — subprocess timeout (the notmuch analog of the
   RO role's `statement_timeout`) + a cap on result count / output bytes.

**Tool surface for the agent** (mirrors the `md_*` / SQL shapes):
- `notmuch_search(query, limit=20)` → `[{thread_id, msg_id, from,
  subject, date, tags}]` (i.e. `notmuch search --format=json --output=summary`).
- `notmuch_show(msg_id)` → headers + plain body for one message
  (reuse the same sanitisation `mailshow` already does).
- maybe `notmuch_count(query)` for cheap "how many" answers.

Could share the single agent harness from the tier-2 escalator with a
fourth entry in the tool allowlist. Reuses notmuch's existing JSON
output — little new code beyond the sandbox wrapper. Caveat: the index
is ≤15 min stale (same as the inbox view) — fine for retrospective
questions, not for "did this *just* arrive".

## 2026-06-05 — tankekart hasn't earned its keep (user signal)

> "The tankekart features haven't been useful so far."

Real-world signal after living with it: the mind-map / related-mail
panel hasn't paid off. That **reprioritizes the whole fan-out cluster
below** — [[#2026-05-22-—-tankekart-that-actually-fans-out]] (incl. the
"chosen" per-chunk option 2), and the parts of
[[#2026-05-22-—-extract-more-per-qwen-call]] justified mainly by feeding
tankekart branch labels (themes + theme-vectors). Before building more
fan-out, worth asking *why* it's not useful: nearest-neighbours of
routine mail are just more routine mail (the "Stripe receipt → all
Stripe receipts" pitfall from the risks section), the signal may be
genuinely low for this inbox, or a flat related-list just isn't a
workflow the user reaches for. Don't sink the per-chunk / MMR / cluster
work in on a feature that isn't landing — the search-and-escalation
direction (md-search, RO SQL, RO notmuch above) looks like the better
bet for LLM-over-the-corpus value. Not a gravestone yet; a flag to
*not* invest further without evidence the core idea helps.

## 2026-06-01 — notat-foto: GPS som signal (user) — ✅ LANDET 2026-06-05

> "gps should not be stripped, but used as a signal."

**Landet (extraction + surfacing).** Beyond preserving the EXIF on the
stored web image, `note_images.process()` now parses the GPS lat/lon into
signed decimal degrees at upload time and `add_attachment` stamps them into
new `gps_lat`/`gps_lon` columns on `note_attachments` (migration 014). The
/notes/ page renders a "📍 sted" chip linking to an OpenStreetMap pin
(click-to-load — no map request on an idle view), and the sjekk CLI prints
the coordinates + map URL under the `📎 bilde` line. Partial / out-of-range
/ absent fixes resolve to NULL — location is optional by construction
(screenshots, location-off, share-sheet stripping), so a missing fix never
fails the upload. Doctested DMS→decimal helper + extraction/roundtrip tests
in `test_note_images.py` / `test_notes.py`.

Considered this "complete for now"; **still future work** if it proves
useful:

- **Reverse-geocode** the coordinates to a place name and fold it into the
  note's text / the future VLM description ("foto tatt ved Eksempelveien 3B" /
  "…ved Rema Olsvik"). Local-only geocoder or a cached lookup — no need to
  phone out per note.
- **Query by location** ("notater tatt i nærheten av X") — the structured
  columns make this a plain SQL distance filter now; pairs with the
  LLM-composed search ideas below.
- **Sjekk hint**: a grocery photo snapped at a store, a parking-spot photo
  at an airport — location disambiguates terse captures. (Needs the
  reverse-geocode step to be legible; raw lat/lon isn't.)

## 2026-05-31 — notater: bilde-opplasting (user) — ✅ IMPLEMENTERT 2026-06-01

> "Legg til mulighet for å ta eller laste opp bilde på notatsiden. (firefox på android)"

**Landet.** `note_attachments`-tabell (migration 013) lagrer web+thumbnail som
BYTEA i postgres, pluss reserverte `description`/`description_model`/`described_at`
-kolonner for en senere vision-modell-pass (qwen2.5vl) som tolker bildet.
`mail_reader/note_images.py` gjør Pillow-prosessering: EXIF-orienter → nedskaler
til ≤1600px (thumbnail ≤480px) → re-encode JPEG (beholder GPS/EXIF som signal —
se "GPS som signal" over). Capture-formen
er multipart med `<input type=file accept=image/* capture>`; et notat kan ha bilde,
tekst eller begge — bildeløst-tekst og tekstløst-bilde er begge gyldige. Thumbnail
vises i `_note.html`, full størrelse ved klikk (`/notes/attachment/{id}`). Sjekk-CLI:
listingen flagger `📎 bilde (vedlegg #N)`, og `uv run notes image N <fil>` dumper bildet
så det kan åpnes. Se `diary-scripts/scripts/README.md` → "Notes queue — notes".

## 2026-05-31 — notater: vis klokkeslett (user) ✅ LANDET 2026-06-02

> "Legg til klokkeslett på notat siden."

`_note.html` viste før bare dato (`short_date`, f.eks. «31. mai»). user ville ha
**klokkeslett** med — nyttig når flere notater fanges samme dag. Løst med ny
`short_datetime`-filter i `date_format.py` («31. mai 14:07», doctestet),
registrert i `server.py` og brukt i `_note.html`. Kilde: notes-queue #26.

## 2026-05-23 — tier-2 escalation flag → second-level agent (user)

> "Tier 2 should recommend launching another high-level model (with
> thinking) to run with more context (e.g. CALENDAR.md or other
> things) if appropriate. Should not write, but could provide e.g.
> pull requestable branches?"
>
> Addendum: "Should not inject all .md into context, but give somehow
> a reader-window+grep+semantic chunk search interface. General search
> function for the markdown should probably be implemented first."

A third layer above the existing two-pass queue. Tier-2's structured
JSON (qwen3.6) gets a new boolean `escalate` + a one-sentence
`escalate_reason`. When true, a separate agent runs — bigger model,
extended thinking, broader context — and writes its proposal as a
*branch* the user reviews before any state changes.

**Why a third layer.** Tier-2 is structured-but-shallow: it tags
deadlines, themes, entities. It doesn't reason across the workspace
("does this collide with the offsite Astrid mentioned in last
Tuesday's mail?"). A deeper pass with the right context can. But
running a deep pass on every mail is wasteful — most mail is
LinkedIn-notification noise. Tier-2 already knows the difference;
let it decide.

**Read-only by construction.** No auto-writes. The escalator's
output is a git branch with diffs against `CALENDAR.md` / `BILLS.md`
/ `DELIVERIES.md` / a draft reply. User reviews and merges (or doesn't)
in a follow-up turn. Same trust model as `ultrareview`: agent
proposes, human disposes.

**Context interface (per addendum).** Don't dump CLAUDE.md +
USER.md + CALENDAR.md + ... into the escalator's prompt — it'll
drown and the bills will scale poorly. Instead:

1. **General markdown-search tool — build this first.** New script
   `diary-scripts/scripts/search_md.py` (or extend `search-mail` to also index
   the workspace's own .md files). Same pgvector + bge-m3 backend;
   new table `md_chunks` with `(path, chunk_idx, embedding, text)`.
   Walks `*.md` outside `memory/mail/`. Re-indexed on the same
   15-min `mail-sync.timer` cycle, idempotent by `(path,
   chunk_idx, content_hash)`.

2. **Tools handed to the escalator agent**, as a small allowlist:
   - `md_read(path, offset=0, limit=200)` — windowed read.
   - `md_grep(pattern, paths=None)` — literal/regex over the
     curated set.
   - `md_search(query, k=8)` — semantic top-K over `md_chunks`,
     returning `(path, line_range, snippet)` tuples.
   - Optional: `mail_search(query, k=5)` reusing `search-mail` for
     cross-corpus questions ("did Astrid mention the offsite?").

   No writes. No shell. The agent reads, reasons, then emits a
   structured proposal (diff hunks + summary).

3. **Loop guard.** Hard cap on tool-call count per escalation
   (say 30) and on wall time (60 s). If the agent spins, the
   proposal is discarded and the escalation marked `failed`.

**Where it surfaces in the UI.** In the message view, near the
agenda card / action_required badge: a chip "⚑ escalert — forslag
klart" (proposal ready) → click opens a side panel with the diff
hunks and accept/reject buttons (UI-level only — the actual merge
is a `git apply` triggered by a button-press, still confirmed).

**Schema sketch.**

```sql
ALTER TABLE summaries
  ADD COLUMN escalate BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN escalate_reason TEXT;

CREATE TABLE escalations (
  id            BIGSERIAL PRIMARY KEY,
  summary_id    BIGINT REFERENCES summaries(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,    -- pending / running / done / failed
  branch_name   TEXT,             -- git branch with the proposal
  diff_summary  TEXT,             -- 1-3 sentence overview
  tool_calls    INT,
  ms_elapsed    INT,
  workspace_git_sha TEXT,         -- HEAD of the workspace repo at escalation start
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE escalation_tool_calls (
  id            BIGSERIAL PRIMARY KEY,
  escalation_id BIGINT REFERENCES escalations(id) ON DELETE CASCADE,
  seq           INT  NOT NULL,    -- order within the escalation
  tool          TEXT NOT NULL,    -- md_read / md_grep / md_search / mail_search
  args          JSONB NOT NULL,   -- exact args the agent passed
  result        JSONB,            -- what we returned to the agent (capped/truncated)
  git_sha       TEXT NOT NULL,    -- workspace HEAD at the moment of THIS call
  called_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ms_elapsed    INT,
  UNIQUE (escalation_id, seq)
);
CREATE INDEX escalation_tool_calls_esc ON escalation_tool_calls(escalation_id, seq);
```

**Audit / replay (per user).** Every tool call the escalator makes
against the workspace .md store gets a row with the **git SHA of the
workspace repo at the time of that call**. Together with the captured
`args` and `result`, this means any past proposal can be reconstructed:
take the SHA, `git checkout` it, re-run the same `md_grep`/`md_read`/
`md_search` with the recorded args, and you see exactly what the LLM
saw. Without this the escalator is a black box — proposals materialise
and there's no way to ask "why did it think that?" once the repo has
moved on. Capture the SHA per-call (not once per escalation) because
the loop can outlive a `git commit` triggered by an unrelated process
mid-run; `escalations.workspace_git_sha` is the *starting* SHA, the
per-call `git_sha` is the *seen* SHA. Storing `result` (truncated to
e.g. 16 KiB) means you don't need the LLM model to be deterministic
or even available later — the inputs are the record.

**Caveats.**

- **Cost.** Bigger model + extended thinking + tool-loop = real
  money per call. Tier-2's `escalate` decision needs high
  precision — false positives are expensive. Calibrate on a hold-out
  set before turning it on globally.
- **Latency.** Minutes, not seconds. Has to be background-only;
  the user view never blocks on an escalation.
- **Trust.** The diff-as-output keeps blast radius small but raises
  the bar on diff quality. Bad proposals waste user's review time
  more than they save.
- **Order of work.** Per user's addendum, the markdown-search tool
  (`diary-scripts/scripts/search_md.py` + the three `md_*` tool surfaces) is the
  prerequisite. Build and stabilise it on its own first — it's
  useful in many other contexts (general workspace Q&A, the LLM
  natural-language search idea), and the escalator can land on top
  once it's solid.

Related: [[#2026-05-22-—-llm-composed-search-via-read-only-db-role]]
extends this in the SQL direction; this idea extends it in the
markdown direction. The two could share a single agent harness with
different tool allowlists.

## 2026-05-22 — inbox view should show summaries (user)

> "I want summaries visible in the inbox view."

The frontpage today only shows From / Subject / date. With summaries now
cached, surface the best-available `short` per thread on the inbox row.
Same `read_state()` driver as message-view cards, so `done_draft` shows
the draft with the existing styling and HTMX polling brings the final in
without a reload.

Open questions:
- Which message in a thread is the summary source? Probably the latest
  non-self message — same logic as `latest_message_id_in_thread`.
- Bump priorities for everything on screen when the inbox loads? Yes —
  same `bump_priority()` call we already do on tankekart.
- Mobile real-estate: two-line truncation? Fade-to-edge? Worth a quick
  pass with the designer subagent before committing visual.

## 2026-05-22 — LLM-composed search via read-only DB role (user)

> "Search interface through a llm, that composes the query to execute
> (should be a different postgresql role without write access)."

Free-form natural-language search box. The LLM gets a tool-call
interface that emits SQL (or maybe a typed query AST) and the webapp
executes it under a Postgres role with `SELECT`-only grants on the
relevant tables — messages, chunks, summaries, themes, entities,
summary_temporal. No write access, no role-escalation paths.

Why this works for our shape:
- We have rich structured indices already (themes, entities, temporal,
  chunk embeddings, notmuch tags). A query like "what bills came in
  april with deadline before may 15" reduces to a join across
  `entities(kind='money')` + `summary_temporal(kind='deadline')`.
- The cost of a wrong query is bounded (just returns weird results) —
  read-only role plus statement timeout makes the blast radius small.

Implementation sketch:
1. Migration creates `mail_reader_ro` role with explicit `SELECT` grants
   and a `statement_timeout` of a few seconds.
2. New module `nl_search.py` opens a separate psycopg pool with that
   role's DSN. Never reuses the main connection pool.
3. LLM prompt: schema dump + system rules ("only SELECT", "no
   pg_catalog", "no functions outside an allowlist") + the user's
   question. Output: SQL string + a one-sentence explanation.
4. Execute via the RO pool, render as a result list (subject + summary
   + linkback). Optionally show the SQL on click for transparency.
5. Cache the (question, SQL) pair so repeated queries skip the LLM.

Risks:
- SQL injection / accidental joins that return PII for a different
  mail. RO role minimizes the blast; statement timeout caps cost.
- Quality drops fast as schema grows — DB role still bounds damage.
- pg auth: need a separate DSN env var; document in DESIGN.md when
  promoted.

Related: see "search box (reuse `search-mail`)" in DESIGN.md v2 list.
This is the LLM-driven escalation of that.

---

## 2026-05-22 — extract more per qwen call (user)

> "When spending so much time on qwen for summarizing, perhaps we should
> extract more from the context, like the 3-5 themes, and then also cache
> the embedding vectors for those themes. Other things we should extract?"

Each qwen call is ~3 s warm, longer cold. If we're paying that, one
structured extraction yields data we'd otherwise re-derive per feature.
Single round-trip JSON response, cached alongside `summaries.short`.

**High-value extractions:**

- **`short`** (already there) — 1-sentence TL;DR.
- **`themes`**: 3-5 short phrases, in the mail's language. Then embed
  each phrase with bge-m3 and cache the embeddings (new column
  `themes_embeddings vector(1024)[]` or a side table). Powers
  option-4 named branches in [[#2026-05-22-—-tankekart-that-actually-fans-out]]
  with no extra qwen calls at tankekart time.
- **`action_required`** (bool) + **`action`** (string, one sentence):
  does this mail ask the recipient to *do* something? "Bekreft møtetid",
  "Betal innen 15. mai", "Svar med samtykke". Enables an
  inbox-by-action view, or a "todo from mail" digest.
- **`urgency`** (`low` / `normal` / `high`): how time-sensitive. Bumps
  for explicit deadlines, words like "haster" / "urgent".
- **`category`** (enum): `personal`, `school`, `bills`, `delivery`,
  `auth_2fa`, `newsletter`, `receipt`, `commerce`, `medical`, `work`,
  `noise`. Cross-checks the existing `tag:digest::*` from notmuch — we
  can use disagreement as a signal for retagging.
- **`entities`**: `{people: [...], orgs: [...], dates: [...], amounts: [...]}`.
  Cheap, well-shaped, lets us cross-link ("show all mail mentioning
  Astrid Hansen in May").
- **`reply_priors`**: should I reply? If so, in one sentence, what's
  the gist of the appropriate reply? Useful when compose is added later.
- **`primary_language`**: `no`/`en`/`mixed`. Cleaner than guessing per
  feature; also lets us call `qwen` with language-pinned prompts.

**Schema sketch:**

```sql
ALTER TABLE summaries
  ADD COLUMN themes TEXT[],
  ADD COLUMN action_required BOOLEAN,
  ADD COLUMN action TEXT,
  ADD COLUMN urgency TEXT,
  ADD COLUMN category TEXT,
  ADD COLUMN entities JSONB,
  ADD COLUMN reply_priors TEXT,
  ADD COLUMN primary_language TEXT;

CREATE TABLE summary_theme_vectors (
  summary_id BIGINT REFERENCES summaries(id) ON DELETE CASCADE,
  theme_idx  INT  NOT NULL,
  theme      TEXT NOT NULL,
  embedding  vector(1024) NOT NULL,
  PRIMARY KEY (summary_id, theme_idx)
);
CREATE INDEX summary_theme_hnsw
  ON summary_theme_vectors USING hnsw (embedding vector_cosine_ops);
```

**Prompt shape**: one structured JSON response. Qwen3.6 handles JSON
mode well. Strip everything outside the outermost `{...}` (qwen tends
to chatter); validate with a tiny pydantic model; if invalid → status
`failed` with the raw output captured in `error`.

**Caveats:**

- More extraction = more tokens generated = slower per call. Mitigated
  by it being one call instead of N.
- We're committing to a schema that may change. The `summaries.model`
  column already gives us versioning per (mid, model); add an
  `extraction_schema_version` column or a structured `extras JSONB`
  blob to allow drift without nuking history.
- Themes are the biggest payoff for the mind-map work — that pairs
  cleanly with option (2) per-chunk branches: combine "what the mail
  said" (chunks) with "what it was about" (themes) for the branch label.

## 2026-05-22 — tankekart that actually fans out (user + claude)

> "The similarity is kind of ranking in one dimension. Would it be
> possible to 'fan out' into a actual mind map or something with thoughts
> in different directions?"

Right — the current implementation mean-pools all body chunks into one
query vector and shows top-K by cosine. That's a *list of nearest
neighbours*, not a mind map. Real "directions" require something that
separates them.

Five strategies, in ascending complexity. They compose:

### 1. MMR (Maximum Marginal Relevance) — drop-in diversity

Same K=10, but each pick maximizes `λ·sim(q,d) − (1−λ)·max sim(d, picked)`.
Picks 1 nearest, then iteratively picks the next mail that's similar to
the query *but dissimilar from already-picked*. Classic IR move. ~30
lines, no new deps, no UI change. Diverse leaves but still a flat list —
not really a mind map, just a less-redundant ranking.

### 2. Per-chunk queries — branches from what the mail actually said *(chosen)*

Don't mean-pool. Each chunk of the open mail is its own query vector.
Top-N per chunk → each chunk becomes a *branch*, leaves on that branch
are the mails most related to that part of the open mail.

- The branches' identities come from the source: this branch is the
  "Hi Astrid, the electrician arrives Tuesday" part of the mail; that
  branch is the "by the way, the invoice from last month" part.
- Branch label (v1): first ~80 chars of the chunk text. Honest, no LLM.
- Nearly free: chunks already exist in `chunks`. One LATERAL SQL query
  does the whole thing for indexed mail. For live-embed (mail not in
  mailvec), each chunk runs one nearest-neighbour query — fast.
- Dedup question: a leaf can appear under multiple branches (means
  "related on multiple dimensions"). Allow duplicates in v1 — the
  multi-direction signal is informative.
- Fallback: if the mail has only one chunk (short mail), branches
  degenerate to a single flat list. Same as today's behaviour.

### 3. Cluster the top-N — emergent themes

Pull top-40 by similarity, run k-means / agglomerative on the result
embeddings, render 3-5 clusters as branches. Clusters are *emergent
from the result set*, not from the open mail. Tradeoff vs (2):
clusters can surface themes the open mail doesn't talk about itself
("oh, three of these are unrelated to Astrid but all about the bank
loan"); but clusters are unlabeled — you see the leaves and infer the
theme. Needs scikit-learn (or hand-rolled k-means), more code.

### 4. LLM-extracted aspects — *named* branches *(promising; later)*

Ask qwen to name 3-5 themes in the open mail. Embed each theme; top-N
per theme. Branches now carry natural-language labels ("varmekabler",
"tilbudsbrev", "betaling"). Slowest, most expensive, but most legible.

**Pairs powerfully with (2):** use chunks to find candidates, use the
LLM to label the resulting branches with what they're "about". Or use
the LLM to rewrite the chunk-text label into a 2-3-word name.

### 5. Multi-facet — non-semantic dimensions

Separate columns/branches per facet:
- *People*: mails to/from same correspondents.
- *Time*: same week, same month.
- *Label*: same Proton label / digest tag.
- *Thread*: continuations/forks of this conversation.

Doesn't need new embeddings. Adds richness orthogonal to semantic
similarity. Most code; mostly UI grouping.

### Build order

- **Now**: (2). Cheapest, biggest UX shift. Tells us whether body chunks
  even map to distinct directions in practice.
- **If chunks cluster into one direction in practice**: jump to (3).
- **If (2) works**: add (4) on top — name the branches with qwen.
- **Whenever**: (5) as parallel columns/sections, not a replacement.

### Open subquestions

- How many branches? 3-5 feels right. With long mails (10+ chunks) we
  may need to fold neighbouring chunks together (or run (3) over the
  chunks first to pick representative ones).
- How many leaves per branch? 3-4. With 4 branches × 4 leaves = 16
  cards, denser than today but still scannable on mobile.
- Should the open mail's chunks be visible to the user (as branch
  labels)? Yes for v1 (honest). Once (4) lands, replace with named
  labels.
- Mobile layout: stacked sections work; horizontal scroll per branch
  could also work for "tabs" UX, less Norway-flag-on-screen.

## 2026-05-22 — `summaries.status` enum (user)

> "Perhaps the summaries table should contain an enum with the state of
> the generation. That would also support an extension for streaming
> text."

This fixes three things at once that are otherwise three separate fixes:

1. **In-flight dedup.** Today, two requests for the same uncached message
   both call Ollama in parallel — visible right now as a wedged GPU when I
   kept retrying a hung curl. With a status column, the generator
   `INSERT ... ON CONFLICT DO NOTHING` claims the row at `pending`. The
   second request reads `pending` and waits / shows a placeholder instead
   of double-spending GPU cycles.

2. **Streaming.** Composes cleanly with [[#2026-05-22-—-stream-summary-tokens]] —
   while tokens come in, status is `streaming` and `short` carries the
   partial text. Readers poll / SSE on changes. Final flush sets
   `status='done'`.

3. **Crash recovery + retries.** A row stuck in `pending` for >N seconds
   without progress can be reclaimed. `failed` status surfaces errors
   without burning a real summary slot, and unblocks the message for a
   retry on next view.

Sketch:

```sql
CREATE TYPE summary_status AS ENUM ('pending', 'streaming', 'done', 'failed');

ALTER TABLE summaries
  ADD COLUMN status summary_status NOT NULL DEFAULT 'done',
  ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN error TEXT;
```

(`'done'` default keeps existing rows correct; `short` keeps holding either
partial-or-final text.)

State machine: absent → `pending` (claimed) → `streaming` (first token) →
`done` (final flush). `failed` is a terminal-but-retryable sink.

Cross-process coordination on transitions: `LISTEN`/`NOTIFY` per
`message_id` is cheap and we already have one PG connection per request.

Worth pairing the implementation work with the streaming endpoint — both
land at once. Belongs in DESIGN.md once we commit.

## 2026-05-22 — stream summary tokens (user)

> "summaries should stream tokens as they become available."

Right now the tankekart fragment blocks until **all** 8–12 summaries are
generated (serial, ~1-3s warm per card, multi-minutes cold). Streaming
tokens per card makes the page feel alive even on a cold start.

Sketch:

- Render the tankekart fragment immediately with empty summary cells:
  `<div class="summary" sse-swap="message" sse-connect="/api/sum/{msgid}"></div>`
- One SSE endpoint per card: `GET /api/sum/{msgid}` streams tokens until
  done, then writes the final string to the `summaries` table.
- HTMX has the `htmx-ext-sse` extension; lightweight enough to vendor.
- Ollama's `/api/chat` with `"stream": true` returns NDJSON — easy to
  forward as SSE.
- Cache hits skip streaming entirely: endpoint returns the full cached
  summary in one event and closes.

Open subquestions:
- 8–12 concurrent SSE connections to one origin — fine? (browsers cap
  HTTP/1.1 at 6 per origin. With HTTP/2 via Caddy → no cap. Worth
  checking Caddy is doing h2.)
- Ollama serialises per-model anyway, so client concurrency doesn't speed
  up generation — but it DOES let the *first* card start rendering
  immediately rather than wait for the entire batch to finish. So
  streaming is purely a UX win, not throughput.
- The current `get_or_create_summary` is sync. The SSE endpoint would
  need a streaming variant; the cache write happens at "done".


## 2026-05-22 — initial brief (user)

- Webpage to read Proton mail. Mobile-friendly.
- Hosted on the tailnet, **no auth beyond Tailscale identity**.
- Front page = current Proton inbox.
- Mail detail view shows the mail + a **tankekart** (mind-map) of related mails
  with short summaries.
- Summaries: cached in the embeddings DB (`mailvec`), generated by an Ollama
  model on `gpu-host`. Candidate models:
  - `qwen3.6:35b-a3b-q8_0` (38 GB) — best quality, slowest
  - `qwen3.6:35b-a3b` (23 GB) — q4 of the same, probably good enough
  - `qwen3:30b-a3b` (18 GB) — older MoE, faster
  - `qwen2.5:3b` — fast small model, fine for 1-line TL;DRs
  - `llama3.3:70b` (42 GB) — alternative for quality
  - `qwen2.5vl:7b` — VLM, for attachment images later

## 2026-05-22 — open questions (claude)

These need a call before DESIGN.md is locked. Asked inline in chat, answers
will land in DESIGN.md.

- **Frontend stack.** Server-rendered HTML + HTMX (no build step, small,
  mobile-fine) vs SPA (React/Svelte) vs plain HTML+vanilla-JS. HTMX is the
  natural fit for the rest of this repo's "small Python script" aesthetic.
- **Tankekart shape.** Interactive force-directed graph (vis-network /
  cytoscape) vs ranked list of summary cards with similarity bars vs 2D PCA
  scatter. Graphs look cool but are awkward on phones; ranked list is
  honest and tappable.
- **Inbox source.** Query notmuch `tag:inbox` (already kept in sync every
  15 min — staleness ≤ 15 min) vs talk to Proton Bridge IMAP live (always
  current, more moving parts). Notmuch is simpler and matches everything
  else here.
- **Summary granularity.** One short (1-sentence) TL;DR vs short + long
  (paragraph). Disk is cheap; the cost is model time on first view. Maybe
  generate short on-demand, long lazily on detail-view open.

## 2026-05-22 — design hypotheses (claude)

Defaults I'd pick if not contradicted:

- **Backend**: Python + FastAPI + uvicorn, dependencies declared inline
  (PEP 723) so it runs the same as the other scripts (`uv run`).
- **Host**: server (notmuch index + Postgres are here). Mac Studio is
  for models only.
- **Tailnet exposure**: bind to the server's Tailscale IP `<host>:<port>` or use
  `tailscale serve`. No auth in-app — Tailscale provides identity. Add
  a `X-Tailscale-User` header check later if we ever want to allowlist.
- **Mail rendering**: server-side HTML→sanitized-HTML (bleach or
  nh3). Keep plain-text as the fallback (matches `uv run mailshow`).
- **Tankekart algorithm**: per-mail "mean embedding" (avg of body chunks)
  → top-K cosine neighbours from `chunks`/`messages`. Exclude same thread
  (already in context) or include with a different marker. K ≈ 10.
- **Summary cache**: new table `summaries` keyed by `message_id` with
  `model`, `short`, `long`, `generated_at`. Generate lazily on first
  open. Background script can backfill the inbox proactively.

## 2026-05-22 — features parking lot (claude)

Things worth considering but not in v1:

- **Compose / reply.** Out of scope for v1 — read-only.
- **Search box.** Reuse `search-mail` semantics. Mobile-friendly results.
- **Tag editing.** Toggle `tag:digest::keep` from the UI for training data.
- **Attachment viewer.** Use already-extracted text from `attachments` table
  for quick previews; raw download for non-text.
- **Image attachment summaries.** Once `embed_images.py` exists, blend VLM
  descriptions into the tankekart.
- **Thread view.** Group by `thread_id`, show the conversation flat.
- **"Why is this in my inbox?"** show signals: tier, tags, similar past
  mail that ended up archived vs kept.
- **Mark-as-read / archive button.** Push back to notmuch tags +
  `archive-inbox` rules.
- **Offline-first.** Service worker + cached recent inbox JSON. Probably
  overkill; the tailnet is what you're on anyway.

## 2026-05-22 — risks / pitfalls (claude)

- **HTML mail XSS.** Proton mail is full of marketing HTML. Sanitize hard
  (no `<script>`, no inline event handlers, no remote images by default —
  proxy them or block).
- **Remote tracking pixels.** If we render external images, every newsletter
  knows when you opened it. Block-by-default, click-to-load.
- **Summary drift.** If we switch models, old summaries are stale but still
  cached. The `model` column + a `--regenerate` flag handles that.
- **Tankekart noise.** Nearest neighbours of "Stripe receipt" are all other
  Stripe receipts — not useful. Either weight by recency / thread diversity,
  or surface "similar but in a different thread."
- **Latency.** Inbox load is cheap (notmuch); detail-view summary is slow
  if model isn't preloaded on `gpu-host`. Keep a small model warm? Or
  precompute on the 15-min sync cron.
