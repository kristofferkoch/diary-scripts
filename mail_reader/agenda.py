"""Upcoming dated items extracted from mail bodies (tier-2 structured pass).

The agenda strip on the inbox page renders deadlines, events, and
valid-until dates from `summary_temporal` so user can glance at "what
do I owe this week" without paging through the inbox.

Dedup matters: the same fliser-bestillingsfrist gets extracted from
every reply in a thread. We collapse on `(thread_id, kind, occurs_at)`
and keep the row from the most-recent message in that thread — that
row's summary text is typically the most refined.

`mentioned` kind is filtered out: by the migration's own definition
it's "generic date reference, no action implied" — not agenda fodder.
"""
from __future__ import annotations

import datetime
from typing import TypedDict

import psycopg

from . import summarize
from .thread_id import ThreadId


class AgendaItem(TypedDict):
    occurs_at: datetime.date
    kind: str            # 'deadline' | 'event' | 'valid_until'
    note: str | None
    subject: str
    thread_id: ThreadId
    message_id: str
    summary: str | None  # the `short` summary of the source mail


def list_upcoming(conn: psycopg.Connection, days: int = 14) -> list[AgendaItem]:
    """Items from today through today+`days`, sorted by date then kind.

    Filters to the current `PROMPT_VERSION` so a stale-prompt summary
    doesn't pollute the strip while a regen is in flight. Drops the
    `mentioned` kind. Dedupes by `(thread_id, kind, occurs_at)`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH dedup AS (
                SELECT DISTINCT ON (m.thread_id, st.kind, st.occurs_at)
                    st.occurs_at, st.kind, st.note,
                    m.subject, m.thread_id, m.message_id, s.short
                FROM summary_temporal st
                JOIN summaries s ON s.id = st.summary_id
                JOIN messages   m ON m.id = s.message_id
                WHERE st.occurs_at >= current_date
                  AND st.occurs_at <  current_date + make_interval(days => %s)
                  AND st.kind <> 'mentioned'
                  AND s.status = 'done'
                  AND s.prompt_version = %s
                ORDER BY m.thread_id, st.kind, st.occurs_at, m.date DESC
            )
            SELECT occurs_at, kind, note, subject, thread_id, message_id, short
            FROM dedup
            ORDER BY occurs_at ASC, kind ASC
            """,
            (days, summarize.PROMPT_VERSION),
        )
        rows = cur.fetchall()
    return [
        AgendaItem(
            occurs_at=r[0],
            kind=r[1],
            note=r[2],
            subject=r[3],
            # DB stores the prefixed form; ThreadId normalizes to bare
            # so URL building and notmuch queries always get the right
            # shape — see mail_reader/thread_id.py.
            thread_id=ThreadId(r[4]),
            message_id=r[5],
            summary=r[6],
        )
        for r in rows
    ]
