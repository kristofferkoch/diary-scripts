"""Tests for mail_reader.entities.

Integration: hits the real `mailvec` DB. Skipped when unreachable, same
pattern as test_related / test_summarize.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader import entities, summarize


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


def _pick_message_with_entities(conn: psycopg.Connection) -> str | None:
    """A mail with ≥1 tier-2 entity at the current prompt_version."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.message_id
            FROM messages m
            JOIN summaries s ON s.message_id = m.id
                            AND s.prompt_version = %s
                            AND s.status = 'done'
            JOIN summary_entities se ON se.summary_id = s.id
            GROUP BY m.id, m.message_id
            HAVING COUNT(*) >= 1
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (summarize.PROMPT_VERSION,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_chips_for_message_returns_chips(conn):
    mid = _pick_message_with_entities(conn)
    if mid is None:
        pytest.skip("no message with tier-2 entities at current prompt_version")
    chips = entities.chips_for_message(conn, mid)
    assert isinstance(chips, list)
    # Every chip is well-formed and non-empty.
    for c in chips:
        assert c["id"] > 0
        assert c["kind"]
        assert c["value"]
        assert c["label"]
        # url chips must not appear — we filter those out.
        assert c["kind"] != "url"


def test_chips_unknown_message_returns_empty(conn):
    """An unknown notmuch id should produce no chips, not raise."""
    chips = entities.chips_for_message(conn, "no-such@example.invalid")
    assert chips == []


def test_entity_by_id_round_trip(conn):
    """An entity row that exists should round-trip; non-existent → None."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, value FROM entities ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no entities in DB")
    eid, kind, value = row
    ent = entities.entity_by_id(conn, eid)
    assert ent is not None
    assert ent["id"] == eid
    assert ent["kind"] == kind
    assert ent["value"] == value
    assert entities.entity_by_id(conn, -1) is None


def test_messages_for_entity_dedups_by_thread(conn):
    """For an entity mentioned in several mails of the same thread,
    messages_for_entity returns one row per thread (latest mention)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id
            FROM entities e
            JOIN summary_entities se ON se.entity_id = e.id
            JOIN summaries s ON s.id = se.summary_id
                            AND s.status = 'done'
                            AND s.prompt_version = %s
            JOIN messages m ON m.id = s.message_id
            GROUP BY e.id
            HAVING COUNT(DISTINCT COALESCE(m.thread_id, m.message_id)) >= 1
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """,
            (summarize.PROMPT_VERSION,),
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no entity with mentions at current prompt_version")
    rows = entities.messages_for_entity(conn, row[0], limit=20)
    seen_threads: set[str] = set()
    for r in rows:
        key = r["thread_id"].db_form if r["thread_id"] else r["message_id"]
        assert key not in seen_threads, "thread should appear at most once"
        seen_threads.add(key)
