"""Notes queue — capture inbox for "things I don't want to forget".

Backs the /notes/ page (see server.py) and the sjekk-side CLI
(scripts/notes.py). A note is one free-text row in `notes_queue`; the
page shows pending rows newest-first with inline edit + delete. A check
round digests pending notes and marks them done.

Thin CRUD over Postgres — no ORM, parameterised SQL only. Offensive by
design: callers pass a real id; a missing row on update/delete raises
rather than silently no-op'ing, so the UI/CLI can surface a 404.
"""
from __future__ import annotations

from typing import TypedDict

import psycopg
from psycopg.rows import dict_row


class Note(TypedDict):
    id: int
    body: str
    status: str
    created_at: object  # datetime; kept opaque so this module needn't import it
    updated_at: object


def list_pending(conn: psycopg.Connection, limit: int = 200) -> list[Note]:
    """Pending notes, newest first — what the page and the sjekk see."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, body, status, created_at, updated_at "
            "FROM notes_queue WHERE status = 'pending' "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()  # type: ignore[return-value]


def get(conn: psycopg.Connection, note_id: int) -> Note | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, body, status, created_at, updated_at "
            "FROM notes_queue WHERE id = %s",
            (note_id,),
        )
        return cur.fetchone()  # type: ignore[return-value]


def add(conn: psycopg.Connection, body: str) -> Note:
    """Insert a pending note. Raises ValueError on empty/whitespace body."""
    body = body.strip()
    if not body:
        raise ValueError("note body is empty")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO notes_queue (body) VALUES (%s) "
            "RETURNING id, body, status, created_at, updated_at",
            (body,),
        )
        row = cur.fetchone()
    conn.commit()
    return row  # type: ignore[return-value]


def update_body(conn: psycopg.Connection, note_id: int, body: str) -> Note:
    """Edit a note's text. Raises ValueError on empty body, KeyError if no
    such note."""
    body = body.strip()
    if not body:
        raise ValueError("note body is empty")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "UPDATE notes_queue SET body = %s, updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, body, status, created_at, updated_at",
            (body, note_id),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(note_id)
    conn.commit()
    return row  # type: ignore[return-value]


def set_status(conn: psycopg.Connection, note_id: int, status: str) -> Note:
    """Move a note between 'pending' and 'done'. Raises on bad status or
    missing row."""
    if status not in ("pending", "done"):
        raise ValueError(f"bad status: {status!r}")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "UPDATE notes_queue SET status = %s, updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, body, status, created_at, updated_at",
            (status, note_id),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(note_id)
    conn.commit()
    return row  # type: ignore[return-value]


def delete(conn: psycopg.Connection, note_id: int) -> None:
    """Hard-delete a note. Raises KeyError if it didn't exist."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notes_queue WHERE id = %s", (note_id,))
        if cur.rowcount == 0:
            raise KeyError(note_id)
    conn.commit()
