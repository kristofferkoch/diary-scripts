"""Tests for mail_reader.inbox.

Covers the `thread_latest_mids` batch lookup that powers the inbox-row
summary feature: one SQL round-trip resolves the newest embedded
message-id for every visible thread.
"""
from __future__ import annotations

import pytest

from mail_reader import inbox
from mail_reader.thread_id import ThreadId


@pytest.fixture
def conn(db_conn):
    return db_conn


@pytest.fixture
def message_thread(conn):
    tid = ThreadId("pytest-inbox")
    with conn.cursor() as cur:
        for mid, date in (
            ("old@example.invalid", "2026-01-01T12:00:00Z"),
            ("new@example.invalid", "2026-01-02T12:00:00Z"),
        ):
            cur.execute(
                "INSERT INTO messages (message_id, thread_id, date, tier) "
                "VALUES (%s, %s, %s, 2)",
                (mid, tid.db_form, date),
            )
    return tid


def test_thread_latest_mids_returns_newest_per_thread(conn, message_thread):
    result = inbox.thread_latest_mids(conn, [message_thread])
    assert result == {message_thread: "new@example.invalid"}


def test_thread_latest_mids_omits_unknown_threads(conn):
    """Threads not present in `messages` are omitted from the result
    (not represented as None) so callers see the absent ones clearly."""
    result = inbox.thread_latest_mids(
        conn, [ThreadId("definitely-not-a-real-thread-id")],
    )
    assert result == {}


def test_thread_latest_mids_empty_input_returns_empty(conn):
    assert inbox.thread_latest_mids(conn, []) == {}


def test_thread_latest_mids_accepts_either_form_via_threadid(conn, message_thread):
    """Bare and prefixed inputs normalize to the same database key."""
    from_bare = inbox.thread_latest_mids(conn, [ThreadId(str(message_thread))])
    from_prefixed = inbox.thread_latest_mids(conn, [ThreadId(message_thread.db_form)])
    assert from_bare == from_prefixed == {message_thread: "new@example.invalid"}
