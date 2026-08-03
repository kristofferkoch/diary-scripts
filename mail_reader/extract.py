"""Persist the structured side of a tier-2 summary.

The slow pass returns JSON with summary text + action flag + dates +
themes + entities. This module shreds that JSON into the side tables
defined in migration 007:

  - `entities`         — deduped via per-kind `normalized` form
  - `summary_entities` — M2M link to summaries
  - `summary_temporal` — flat per-summary list of dates

(The `themes` / `summary_themes` side was retired 2026-08-02 together with
summary generation — its dedup needed a live embedding call, and no new
summaries means no new themes. The tables stay in place for existing rows.)

Helpers don't commit; the caller owns the transaction. (Their original
caller — `summarize.py`'s tier-2 finalize — was removed with summary
generation; the helpers stay for the existing rows and any future
extraction pass.)
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

import psycopg


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
