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
