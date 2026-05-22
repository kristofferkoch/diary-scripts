# DESIGN.md — mail-reader

**Status:** draft v0 — initial hypotheses, not yet ratified. See `IDEAS.md`
for alternatives and rationale. Lock decisions here as they settle; move
discarded branches back to IDEAS.md with a one-line "tried, rejected because…"
note.

---

## 1. Goal

A small webpage that lets user read Proton mail from a phone or laptop on
the Tailnet, with a "tankekart" view of semantically related mail on each
message page.

Non-goals (v1): compose, reply, attach, label-editing.

## 2. Deployment shape

- **Host:** `server` (notmuch + Postgres are local here).
- **Tailnet exposure:** bind to the tailscale interface on a fixed port,
  e.g. `100.84.82.95:8800`. No in-app authentication — Tailscale is the
  perimeter. Add a Tailscale-user header check later if needed.
- **Process supervision:** systemd user unit `mail-reader.service`, same
  style as `mail-sync.service`.

## 3. Data sources

| What                | Where                                  | How                |
|---------------------|----------------------------------------|--------------------|
| Inbox listing       | notmuch `tag:inbox` (15-min sync)      | `notmuch search`   |
| Mail body           | notmuch raw → parsed                   | reuse `mailshow.py` helpers |
| Attachments         | `attachments` table (already populated) | Postgres           |
| Embeddings          | `chunks` table (1024-d bge-m3)         | Postgres + pgvector |
| Summaries           | **new** `summaries` table              | Postgres (this app writes it) |
| Summarisation model | Ollama on `gpu-host:11434`           | `/api/chat`        |

## 4. Schema additions

New migration `migrations/003_summaries.sql`:

```sql
CREATE TABLE summaries (
  id            BIGSERIAL PRIMARY KEY,
  message_id    BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  model         TEXT   NOT NULL,
  short         TEXT   NOT NULL,         -- 1-sentence TL;DR
  long          TEXT,                    -- paragraph, optional, lazy
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (message_id, model)
);
CREATE INDEX summaries_message_idx ON summaries(message_id);
```

One row per (message, model) so we can A/B and regenerate without losing
history. v1 reads `WHERE model = $current_model ORDER BY generated_at DESC
LIMIT 1`.

## 5. Backend

- **Language/runtime:** Python 3.14 (matches repo), declared with PEP 723
  inline deps so `uv run mail-reader/server.py` Just Works.
- **Framework:** FastAPI + Jinja2 templates + HTMX on the client. No build
  step, server-rendered HTML, fragment swaps for nav.
- **Modules (planned):**
  - `mail_reader/inbox.py` — list inbox via notmuch.
  - `mail_reader/message.py` — fetch + render single message (reuse body/HTML
    sanitisation from `mailshow.py`).
  - `mail_reader/related.py` — given a message_id, return top-K semantic
    neighbours (mean-pool body chunks, cosine search in `chunks`).
  - `mail_reader/summarize.py` — get-or-create summary via Ollama chat API.
  - `mail_reader/server.py` — HTTP entrypoint, routes, templates.

## 6. Routes

```
GET  /                       → inbox (latest N from tag:inbox)
GET  /m/{message_id}         → mail + tankekart
GET  /m/{message_id}/img/{i} → inline image (proxied / sanitized)
GET  /api/summary/{message_id} → JSON {short, long, model, generated_at}
                                  generates if missing
GET  /healthz                → 200 ok
```

URL form for `{message_id}`: URL-encoded RFC-822 Message-ID, matching how
notmuch addresses messages (`id:<...>`).

## 7. Tankekart algorithm (v1)

1. Look up the open message's `chunks.embedding` rows.
2. Mean-pool them into a single query vector `q`.
3. `SELECT message_id, MIN(embedding <=> $q) AS dist FROM chunks
    GROUP BY message_id ORDER BY dist ASC LIMIT 40`.
4. Drop the open message itself.
5. Drop / collapse same-`thread_id` results (deduplicate to one rep per
   thread, since the thread view is one click away anyway).
6. Take top 8–12.
7. For each, ensure a summary exists (lazy generate). Display as cards.

**Presentation:** ranked list of cards, each showing sender, date, subject,
1-line summary, similarity bar (e.g. "92% match"). Cards link to that
mail's detail page. Graph visualisation (force-directed) is deferred —
phones make it awkward. See IDEAS.md.

## 8. Summarisation prompt (v1)

System: short, neutral, in the **mail's primary language** (Norwegian or
English — detect from body, default Norwegian if mixed).

Output: one sentence, max ~140 chars, describing what the mail is *about*
and what (if anything) it asks of the recipient.

Model: `qwen3.6:35b-a3b` (q4, 23 GB) on `gpu-host`. Strong NO/EN, fast
enough on M3 Ultra. Bump to q8 if q4 output looks sloppy in practice.

## 9. Mobile UX

- Single column. Inbox = tappable rows (sender, subject, snippet, date).
- Mail detail = body up top, tankekart cards stacked below.
- No fancy CSS framework. Plain CSS, system font, ~16px base.
- Pull-to-refresh = browser default. No PWA install path in v1.

## 10. Security

- Sanitize all HTML mail with `nh3` (allowlist tags + attrs).
- Block remote images by default (rewrite `src` to a placeholder; click to
  load). Strip on-* attributes always.
- No write paths in v1 → no CSRF surface.
- Tailnet only — verify by binding to the tailscale IP and *not* `0.0.0.0`.

## 11. Decisions log (2026-05-22)

- **Frontend:** FastAPI + Jinja2 + HTMX. No SPA, no build step.
- **Tankekart:** ranked list of cards. Graph view deferred to IDEAS.md.
- **Summary model:** `qwen3.6:35b-a3b` (q4). Re-evaluate if quality is off.
- **Summary timing:** lazy — generated on first detail-view open, then
  cached. No eager backfill in v1; revisit if first-click latency is bad.

## 12. Roadmap

- **v0 (this doc).** Decisions pending.
- **v1.** Read-only inbox + detail + tankekart + cached summaries.
- **v2.** Search box (reuse `search_mail.py`); tag toggles (`digest::keep`).
- **v3.** Compose / reply (much bigger — needs SMTP send path via Proton
  Bridge; revisit then).
