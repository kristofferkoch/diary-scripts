"""Persist the structured side of a tier-2 summary.

The slow pass returns JSON with summary text + action flag + dates +
themes + entities. This module shreds that JSON into the side tables
defined in migration 007:

  - `themes`           — deduped via bge-m3 nearest-neighbour
  - `summary_themes`   — M2M link to summaries
  - `entities`         — deduped via per-kind `normalized` form
  - `summary_entities` — M2M link to summaries
  - `summary_temporal` — flat per-summary list of dates

Helpers don't commit; the caller in `summarize.py` wraps the whole
finalize step in a single transaction so failures roll back cleanly.
"""
from __future__ import annotations

import json
import re
import sys as _sys
from pathlib import Path as _Path
from typing import Any, TypedDict

import psycopg

# scripts/ isn't a Python package; reuse its embed helper rather than
# duplicating the Ollama call. Matches the pattern in related.py.
_SCRIPTS_DIR = str(_Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
from embed_mail import embed_batch, vec_literal  # noqa: E402


# Two theme strings whose bge-m3 vectors are within this cosine of each
# other are treated as the same concept (reuse the existing row instead
# of inserting). 0.85 is a starting point — tighten if we see false
# merges, loosen if we see "varmekabler" vs "gulvarme" splitting.
THEME_DEDUP_COSINE = 0.85

ENTITY_KINDS = frozenset({
    "person", "org", "place", "money", "identifier", "contact", "url",
})
TEMPORAL_KINDS = frozenset({"deadline", "event", "valid_until", "mentioned"})

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Trailing legal-form noise we strip before dedup so "Hafslund AS" and
# "Hafslund" collapse to one row. Order matters — longest first.
_ORG_SUFFIXES = (
    " asa", " sa", " ab", " ltd", " inc.", " inc", " llc",
    " gmbh", " plc", " kommune", " as",
)


class Temporal(TypedDict):
    kind: str
    occurs_at: str  # ISO YYYY-MM-DD
    note: str | None


class Entity(TypedDict):
    kind: str
    value: str
    meta: dict[str, Any]


# ---------- normalization ----------

def _norm_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def normalize_entity(kind: str, value: str, meta: dict[str, Any]) -> str:
    """Canonical dedup key per kind. Exposed for tests."""
    base = _norm_whitespace(value)
    if not base:
        return ""
    if kind == "org":
        for sfx in _ORG_SUFFIXES:
            if base.endswith(sfx):
                base = base[: -len(sfx)].rstrip()
                break
        return base
    if kind == "money":
        amount = meta.get("amount")
        currency = (meta.get("currency") or "NOK").upper()
        if amount is not None:
            try:
                return f"{float(amount):.2f}{currency}"
            except (TypeError, ValueError):
                pass
        return base
    if kind == "identifier":
        idtype = (meta.get("type") or "").strip().lower()
        digits = re.sub(r"\D", "", value)
        if digits and idtype in ("kid", "account", "order", "tracking"):
            return f"{idtype}:{digits}"
        return f"{idtype}:{base}" if idtype else base
    if kind == "contact":
        method = (meta.get("method") or "").strip().lower()
        if method == "tel":
            digits = re.sub(r"\D", "", value)
            return f"tel:{digits}" if digits else ""
        return f"mail:{base}"
    if kind == "url":
        return base.rstrip("/")
    return base


# ---------- themes ----------

def upsert_themes(conn: psycopg.Connection, summary_id: int,
                  theme_texts: list[str]) -> list[int]:
    """Embed each theme, find-or-create in `themes`, link to summary.

    Dedup is two-stage:
      1. Exact text match (cheap).
      2. bge-m3 cosine ≥ THEME_DEDUP_COSINE against the HNSW index.

    Returns the list of theme_ids attached to this summary. Does NOT
    commit — the caller owns the transaction.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in theme_texts:
        if not isinstance(t, str):
            continue
        s = t.strip()
        key = s.lower()
        if not s or key in seen:
            continue
        cleaned.append(s)
        seen.add(key)
    if not cleaned:
        return []

    vecs = embed_batch(cleaned)
    ids: list[int] = []
    with conn.cursor() as cur:
        for text, vec in zip(cleaned, vecs):
            vlit = vec_literal(vec)

            cur.execute("SELECT id FROM themes WHERE text = %s", (text,))
            r = cur.fetchone()
            if r is not None:
                ids.append(r[0])
                continue

            # Nearest neighbour. `<=>` is cosine distance in pgvector
            # (0 = identical, 1 = orthogonal). Similarity = 1 - distance.
            cur.execute(
                """
                SELECT id, 1 - (embedding <=> %s::vector) AS sim
                FROM themes
                ORDER BY embedding <=> %s::vector
                LIMIT 1
                """,
                (vlit, vlit),
            )
            nn = cur.fetchone()
            if nn is not None and float(nn[1]) >= THEME_DEDUP_COSINE:
                ids.append(nn[0])
                continue

            cur.execute(
                """
                INSERT INTO themes (text, embedding)
                VALUES (%s, %s::vector)
                ON CONFLICT (text) DO UPDATE
                    SET text = themes.text  -- no-op, lets RETURNING fire
                RETURNING id
                """,
                (text, vlit),
            )
            ins = cur.fetchone()
            assert ins is not None
            ids.append(ins[0])

        if ids:
            cur.executemany(
                """
                INSERT INTO summary_themes (summary_id, theme_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                [(summary_id, tid) for tid in ids],
            )
    return ids


# ---------- entities ----------

def upsert_entities(conn: psycopg.Connection, summary_id: int,
                    items: list[Entity]) -> list[int]:
    """Find-or-create each entity by (kind, normalized); link to summary.

    Unknown kinds, empty values, and entities whose normalized form
    collapses to "" are silently skipped — the LLM is free to be sloppy.
    Does NOT commit.
    """
    ids: list[int] = []
    with conn.cursor() as cur:
        for ent in items or []:
            if not isinstance(ent, dict):
                continue
            kind = str(ent.get("kind") or "").strip()
            value = str(ent.get("value") or "").strip()
            meta = ent.get("meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            if kind not in ENTITY_KINDS or not value:
                continue
            normalized = normalize_entity(kind, value, meta)
            if not normalized:
                continue

            cur.execute(
                """
                INSERT INTO entities (kind, value, normalized, meta)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (kind, normalized) DO UPDATE
                    SET kind = entities.kind  -- no-op, lets RETURNING fire
                RETURNING id
                """,
                (kind, value, normalized, json.dumps(meta)),
            )
            r = cur.fetchone()
            if r is not None:
                ids.append(r[0])

        if ids:
            cur.executemany(
                """
                INSERT INTO summary_entities (summary_id, entity_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                [(summary_id, eid) for eid in ids],
            )
    return ids


# ---------- temporal ----------

def insert_temporal(conn: psycopg.Connection, summary_id: int,
                    items: list[Temporal]) -> int:
    """Insert each valid temporal row. Skips rows whose `occurs_at`
    isn't a literal ISO date so a single mangled date doesn't blow up
    the whole transaction. Returns rows inserted. Does NOT commit.
    """
    inserted = 0
    with conn.cursor() as cur:
        for t in items or []:
            if not isinstance(t, dict):
                continue
            kind = str(t.get("kind") or "").strip()
            occurs_at = str(t.get("occurs_at") or "").strip()
            note_raw = t.get("note")
            note = str(note_raw).strip() if note_raw else None
            if kind not in TEMPORAL_KINDS:
                continue
            if not _ISO_DATE.match(occurs_at):
                continue
            cur.execute(
                """
                INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
                VALUES (%s, %s, %s::date, %s)
                ON CONFLICT DO NOTHING
                """,
                (summary_id, kind, occurs_at, note),
            )
            if cur.rowcount:
                inserted += 1
    return inserted
