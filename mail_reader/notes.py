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

from typing import TYPE_CHECKING, NotRequired, TypedDict

import psycopg
from psycopg.rows import dict_row

if TYPE_CHECKING:  # only for the type hint — keep this module Pillow-free
    from .note_images import ProcessedImage


class Note(TypedDict):
    id: int
    body: str
    status: str
    created_at: object  # datetime; kept opaque so this module needn't import it
    updated_at: object
    # First image attached to this note, if any. Populated by list_pending/get
    # (which resolve it in one query); the mutation helpers don't return it.
    # gps_lat/gps_lon ride along so the page can show a map link without a
    # second round trip; both None when the photo carried no location.
    attachment_id: NotRequired[int | None]
    gps_lat: NotRequired[float | None]
    gps_lon: NotRequired[float | None]


class Attachment(TypedDict):
    id: int
    note_id: int
    mime_type: str
    width: int | None
    height: int | None
    description: str | None
    description_model: str | None
    described_at: object
    gps_lat: float | None
    gps_lon: float | None
    created_at: object


# Notes are listed/fetched with their (optional) first attachment id resolved
# in one query, so the template can render a thumbnail without a second round
# trip. A note may legitimately have an image but no text (capture from a
# phone), hence resolving the attachment independently of the body.
_NOTE_COLS = (
    "n.id, n.body, n.status, n.created_at, n.updated_at, "
    "a.id AS attachment_id, a.gps_lat, a.gps_lon"
)
_NOTE_FROM = (
    "FROM notes_queue n "
    "LEFT JOIN LATERAL ("
    "  SELECT id, gps_lat, gps_lon FROM note_attachments "
    "  WHERE note_id = n.id ORDER BY id LIMIT 1"
    ") a ON true"
)


def list_pending(conn: psycopg.Connection, limit: int = 200) -> list[Note]:
    """Pending notes, newest first — what the page and the sjekk see."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_NOTE_COLS} {_NOTE_FROM} "
            "WHERE n.status = 'pending' "
            "ORDER BY n.created_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()  # type: ignore[return-value]


def get(conn: psycopg.Connection, note_id: int) -> Note | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_NOTE_COLS} {_NOTE_FROM} WHERE n.id = %s",
            (note_id,),
        )
        return cur.fetchone()  # type: ignore[return-value]


def add(conn: psycopg.Connection, body: str, *, allow_empty: bool = False) -> Note:
    """Insert a pending note. Raises ValueError on empty/whitespace body
    unless ``allow_empty`` — used when the note carries only an image, so the
    text can legitimately be blank."""
    body = body.strip()
    if not body and not allow_empty:
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
    """Hard-delete a note. Raises KeyError if it didn't exist. The note's
    attachments go with it via ON DELETE CASCADE."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notes_queue WHERE id = %s", (note_id,))
        if cur.rowcount == 0:
            raise KeyError(note_id)
    conn.commit()


# --- attachments -----------------------------------------------------------
# Images attached to a note. Bytes processed by note_images.process() before
# they reach here (this module stays Pillow-free); the description columns are
# filled in later by a vision-model pass via set_description().


def add_attachment(
    conn: psycopg.Connection, note_id: int, image: ProcessedImage
) -> int:
    """Store a processed image for a note; returns the new attachment id.

    Propagates a foreign-key violation if ``note_id`` doesn't exist — the
    caller (server) always inserts the note first, so this can't happen
    in normal flow and a raise is the honest signal if it does."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO note_attachments "
            "(note_id, mime_type, image_bytes, thumb_bytes, width, height, "
            "gps_lat, gps_lon) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                note_id,
                image.mime_type,
                image.image_bytes,
                image.thumb_bytes,
                image.width,
                image.height,
                image.gps_lat,
                image.gps_lon,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields a row
        attachment_id = row[0]
    conn.commit()
    return attachment_id


def get_attachment(conn: psycopg.Connection, attachment_id: int) -> Attachment | None:
    """Attachment metadata (no bytes) — what the CLI/sjekk reads."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, note_id, mime_type, width, height, "
            "description, description_model, described_at, "
            "gps_lat, gps_lon, created_at "
            "FROM note_attachments WHERE id = %s",
            (attachment_id,),
        )
        return cur.fetchone()  # type: ignore[return-value]


def get_attachment_blob(
    conn: psycopg.Connection, attachment_id: int, *, thumb: bool = False
) -> tuple[str, bytes] | None:
    """Return ``(mime_type, bytes)`` for one attachment, or None if missing.

    Selects only the requested blob column so serving a thumbnail never
    loads the full-size image."""
    column = "thumb_bytes" if thumb else "image_bytes"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT mime_type, {column} FROM note_attachments WHERE id = %s",
            (attachment_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], bytes(row[1])


def set_description(
    conn: psycopg.Connection, attachment_id: int, description: str, model: str
) -> None:
    """Record a vision model's interpretation of the image, versioned with
    the model id and the current time. Raises KeyError if no such attachment."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE note_attachments "
            "SET description = %s, description_model = %s, described_at = now() "
            "WHERE id = %s",
            (description, model, attachment_id),
        )
        if cur.rowcount == 0:
            raise KeyError(attachment_id)
    conn.commit()
