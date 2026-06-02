"""Multi-pass LLM summaries with a DB-backed priority queue.

Two passes are configured: a fast draft (qwen2.5:3b) and a slow final
(qwen3.6:35b-a3b). When a mail is requested, BOTH passes are claimed —
the draft lands first and is shown immediately, the final replaces it
when ready. All historical rows are preserved (no overwrites of `done`
or `failed` rows) for future quality assessment.

The DB acts as the queue. Workers in `mail_reader.workers` consume it
via `UPDATE … FOR UPDATE SKIP LOCKED`, ordered by `(priority DESC,
requested_at DESC, id ASC)`. Priority is a 0..3 heuristic-floor score
written at enqueue (see `priority.py`); `requested_at` orders within a
priority tier. `bump_priority()` promotes a row to HIGH and sets
`requested_at = now()` so a user-opened mail always beats algorithmic
guesses.

State machine (see DESIGN.md §11):

  absent  →  pending  →  done
                     →  failed
  failed  →  (manual retry creates a fresh pending row)

`done_stale` and `done_draft` are *computed* statuses returned by
`read_state` based on what's available across tiers and prompt versions
— the DB enum only carries the four transient values above.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TypedDict

import psycopg

from . import db, extract, priority
from .config import ollama_url


OLLAMA_URL = ollama_url()  # $OLLAMA_URL → config hosts.llm (see config.ollama_url)

# Bump when the prompt changes; rows at older versions are preserved and
# surfaced as `done_stale` while a new version regen runs.
# p3: tier-2 returns structured JSON (short + action_required + temporal
# + themes + entities). Tier-1 is unchanged free-text.
# p4: replace concrete few-shot examples with abstract patterns — qwen2.5
# was parroting the example facts ("nr 41463", "Eksempel Elektriske",
# "bekreft oppmøte innen tirsdag") into unrelated summaries.
# p5: drop the patterns from tier-1 too. The small model treated even
# abstract <placeholder> patterns as recipes and *hallucinated* invoices
# from LinkedIn notification mails. Rules-only prompt instead.
PROMPT_VERSION = os.environ.get("SUMMARY_PROMPT_VERSION", "p5")

# A row in `pending`/`streaming` for longer than this is presumed
# abandoned (worker crashed, Ollama hung) and may be reclaimed.
RECLAIM_AFTER_SECONDS = int(os.environ.get("SUMMARY_RECLAIM_SECONDS", "600"))

# Shared Ollama sampling/runtime knobs. `temperature` keeps outputs
# stable for structured extraction; `num_predict` caps runaway
# generation (each call is at most a 140-char short + a small JSON
# envelope, so 600 tokens is well over comfortable headroom);
# `num_ctx` is set explicitly because Ollama's default (4096) is just
# barely enough for our system prompt + body — silent truncation is
# the worst failure mode.
_OLLAMA_OPTIONS: dict[str, float | int] = {
    "temperature": 0.2,
    "num_predict": 600,
    "num_ctx": 8192,
}


class Pass(TypedDict):
    model: str
    tier: int
    label: str           # short display name for the topbar queue indicator


# Ordered by tier ascending. The *final* pass is the highest tier — what
# `read_state` prefers when both are done.
PASSES: list[Pass] = [
    {"model": "qwen2.5:3b",      "tier": 1, "label": "utkast"},   # draft: ~1 s warm, ~6 s cold
    {"model": "qwen3.6:35b-a3b", "tier": 2, "label": "endelig"},  # final: ~3 s warm, ~3 min cold
]
MAX_TIER = max(p["tier"] for p in PASSES)
PASS_BY_TIER: dict[int, Pass] = {p["tier"]: p for p in PASSES}

# Back-compat: the default-pass model when the caller doesn't specify.
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", PASS_BY_TIER[MAX_TIER]["model"])


_SYSTEM = """\
Du oppsummerer e-poster på én setning, maks 140 tegn. Skriv på e-postens språk \
(norsk eller engelsk).

ABSOLUTTE KRAV:
1. Bruk bare informasjon som FAKTISK står i e-postens tekst. Ikke finn på \
navn, datoer, beløp, fakturanumre, frister eller andre fakta. Hvis du er i \
tvil om noe står i e-posten, IKKE inkluder det.
2. Hvis e-posten er en automatisk notifikasjon, en sosial-medie-varsel \
(LinkedIn-reaksjon, kommentar, like), nyhetsbrev eller markedsføring — \
beskriv det nøkternt som det det er. Ikke gjør det om til en faktura, \
forespørsel eller frist som det ikke er.
3. Start direkte med innholdet. Ingen meta-fraser som «E-posten handler om», \
«Mailen er om», «Dette er en e-post om» — gå rett på sak.
4. Hvis e-posten ber om noe konkret (svar, betaling, bekreftelse, oppmøte), \
nevn det først.
5. Ingen markdown, ingen anførselstegn, kun den ene setningen.

Hvordan velge innhold (uten maler):
- Ekte handlingskrav: nevn hva som må gjøres, av hvem, innen når — men \
bare hvis alt dette faktisk står i e-posten.
- Informasjonsmail: gjengi det konkrete temaet og hovedfakta som står der.
- Notifikasjon / sosial reaksjon / nyhetsbrev / kvittering: beskriv kort \
hva slags varsel det er og fra hvem. IKKE oppfinn fakturaer, beløp, \
datoer eller frister som ikke står i e-posten.
- For kort eller for generisk til å oppsummere meningsfullt: skriv kort \
hva slags melding det er.
"""


# Tier-2 returns structured JSON. Prompt is in English because
# JSON-schema discipline is more reliable that way across qwen variants,
# while content fields (`short`, `themes`, `note`) are explicitly told
# to follow the email's own language so the user-visible text stays
# Norwegian when the mail is Norwegian.
_SYSTEM_TIER2 = """\
You analyze emails and return ONLY valid JSON. No markdown, no comments, \
no text outside the JSON object.

Schema:
{
  "short": "<one sentence, max 140 chars, IN THE EMAIL'S LANGUAGE>",
  "action_required": <true | false>,
  "temporal": [
    {"kind": "deadline"|"event"|"valid_until"|"mentioned",
     "occurs_at": "YYYY-MM-DD",
     "note": "<short, in the email's language: 'forfall', 'RSVP', ...>"}
  ],
  "themes": ["<1-3 words, IN THE EMAIL'S LANGUAGE>", ...],
  "entities": [
    {"kind": "person"|"org"|"place"|"money"|"identifier"|"contact"|"url",
     "value": "<raw value as extracted>",
     "meta": { /* kind-specific, see below */ }}
  ]
}

LANGUAGE RULE (critical):
Human-readable content fields (`short`, every `note`, every `themes` entry) \
MUST be written in the same language as the email body. If the email is in \
Norwegian, write these fields in Norwegian. If in English, in English. \
Do not translate. The JSON skeleton (field names, enum tags like "deadline", \
"org") stays as specified above.

`short` rules:
- Open directly with the substance. Do NOT use meta-phrases like \
«E-posten handler om», «Mailen er om», «Dette er en e-post om», \
"This email is about", "The email contains" — get straight to the point.
- If the email asks for something (reply, payment, confirmation, attendance), \
mention that first.
- Be concrete about dates, amounts, names.

`action_required` rules:
- true if the recipient must do something concrete: pay, reply, RSVP, attend, \
submit a document, take some action.
- false for informational mail, receipts, notifications with no required action.

`temporal` rules:
- ISO dates only (YYYY-MM-DD). If the email says "next Tuesday" and you are \
not certain of the calendar date, omit the entry.
- `deadline`: recipient must act before this date.
- `event`: something happens on this date (meeting, opening day, other party's deadline).
- `valid_until`: offer / link / coupon expires.
- `mentioned`: generic date reference with no action implied.

`themes` rules:
- 3-5 short theme phrases, in the email's language. Examples for Norwegian: \
["varmekabler", "soverom"], ["foreldremøte", "barnehage"], \
["faktura", "eksempel elektriske"]. Examples for English: \
["invoice", "monthly bill"], ["parent meeting", "kindergarten"].

`entities` rules per kind:
- person: meta = {} or {"role": "sender"|"mentioned"}.
- org: meta = {} or {"type": "bank"|"school"|"agency"|"shop"|...}.
- place: meta = {} or {"address": "...", "city": "..."}.
- money: meta = {"amount": <number>, "currency": "<ISO-3, usually NOK>"}.
- identifier: meta = {"type": "kid"|"account"|"case"|"order"|"tracking"|...}. \
  `value` is the identifier string itself.
- contact: meta = {"method": "tel"|"mail"}.
- url: meta = {} or {"href": "<full url>"}.

If a list field is empty, use [] — do not omit the field.

CRITICAL: Do not copy facts from the schema illustration below. Names,
amounts, dates, and identifiers in <angle brackets> are placeholders to
show the shape — replace them with values from the actual email. If the
email has no value for a field, omit the corresponding object entirely
rather than reusing the placeholder.

Shape illustration (placeholders, NOT real values to echo):
{
  "short": "<one-sentence summary of the actual email>",
  "action_required": <true if recipient must act, else false>,
  "temporal": [
    {"kind": "deadline", "occurs_at": "<YYYY-MM-DD>", "note": "<short label>"}
  ],
  "themes": ["<short phrase in email's language>", "<another>"],
  "entities": [
    {"kind": "org", "value": "<actual org name from email>", "meta": {}},
    {"kind": "money", "value": "<raw text>",
     "meta": {"amount": <number>, "currency": "<ISO-3>"}},
    {"kind": "identifier", "value": "<actual identifier>",
     "meta": {"type": "<kind>"}}
  ]
}
"""


class SummaryState(TypedDict):
    # Composite status, possibly broader than the DB enum:
    #   'done'        — best configured tier is done at current prompt.
    #   'done_draft'  — lower-tier done, higher tier still in flight (poll!)
    #   'done_stale'  — only an older-prompt-version done row exists.
    #   'pending' / 'streaming' / 'failed' — no done content yet.
    status: str
    short: str | None
    error: str | None
    # Only the tier-2 structured pass sets this; tier-1 / pending / failed
    # rows default to False. See migration 007.
    action_required: bool


# ----------------- Ollama -----------------

def _ollama_chat(model: str, subject: str, from_addr: str, body: str) -> str:
    prompt = f"Avsender: {from_addr}\nEmne: {subject}\n\n{body[:6000]}"
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,  # no-op for qwen2.5; useful if model is swapped
            "keep_alive": "30m",
            "options": _OLLAMA_OPTIONS,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return (data.get("message", {}).get("content") or "").strip()


def _ollama_chat_json(model: str, subject: str, from_addr: str,
                      body: str) -> dict:
    """Call Ollama in JSON mode; parse and return the structured payload.
    Raises ValueError on malformed JSON so the caller can mark the row
    failed (it'll be re-claimed on a future request)."""
    prompt = f"Avsender: {from_addr}\nEmne: {subject}\n\n{body[:6000]}"
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_TIER2},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "think": False,  # qwen3 hybrid-reasoning models emit a long
                             # <think>...</think> trace by default; for
                             # structured output that's wasted compute.
            "keep_alive": "30m",
            "options": _OLLAMA_OPTIONS,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    raw = (data.get("message", {}).get("content") or "").strip()
    if not raw:
        raise ValueError("empty model response")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"non-JSON response: {raw[:200]}") from e
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {type(payload).__name__}")
    return payload


# ----------------- state read -----------------

def read_state(conn: psycopg.Connection, notmuch_msg_id: str) -> SummaryState | None:
    """Pick the best summary state to render for a mail. Order:
      1. Best `done` at current prompt_version → 'done' if its tier
         is MAX_TIER, else 'done_draft' if a higher tier is in flight,
         else 'done' (no better pass configured/expected).
      2. Latest in-flight (any pass) at current prompt_version → its raw
         status ('pending' or 'streaming').
      3. Old-prompt-version `done` (any tier) → 'done_stale'.
      4. Latest failed at current prompt_version → 'failed'.
      5. None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        mrid = row[0]

        # 1. best done at current prompt
        cur.execute(
            """
            SELECT short, quality_tier, action_required
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status = 'done'
            ORDER BY quality_tier DESC, generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        done = cur.fetchone()

        # any in-flight at current prompt, AND tier of highest in-flight
        cur.execute(
            """
            SELECT MAX(quality_tier)
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status IN ('pending', 'streaming')
            """,
            (mrid, PROMPT_VERSION),
        )
        inflight_max = cur.fetchone()
        max_inflight_tier = inflight_max[0] if inflight_max else None

        if done is not None:
            short, done_tier, action_required = done
            if max_inflight_tier is not None and max_inflight_tier > done_tier:
                status = "done_draft"
            else:
                status = "done"
            return {
                "status": status,
                "short": short,
                "error": None,
                "action_required": bool(action_required),
            }

        if max_inflight_tier is not None:
            # Pending text not done yet — return the most-recent pending
            # row's status so the UI shows the right placeholder.
            cur.execute(
                """
                SELECT status::text
                FROM summaries
                WHERE message_id = %s
                  AND prompt_version = %s
                  AND status IN ('pending', 'streaming')
                ORDER BY requested_at DESC, id DESC
                LIMIT 1
                """,
                (mrid, PROMPT_VERSION),
            )
            in_row = cur.fetchone()
            return {
                "status": in_row[0] if in_row else "pending",
                "short": "",
                "error": None,
                "action_required": False,
            }

        # 3. stale: latest done at an older prompt version. The
        # action_required column was added in p3; pre-p3 rows have the
        # column-default False, which is the right thing to render.
        cur.execute(
            """
            SELECT short, action_required
            FROM summaries
            WHERE message_id = %s
              AND status = 'done'
              AND prompt_version <> %s
            ORDER BY quality_tier DESC, generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        stale = cur.fetchone()
        if stale is not None:
            return {
                "status": "done_stale",
                "short": stale[0],
                "error": None,
                "action_required": bool(stale[1]),
            }

        # 4. failed at current
        cur.execute(
            """
            SELECT short, error
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status = 'failed'
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        failed = cur.fetchone()
        if failed is not None:
            return {
                "status": "failed",
                "short": failed[0],
                "error": failed[1],
                "action_required": False,
            }

    return None


# ----------------- claim (enqueue) -----------------

class _MsgMeta(TypedDict):
    id: int
    from_addr: str | None
    subject: str | None


def _msg_meta(conn: psycopg.Connection,
              notmuch_msg_id: str) -> _MsgMeta | None:
    """Fetch (id, from_addr, subject) for a notmuch message-Id. Returns
    None if the message hasn't been embedded yet — enqueue paths use
    this to know there's nothing to enqueue against."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, from_addr, subject FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        r = cur.fetchone()
        if r is None:
            return None
        return {"id": r[0], "from_addr": r[1], "subject": r[2]}


def _claim_one(conn: psycopg.Connection, meta: _MsgMeta,
               model: str, tier: int) -> bool:
    """Atomic claim for a specific (model, tier) pass at current prompt version.
    Returns True iff this caller inserted a fresh `pending` row.

    Priority is computed from the sender by `priority.score()` and
    written into the new row. Tier-1 may later refine it once the body
    has been read; until then the heuristic-floor is the only writer."""
    mrid = meta["id"]
    prio = priority.score(meta.get("from_addr"), meta.get("subject"))
    with conn.cursor() as cur:
        # Reclaim abandoned in-flight rows for this exact pass.
        cur.execute(
            """
            UPDATE summaries
               SET status = 'failed',
                   error  = 'abandoned (reclaim)'
             WHERE message_id = %s
               AND model = %s
               AND prompt_version = %s
               AND status IN ('pending', 'streaming')
               AND updated_at < now() - make_interval(secs => %s)
            """,
            (mrid, model, PROMPT_VERSION, RECLAIM_AFTER_SECONDS),
        )
        # Insert new pending row. The partial unique index
        # `summaries_inflight_lock` prevents two in-flight rows for the
        # same (mid, model, version). Done/failed rows don't conflict.
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status,
                 quality_tier, requested_at, priority)
            VALUES (%s, %s, %s, '', 'pending', %s, now(), %s)
            ON CONFLICT (message_id, model, prompt_version)
            WHERE status IN ('pending', 'streaming') DO NOTHING
            RETURNING id
            """,
            (mrid, model, PROMPT_VERSION, tier, prio),
        )
        claimed = cur.fetchone() is not None
        conn.commit()
        return claimed


def claim_for_generation(conn: psycopg.Connection, notmuch_msg_id: str,
                          *, model: str | None = None,
                          tier: int | None = None) -> bool:
    """Claim a single pass. Defaults to the highest-tier pass (the "final"
    summary). Used by tests + summarize_inbox; the webapp uses
    `schedule_all_passes` instead."""
    meta = _msg_meta(conn, notmuch_msg_id)
    if meta is None:
        return False
    if model is None:
        p = PASS_BY_TIER[MAX_TIER]
        model, tier = p["model"], p["tier"]
    assert tier is not None
    return _claim_one(conn, meta, model, tier)


def reclaim_stale_streaming(conn: psycopg.Connection,
                             max_age_seconds: int = 0) -> int:
    """Flip any `streaming` row older than `max_age_seconds` to `failed`.

    Independent of `model` and `prompt_version` — the per-pass reclaim in
    `_claim_one` only fires when a new claim hits the same (mid, model,
    version), so rows whose prompt version is no longer active (e.g. p2
    after we moved to p5) would otherwise stay stuck forever.

    `updated_at` is bumped to `now()` by the `summaries_touch_updated_at`
    trigger on every UPDATE; for a row currently in `streaming`, that's
    the time the worker claimed it. So `now() - updated_at` is the
    processing age. With `max_age_seconds=0` every streaming row is
    reclaimed (intended for worker startup — any row in `streaming` at
    boot is from a dead previous worker).

    Returns the number of rows reclaimed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries
               SET status = 'failed',
                   error  = 'abandoned (reaper)'
             WHERE status = 'streaming'
               AND updated_at < now() - make_interval(secs => %s)
            """,
            (max_age_seconds,),
        )
        n = cur.rowcount
        conn.commit()
    return n


def schedule_all_passes(conn: psycopg.Connection,
                        notmuch_msg_id: str) -> int:
    """Enqueue every configured pass for this mail. Returns the count of
    NEW claims (0 if all passes already have rows). Idempotent — the
    partial unique index keeps existing in-flight rows from duplicating."""
    meta = _msg_meta(conn, notmuch_msg_id)
    if meta is None:
        return 0
    new = 0
    for p in PASSES:
        if _claim_one(conn, meta, p["model"], p["tier"]):
            new += 1
    return new


class QueueCount(TypedDict):
    tier: int
    label: str
    model: str
    count: int


def queue_counts(conn: psycopg.Connection) -> list[QueueCount]:
    """One row per configured pass with its pending+streaming count.
    Order matches `PASSES` so the topbar indicator can render them
    left-to-right in tier order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT quality_tier, model, count(*)::int
            FROM summaries
            WHERE status IN ('pending', 'streaming')
            GROUP BY quality_tier, model
            """,
        )
        by_key = {(t, m): n for t, m, n in cur.fetchall()}
    out: list[QueueCount] = []
    for p in PASSES:
        out.append({
            "tier": p["tier"],
            "label": p["label"],
            "model": p["model"],
            "count": by_key.get((p["tier"], p["model"]), 0),
        })
    return out


def bump_priority(conn: psycopg.Connection,
                  notmuch_msg_ids: list[str]) -> int:
    """Move any pending rows for these mails to the front of the queue.

    The user opening a mail is a strong signal that overrides the
    heuristic-floor: we promote `priority` to HIGH (never demote — `GREATEST`
    keeps an already-HIGH row at HIGH rather than overwriting) and set
    `requested_at = now()` so the row also wins the tiebreak within the
    HIGH band. Returns rows touched."""
    if not notmuch_msg_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries
               SET priority = GREATEST(priority, %s),
                   requested_at = now()
             WHERE status = 'pending'
               AND message_id IN (
                   SELECT id FROM messages WHERE message_id = ANY(%s)
               )
            """,
            (priority.HIGH, notmuch_msg_ids),
        )
        n = cur.rowcount
        conn.commit()
    return n


# ----------------- generate -----------------

def _terminal(conn: psycopg.Connection, row_id: int, status: str,
              short: str | None = None, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries
               SET status = %s::summary_status,
                   short  = COALESCE(%s, short),
                   error  = %s
             WHERE id = %s
            """,
            (status, short, error, row_id),
        )
        conn.commit()


def _finalize_tier2(conn: psycopg.Connection, row_id: int,
                    payload: dict) -> None:
    """Flip a tier-2 row to `done` and persist all the structured
    side-table data atomically. On any DB error the side-table inserts
    roll back together with the status flip so the queue can retry."""
    short = str(payload.get("short") or "").strip()
    if not short:
        _terminal(conn, row_id, "failed", error="JSON missing 'short'")
        return
    action_required = bool(payload.get("action_required"))
    temporal = payload.get("temporal") or []
    themes = payload.get("themes") or []
    entities = payload.get("entities") or []
    if not isinstance(temporal, list): temporal = []
    if not isinstance(themes, list):   themes = []
    if not isinstance(entities, list): entities = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE summaries
                   SET status = 'done'::summary_status,
                       short  = %s,
                       error  = NULL,
                       action_required = %s
                 WHERE id = %s
                """,
                (short, action_required, row_id),
            )
        extract.insert_temporal(conn, row_id, temporal)
        extract.upsert_themes(conn, row_id, [str(t) for t in themes])
        extract.upsert_entities(conn, row_id, entities)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _terminal(conn, row_id, "failed", error=f"finalize: {e}"[:500])


def generate_and_store(conn: psycopg.Connection, notmuch_msg_id: str,
                       *, model: str | None = None) -> None:
    """Run the LLM for `mid` at the given model (default: highest-tier
    final pass) and flip the matching `pending`/`streaming` row to `done`
    (or `failed`). The caller is expected to have claimed first.

    Tier-1 rows go through the free-text path (`_ollama_chat`) — only the
    `short` column is written. Tier-MAX rows go through the structured
    JSON path (`_ollama_chat_json` + `_finalize_tier2`), which also fills
    `action_required` and the side tables `summary_temporal`,
    `summary_themes`, `summary_entities` (and `themes` / `entities`)."""
    if model is None:
        model = PASS_BY_TIER[MAX_TIER]["model"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.quality_tier, m.subject, m.from_addr,
                   string_agg(c.text, E'\n\n' ORDER BY c.chunk_idx)
            FROM summaries s
            JOIN messages m ON m.id = s.message_id
            LEFT JOIN chunks c
                   ON c.message_id = m.id AND c.attachment_id IS NULL
            WHERE m.message_id = %s
              AND s.model = %s
              AND s.prompt_version = %s
              AND s.status IN ('pending', 'streaming')
            GROUP BY s.id, m.subject, m.from_addr
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (notmuch_msg_id, model, PROMPT_VERSION),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        return
    row_id, tier, subject, from_addr, body = row
    if not body:
        _terminal(conn, row_id, "failed", error="no body chunks")
        return

    structured = (tier == MAX_TIER)
    try:
        if structured:
            payload = _ollama_chat_json(
                model, subject or "", from_addr or "", body,
            )
        else:
            text = _ollama_chat(model, subject or "", from_addr or "", body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _terminal(conn, row_id, "failed", error=str(e)[:500])
        return
    except ValueError as e:
        # Malformed JSON from the structured path.
        _terminal(conn, row_id, "failed", error=str(e)[:500])
        return

    if structured:
        _finalize_tier2(conn, row_id, payload)
    else:
        text = (text or "").strip()
        if not text:
            _terminal(conn, row_id, "failed", error="empty model response")
            return
        _terminal(conn, row_id, "done", short=text)


def generate_and_store_bg(notmuch_msg_id: str,
                          model: str | None = None) -> None:
    """Off-request entry point — opens its own DB connection."""
    with db.connect() as conn:
        generate_and_store(conn, notmuch_msg_id, model=model)
