"""Tests for mail_reader.related.

Integration-style: uses synthetic rows in the disposable test database.

Headline regression: on 2026-05-22 the tankekart endpoint 500'd with
`psycopg.errors.IndeterminateDatatype: could not determine data type of
parameter $3`. The bare `%s IS NULL` form in the SQL had no type hint
from surrounding context, so Postgres refused to prepare the statement
regardless of the actual parameter value (the offending row had
`thread_id = 'thread:…'`, not NULL — my first reading of the error was
wrong). Fix added explicit `%s::text` casts.

`test_tankekart_does_not_raise_on_indeterminate_datatype` pins that the
SQL prepares successfully against a deterministic message. If you remove
the casts in `related.py`, this test should fail with IndeterminateDatatype.
"""
from __future__ import annotations

import pytest

from mail_reader.related import tankekart


@pytest.fixture
def conn(db_conn):
    return db_conn


def _vector(width: int, first: float, second: float = 0.0) -> str:
    return "[" + ",".join(map(str, [first, second, *([0.0] * (width - 2))])) + "]"


@pytest.fixture
def related_mids(conn):
    from mail_reader import summarize

    source = "related-source@example.invalid"
    neighbor = "related-neighbor@example.invalid"
    with conn.cursor() as cur:
        for mid, thread_id, text, embedding in (
            (source, "thread:related-source", "source chunk", _vector(4096, 1.0)),
            (neighbor, "thread:related-neighbor", "neighbor chunk", _vector(4096, 0.8, 0.2)),
        ):
            cur.execute(
                "INSERT INTO messages (message_id, thread_id, tier) "
                "VALUES (%s, %s, 2) RETURNING id",
                (mid, thread_id),
            )
            message_row_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO chunks "
                "(message_id, attachment_id, chunk_idx, text, embedding) "
                "VALUES (%s, NULL, 0, %s, %s::vector)",
                (message_row_id, text, embedding),
            )
        cur.execute(
            "INSERT INTO summaries "
            "(message_id, model, prompt_version, short, status, quality_tier) "
            "SELECT id, 'pytest', %s, 'summary', 'done', 2 FROM messages "
            "WHERE message_id = %s RETURNING id",
            (summarize.PROMPT_VERSION, neighbor),
        )
        summary_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO themes (text, embedding) "
            "VALUES ('pytest theme', %s::vector) RETURNING id",
            (_vector(1024, 1.0),),
        )
        theme_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO summary_themes (summary_id, theme_id) VALUES (%s, %s)",
            (summary_id, theme_id),
        )
    return source, neighbor


def test_tankekart_does_not_raise_on_indeterminate_datatype(conn, related_mids):
    """The `%s IS NULL` parameters must have explicit text casts."""
    source, neighbor = related_mids
    branches = tankekart(conn, source, n_per_branch=3)
    assert len(branches) == 1
    assert branches[0]["chunk_idx"] == 0
    assert branches[0]["label"] == "source chunk"
    assert [leaf["message_id"] for leaf in branches[0]["leaves"]] == [neighbor]
    assert branches[0]["leaves"][0]["summary"] is None


def test_tankekart_handles_null_thread_id_branch(conn, related_mids):
    source, neighbor = related_mids
    with conn.cursor() as cur:
        cur.execute("UPDATE messages SET thread_id = NULL WHERE message_id = %s", (source,))
    branches = tankekart(conn, source, n_per_branch=3)
    assert len(branches) == 1
    assert [leaf["message_id"] for leaf in branches[0]["leaves"]] == [neighbor]


def test_tankekart_unknown_message_uses_live_embed_or_returns_empty(conn):
    """An unknown message_id can't be embedded live either (notmuch will
    fail to fetch raw), so the live-embed fallback returns []."""
    branches = tankekart(
        conn, "this-message-does-not-exist@example.invalid", n_per_branch=3,
    )
    assert branches == []


def test_tankekart_emergent_mode_returns_branches(conn, related_mids):
    source, neighbor = related_mids
    branches = tankekart(conn, source, n_per_branch=3, mode="emergent")
    assert len(branches) == 1
    assert branches[0]["label"] == "pytest theme"
    assert [leaf["message_id"] for leaf in branches[0]["leaves"]] == [neighbor]


def test_tankekart_emergent_mode_for_unindexed_returns_empty(conn):
    """emergent mode doesn't have a live-embed fallback. An
    unknown message returns []."""
    assert tankekart(
        conn, "no-such-msg@example.invalid", n_per_branch=3, mode="emergent",
    ) == []
