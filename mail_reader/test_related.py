"""Tests for mail_reader.related.

Integration-style: needs the real `mailvec` Postgres DB. Skipped when the
DB isn't reachable, matching the pattern in `scripts/test_embed_mail.py`.

Headline regression: on 2026-05-22 the tankekart endpoint 500'd with
`psycopg.errors.IndeterminateDatatype: could not determine data type of
parameter $3`. The bare `%s IS NULL` form in the SQL had no type hint
from surrounding context, so Postgres refused to prepare the statement
regardless of the actual parameter value (the offending row had
`thread_id = 'thread:…'`, not NULL — my first reading of the error was
wrong). Fix added explicit `%s::text` casts.

`test_tankekart_does_not_raise_on_indeterminate_datatype` pins that the
SQL prepares successfully against a representative real message. If you
remove the casts in `related.py`, this test should fail with
IndeterminateDatatype.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader.related import tankekart


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


def _pick_message_id(conn: psycopg.Connection, where: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.message_id "
            "FROM messages m "
            "JOIN chunks c ON c.message_id = m.id AND c.attachment_id IS NULL "
            f"WHERE {where} "
            "GROUP BY m.id, m.message_id "
            "LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_tankekart_does_not_raise_on_indeterminate_datatype(conn):
    """Regression for 2026-05-22.

    The query contained `%s IS NULL` with no type context. Postgres'
    prepared-statement protocol refuses to plan such queries:
    `IndeterminateDatatype: could not determine data type of parameter $3`.
    Fix: add `::text` casts on the thread_id parameter placeholders.

    Drop the casts in `related.py` and this test fails. Keep them and it
    returns a list (possibly empty)."""
    mid = _pick_message_id(conn, "m.thread_id IS NOT NULL")
    if mid is None:
        pytest.skip("no embedded messages in mailvec to exercise the SQL")
    out = tankekart(conn, mid, k=3)
    assert isinstance(out, list)
    for r in out:
        assert "message_id" in r
        assert 0.0 <= r["similarity"] <= 1.0
        assert r["summary"] is None  # tankekart() leaves this for the caller


def test_tankekart_handles_null_thread_id_branch(conn):
    """Belt-and-suspenders: if a row with NULL thread_id ever lands in
    mailvec, the `m.thread_id IS NULL OR …` branch should still execute
    cleanly. Currently there are no such rows in the fixture DB, so this
    test will skip — left in to exercise the code path if/when one appears
    (e.g. messages without an In-Reply-To chain)."""
    mid = _pick_message_id(conn, "m.thread_id IS NULL")
    if mid is None:
        pytest.skip("no message with thread_id IS NULL in fixtures")
    out = tankekart(conn, mid, k=3)
    assert isinstance(out, list)


def test_tankekart_returns_empty_for_unknown_message(conn):
    out = tankekart(conn, "this-message-does-not-exist@example.invalid", k=5)
    assert out == []
