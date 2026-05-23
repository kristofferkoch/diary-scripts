"""Tests for mail_reader.inbox.

Covers the `thread_latest_mids` batch lookup that powers the inbox-row
summary feature: one SQL round-trip resolves the newest embedded
message-id for every visible thread.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader import inbox
from mail_reader.thread_id import ThreadId


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")


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


def test_thread_latest_mids_returns_newest_per_thread(conn):
    """Pick a real thread with multiple embedded messages and verify the
    helper returns the newest one's message_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT thread_id
            FROM messages
            WHERE thread_id IS NOT NULL
            GROUP BY thread_id
            HAVING count(*) >= 2
            ORDER BY thread_id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no multi-message thread in DB")
    db_thread_id = row[0]
    tid = ThreadId(db_thread_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT message_id
            FROM messages
            WHERE thread_id = %s
            ORDER BY date DESC
            LIMIT 1
            """,
            (db_thread_id,),
        )
        expected_row = cur.fetchone()
    assert expected_row is not None
    expected_mid = expected_row[0]

    result = inbox.thread_latest_mids(conn, [tid])
    assert result == {tid: expected_mid}


def test_thread_latest_mids_omits_unknown_threads(conn):
    """Threads not present in `messages` are omitted from the result
    (not represented as None) so callers see the absent ones clearly."""
    result = inbox.thread_latest_mids(
        conn, [ThreadId("definitely-not-a-real-thread-id")],
    )
    assert result == {}


def test_thread_latest_mids_empty_input_returns_empty(conn):
    assert inbox.thread_latest_mids(conn, []) == {}


def test_thread_latest_mids_accepts_either_form_via_threadid(conn):
    """ThreadId normalizes both forms (bare from notmuch, prefixed from
    DB) at construction time. The helper takes ThreadId values now —
    callers don't need to think about the form."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT thread_id
            FROM messages
            WHERE thread_id IS NOT NULL
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no embedded message with thread_id")
    prefixed = row[0]
    assert prefixed.startswith("thread:")
    bare = prefixed[len("thread:"):]
    # Both inputs normalize to the same ThreadId.
    from_bare = inbox.thread_latest_mids(conn, [ThreadId(bare)])
    from_prefixed = inbox.thread_latest_mids(conn, [ThreadId(prefixed)])
    assert from_bare and from_prefixed
    assert list(from_bare.values()) == list(from_prefixed.values())
