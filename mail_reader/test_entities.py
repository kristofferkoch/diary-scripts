"""Tests for mail_reader.entities.

Integration: uses synthetic rows in the disposable test database.
"""
from __future__ import annotations

import pytest

from mail_reader import entities, summarize


@pytest.fixture
def conn(db_conn):
    return db_conn


@pytest.fixture
def entity_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (kind, value, normalized) "
            "VALUES ('org', 'Pytest Bank', 'pytest bank') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        eid = row[0]
        cur.execute(
            "INSERT INTO entities (kind, value, normalized) "
            "VALUES ('url', 'https://example.invalid', 'https://example.invalid') "
            "RETURNING id"
        )
        url_id = cur.fetchone()[0]
        for mid, date in (
            ("entity-root@example.invalid", "2026-01-01T12:00:00Z"),
            ("entity-reply@example.invalid", "2026-01-02T12:00:00Z"),
        ):
            cur.execute(
                "INSERT INTO messages (message_id, thread_id, date, tier) "
                "VALUES (%s, 'thread:pytest-entity', %s, 2) RETURNING id",
                (mid, date),
            )
            message_row_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO summaries "
                "(message_id, model, prompt_version, short, status, quality_tier) "
                "VALUES (%s, 'pytest', %s, 'summary', 'done', 2) RETURNING id",
                (message_row_id, summarize.PROMPT_VERSION),
            )
            summary_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO summary_entities (summary_id, entity_id) "
                "VALUES (%s, %s), (%s, %s)",
                (summary_id, eid, summary_id, url_id),
            )
    return eid


def test_chips_for_message_returns_chips(conn, entity_id):
    chips = entities.chips_for_message(conn, "entity-root@example.invalid")
    assert chips == [{
        "id": entity_id,
        "kind": "org",
        "value": "Pytest Bank",
        "label": "Pytest Bank",
    }]


def test_chips_unknown_message_returns_empty(conn):
    chips = entities.chips_for_message(conn, "no-such@example.invalid")
    assert chips == []


def test_entity_by_id_round_trip(conn, entity_id):
    assert entities.entity_by_id(conn, entity_id) == {
        "id": entity_id, "kind": "org", "value": "Pytest Bank",
    }
    assert entities.entity_by_id(conn, -1) is None


def test_messages_for_entity_dedups_by_thread(conn, entity_id):
    rows = entities.messages_for_entity(conn, entity_id, limit=20)
    assert [row["message_id"] for row in rows] == ["entity-reply@example.invalid"]
