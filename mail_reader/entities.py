"""Entity chips on the message view + entity detail page.

Tier-2's structured pass populates `entities` (one row per typed value)
and links them to summaries via `summary_entities`. Two surfaces here:

* `chips_for_message(conn, notmuch_msg_id)` returns the entity chips to
  render in the open mail's header (the rendered Gmail-ish strip under
  the To line). URLs are filtered out — they're rarely useful as
  pivots and would crowd the strip.
* `messages_for_entity(conn, entity_id)` powers `/e/{id}` and lists,
  in reverse chronological order and deduped by thread, every mail
  whose tier-2 summary mentioned the entity.

Both functions filter to the current `summarize.PROMPT_VERSION` so an
older-prompt extraction doesn't bleed in while a regen is in flight.
"""
from __future__ import annotations

from datetime import datetime
from typing import TypedDict

import psycopg

from . import summarize
from .thread_id import ThreadId


# Display order. Anything outside this list is appended at the end.
KIND_ORDER = ("person", "org", "place", "money", "identifier", "contact")
HIDDEN_KINDS = frozenset({"url"})  # noisy as chips; reachable via the body


class Chip(TypedDict):
    id: int
    kind: str
    value: str
    label: str  # how it should render on the chip


class EntityRow(TypedDict):
    id: int
    kind: str
    value: str


class EntityMessage(TypedDict):
    message_id: str
    thread_id: ThreadId | None
    subject: str
    from_addr: str
    date: datetime | None
    summary: str | None
    summary_status: str
    summary_action_required: bool


def _label(kind: str, value: str, meta: dict) -> str:
    """Render-side formatting per kind. Stays small and predictable;
    no LLM, no locale lookup."""
    if kind == "identifier":
        idtype = (meta or {}).get("type")
        if idtype:
            return f"{idtype.upper()}: {value}"
        return value
    if kind == "contact":
        method = (meta or {}).get("method")
        if method == "tel":
            return f"tlf {value}"
        return value
    return value


def chips_for_message(conn: psycopg.Connection,
                      notmuch_msg_id: str,
                      max_chips: int = 12) -> list[Chip]:
    """Entity chips for the open mail's header strip. Sourced from the
    best done tier-2 summary at the current prompt version. Returns up
    to `max_chips` entries; ordered by KIND_ORDER then alphabetically
    on value."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (e.id) e.id, e.kind, e.value, e.meta
            FROM summaries s
            JOIN summary_entities se ON se.summary_id = s.id
            JOIN entities e ON e.id = se.entity_id
            JOIN messages m ON m.id = s.message_id
            WHERE m.message_id = %s
              AND s.prompt_version = %s
              AND s.status = 'done'
            """,
            (notmuch_msg_id, summarize.PROMPT_VERSION),
        )
        rows = cur.fetchall()

    def sort_key(r) -> tuple[int, str]:
        kind = r[1]
        try:
            ki = KIND_ORDER.index(kind)
        except ValueError:
            ki = len(KIND_ORDER)
        return (ki, (r[2] or "").lower())

    chips: list[Chip] = []
    for eid, kind, value, meta in sorted(rows, key=sort_key):
        if kind in HIDDEN_KINDS:
            continue
        chips.append({
            "id": eid,
            "kind": kind,
            "value": value,
            "label": _label(kind, value, meta or {}),
        })
        if len(chips) >= max_chips:
            break
    return chips


def entity_by_id(conn: psycopg.Connection, entity_id: int) -> EntityRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, value FROM entities WHERE id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "kind": row[1], "value": row[2]}


def messages_for_entity(conn: psycopg.Connection, entity_id: int,
                        limit: int = 50) -> list[EntityMessage]:
    """One row per thread, the most recent message in the thread that
    mentions the entity. Includes the summary text so the entity page
    can render mail-like rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH hits AS (
                SELECT m.message_id, m.thread_id, m.subject, m.from_addr,
                       m.date, s.short, s.status::text AS sum_status,
                       s.action_required, s.quality_tier,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(m.thread_id, m.message_id)
                           ORDER BY m.date DESC NULLS LAST,
                                    s.quality_tier DESC
                       ) AS rn
                FROM summary_entities se
                JOIN summaries s ON s.id = se.summary_id
                                AND s.status = 'done'
                                AND s.prompt_version = %s
                JOIN messages m ON m.id = s.message_id
                WHERE se.entity_id = %s
            )
            SELECT message_id, thread_id, subject, from_addr, date,
                   short, sum_status, action_required
            FROM hits
            WHERE rn = 1
            ORDER BY date DESC NULLS LAST
            LIMIT %s
            """,
            (summarize.PROMPT_VERSION, entity_id, limit),
        )
        rows = cur.fetchall()
    out: list[EntityMessage] = []
    for mid, tid, subject, from_addr, date, short, sum_status, action in rows:
        out.append({
            "message_id": mid,
            "thread_id": ThreadId(tid) if tid else None,
            "subject": subject or "",
            "from_addr": from_addr or "",
            "date": date,
            "summary": short,
            "summary_status": sum_status,
            "summary_action_required": bool(action),
        })
    return out
