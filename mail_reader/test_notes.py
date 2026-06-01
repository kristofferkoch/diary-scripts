"""Tests for mail_reader.notes — the notes-queue CRUD.

Hits the real mailvec DB (skips if unreachable, same as the other
DB-backed tests). Every note created here carries a unique marker in its
body; the `cleanup` fixture hard-deletes all marker rows on teardown so
the queue isn't polluted regardless of which assertions fired.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader import notes
from mail_reader.note_images import ProcessedImage


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
MARKER = "[[test_notes-marker]]"


def _dsn_reachable() -> bool:
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _dsn_reachable(),
    reason=f"mailvec DB not reachable at PG_DSN={PG_DSN!r}",
)


@pytest.fixture
def conn():
    with psycopg.connect(PG_DSN) as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup(conn):
    yield
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notes_queue WHERE body LIKE %s", (f"%{MARKER}%",))
    conn.commit()


def _mk(conn, text: str) -> dict:
    return notes.add(conn, f"{text} {MARKER}")


def test_add_returns_pending_row(conn):
    note = _mk(conn, "kjøp melk")
    assert note["status"] == "pending"
    assert note["body"].startswith("kjøp melk")
    assert note["id"] > 0
    assert note["created_at"] == note["updated_at"]


def test_add_strips_and_rejects_empty(conn):
    note = notes.add(conn, f"  trimmet {MARKER}  ")
    # leading/trailing whitespace is stripped; interior is preserved
    assert note["body"] == f"trimmet {MARKER}"
    with pytest.raises(ValueError):
        notes.add(conn, "   ")


def test_list_pending_newest_first_excludes_done(conn):
    a = _mk(conn, "eldst")
    b = _mk(conn, "nyest")
    notes.set_status(conn, a["id"], "done")
    listed = notes.list_pending(conn)
    ids = [n["id"] for n in listed]
    assert b["id"] in ids
    assert a["id"] not in ids  # done notes drop off
    # newest-first: b should appear before any older pending marker note
    assert ids.index(b["id"]) == 0 or all(
        listed[i]["created_at"] >= listed[i + 1]["created_at"]
        for i in range(len(listed) - 1)
    )


def test_update_body_changes_text_and_bumps_updated_at(conn):
    note = _mk(conn, "før")
    edited = notes.update_body(conn, note["id"], f"etter {MARKER}")
    assert edited["body"] == f"etter {MARKER}"
    assert edited["updated_at"] >= edited["created_at"]


def test_update_body_rejects_empty(conn):
    note = _mk(conn, "noe")
    with pytest.raises(ValueError):
        notes.update_body(conn, note["id"], "   ")


def test_update_missing_raises_keyerror(conn):
    with pytest.raises(KeyError):
        notes.update_body(conn, 2_000_000_001, "x")


def test_set_status_roundtrip_and_bad_status(conn):
    note = _mk(conn, "status")
    done = notes.set_status(conn, note["id"], "done")
    assert done["status"] == "done"
    back = notes.set_status(conn, note["id"], "pending")
    assert back["status"] == "pending"
    with pytest.raises(ValueError):
        notes.set_status(conn, note["id"], "garbage")


def test_set_status_missing_raises_keyerror(conn):
    with pytest.raises(KeyError):
        notes.set_status(conn, 2_000_000_002, "done")


def test_delete_removes_and_missing_raises(conn):
    note = _mk(conn, "slett meg")
    notes.delete(conn, note["id"])
    assert notes.get(conn, note["id"]) is None
    with pytest.raises(KeyError):
        notes.delete(conn, note["id"])


def test_get_returns_none_for_missing(conn):
    assert notes.get(conn, 2_000_000_003) is None


# --- attachments -----------------------------------------------------------


def _img(tag: bytes = b"") -> ProcessedImage:
    return ProcessedImage(
        mime_type="image/jpeg",
        image_bytes=b"WEB" + tag,
        thumb_bytes=b"THUMB" + tag,
        width=800,
        height=600,
    )


def test_add_allows_empty_body_only_when_asked(conn):
    note = notes.add(conn, f"  {MARKER}  ", allow_empty=True)
    assert note["body"] == MARKER  # marker survives so cleanup reaps it
    # an image-only note can be truly blank when allow_empty is set
    blank = notes.add(conn, "   ", allow_empty=True)
    try:
        assert blank["body"] == ""
    finally:
        notes.delete(conn, blank["id"])
    # …but the default still rejects an empty body
    with pytest.raises(ValueError):
        notes.add(conn, "   ")


def test_add_attachment_stores_and_serves_blobs(conn):
    note = _mk(conn, "med bilde")
    att_id = notes.add_attachment(conn, note["id"], _img(b"-A"))
    assert att_id > 0

    full = notes.get_attachment_blob(conn, att_id)
    thumb = notes.get_attachment_blob(conn, att_id, thumb=True)
    assert full == ("image/jpeg", b"WEB-A")
    assert thumb == ("image/jpeg", b"THUMB-A")

    meta = notes.get_attachment(conn, att_id)
    assert meta["note_id"] == note["id"]
    assert (meta["width"], meta["height"]) == (800, 600)
    assert meta["description"] is None and meta["described_at"] is None


def test_get_attachment_blob_missing_returns_none(conn):
    assert notes.get_attachment_blob(conn, 2_000_000_004) is None


def test_attachment_id_surfaced_in_get_and_list(conn):
    note = _mk(conn, "synlig vedlegg")
    assert notes.get(conn, note["id"])["attachment_id"] is None  # none yet
    att_id = notes.add_attachment(conn, note["id"], _img())
    assert notes.get(conn, note["id"])["attachment_id"] == att_id
    listed = {n["id"]: n for n in notes.list_pending(conn)}
    assert listed[note["id"]]["attachment_id"] == att_id


def test_image_only_note_has_blank_body_and_attachment(conn):
    note = notes.add(conn, "", allow_empty=True)
    try:
        att_id = notes.add_attachment(conn, note["id"], _img())
        fetched = notes.get(conn, note["id"])
        assert fetched["body"] == ""
        assert fetched["attachment_id"] == att_id
    finally:
        notes.delete(conn, note["id"])


def test_set_description_versions_with_model_and_time(conn):
    note = _mk(conn, "beskriv meg")
    att_id = notes.add_attachment(conn, note["id"], _img())
    notes.set_description(conn, att_id, "et bilde av en katt", "qwen2.5vl:7b")
    meta = notes.get_attachment(conn, att_id)
    assert meta["description"] == "et bilde av en katt"
    assert meta["description_model"] == "qwen2.5vl:7b"
    assert meta["described_at"] is not None
    with pytest.raises(KeyError):
        notes.set_description(conn, 2_000_000_005, "x", "m")


def test_deleting_note_cascades_attachment(conn):
    note = _mk(conn, "slett med bilde")
    att_id = notes.add_attachment(conn, note["id"], _img())
    notes.delete(conn, note["id"])
    assert notes.get_attachment_blob(conn, att_id) is None
