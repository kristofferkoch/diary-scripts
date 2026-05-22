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
    thread_id = row[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT message_id
            FROM messages
            WHERE thread_id = %s
            ORDER BY date DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        expected_row = cur.fetchone()
    assert expected_row is not None
    expected_mid = expected_row[0]

    result = inbox.thread_latest_mids(conn, [thread_id])
    assert result == {thread_id: expected_mid}


def test_thread_latest_mids_omits_unknown_threads(conn):
    """Threads not present in `messages` are omitted from the result
    (not represented as None) so callers see the absent ones clearly."""
    result = inbox.thread_latest_mids(
        conn, ["definitely-not-a-real-thread-id"],
    )
    assert result == {}


def test_thread_latest_mids_empty_input_returns_empty(conn):
    assert inbox.thread_latest_mids(conn, []) == {}


def test_thread_latest_mids_accepts_unprefixed_notmuch_form(conn):
    """Regression: notmuch search emits `00000000000348fc` (bare hex);
    the DB stores `thread:00000000000348fc`. Inbox passed bare IDs and
    got no matches back → every inbox row rendered without a summary
    card. The helper now normalizes either form."""
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
    result = inbox.thread_latest_mids(conn, [bare])
    # Caller-provided form is the key in the returned dict.
    assert bare in result, (
        f"expected unprefixed key {bare!r}, got keys {list(result)!r}"
    )
