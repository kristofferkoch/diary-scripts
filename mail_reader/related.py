"""Tankekart: top-K semantically similar messages.

Strategy: mean-pool the open message's body chunk embeddings into one
query vector, then nearest-neighbour search across `chunks` (body chunks
only, attachments excluded), grouping by message and taking each
message's best match. Exclude the same message and the same thread —
the thread is already in arm's reach from the detail page.
"""
from __future__ import annotations

from datetime import datetime
from typing import TypedDict

import psycopg


class Related(TypedDict):
    message_id: str       # notmuch Message-ID (no <>)
    date: datetime | None
    from_addr: str
    subject: str
    distance: float
    similarity: float     # 1 - distance, clamped to [0, 1]
    summary: str | None   # populated lazily in server.py; None until cached


def tankekart(conn: psycopg.Connection, notmuch_msg_id: str,
              k: int = 10) -> list[Related]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, thread_id FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        row = cur.fetchone()
        if row is None:
            return []
        msg_row_id, thread_id = row

        # pgvector supports `avg(vector)` aggregate.
        cur.execute(
            "SELECT avg(embedding)::vector AS mean "
            "FROM chunks WHERE message_id = %s AND attachment_id IS NULL",
            (msg_row_id,),
        )
        mean_row = cur.fetchone()
        if mean_row is None or mean_row[0] is None:
            return []
        query_vec = mean_row[0]

        # The `%s IS NULL` placeholder has no type context, and psycopg sends
        # parameters without type hints, so Postgres' prepared-statement
        # planner refuses with `IndeterminateDatatype: could not determine
        # data type of parameter $3`. Pin the type with explicit `::text`
        # casts. (Regression covered by test_related.py.)
        cur.execute(
            """
            SELECT m.message_id, m.date, m.from_addr, m.subject,
                   MIN(c.embedding <=> %s::vector) AS dist
            FROM chunks c
            JOIN messages m ON m.id = c.message_id
            WHERE c.attachment_id IS NULL
              AND m.id <> %s
              AND (m.thread_id IS NULL
                   OR %s::text IS NULL
                   OR m.thread_id <> %s::text)
            GROUP BY m.id, m.message_id, m.date, m.from_addr, m.subject
            ORDER BY dist ASC
            LIMIT %s
            """,
            (query_vec, msg_row_id, thread_id, thread_id, k),
        )
        out: list[Related] = []
        for mid, date, from_addr, subject, dist in cur.fetchall():
            d = float(dist)
            out.append({
                "message_id": mid,
                "date": date,
                "from_addr": from_addr or "",
                "subject": subject or "",
                "distance": d,
                "similarity": max(0.0, min(1.0, 1.0 - d)),
                "summary": None,
            })
        return out
