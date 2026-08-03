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


_PICK_SQL_NON_NULL_THREAD = (
    "SELECT m.message_id FROM messages m "
    "JOIN chunks c ON c.message_id = m.id AND c.attachment_id IS NULL "
    "WHERE m.thread_id IS NOT NULL "
    "GROUP BY m.id, m.message_id LIMIT 1"
)
_PICK_SQL_NULL_THREAD = (
    "SELECT m.message_id FROM messages m "
    "JOIN chunks c ON c.message_id = m.id AND c.attachment_id IS NULL "
    "WHERE m.thread_id IS NULL "
    "GROUP BY m.id, m.message_id LIMIT 1"
)


def _pick_message_id(conn: psycopg.Connection, null_thread: bool) -> str | None:
    with conn.cursor() as cur:
        cur.execute(_PICK_SQL_NULL_THREAD if null_thread else _PICK_SQL_NON_NULL_THREAD)
        row = cur.fetchone()
        return row[0] if row else None


def test_tankekart_does_not_raise_on_indeterminate_datatype(conn):
    """Regression for 2026-05-22.

    The query contained `%s IS NULL` with no type context. Postgres'
    prepared-statement protocol refuses to plan such queries:
    `IndeterminateDatatype: could not determine data type of parameter $3`.
    Fix: add `::text` casts on the thread_id parameter placeholders.

    Drop the casts in `related.py` and this test fails. Keep them and it
    returns a list of branches (possibly empty)."""
    mid = _pick_message_id(conn, null_thread=False)
    if mid is None:
        pytest.skip("no embedded messages in mailvec to exercise the SQL")
    branches = tankekart(conn, mid, n_per_branch=3)
    assert isinstance(branches, list)
    for b in branches:
        assert isinstance(b["chunk_idx"], int)
        assert isinstance(b["label"], str)
        for leaf in b["leaves"]:
            assert "message_id" in leaf
            assert 0.0 <= leaf["similarity"] <= 1.0
            assert leaf["summary"] is None


@pytest.fixture
def msg_temporarily_null_thread(conn):
    """Take a real embedded message, NULL its thread_id for the duration
    of the test, restore in teardown. Exercises the `IS NULL` branches
    of the tankekart SQL — those rarely arise in production but the
    code path exists, and the previous IndeterminateDatatype bug lived
    exactly in that branch's parameter handling."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.thread_id, m.message_id
            FROM messages m
            JOIN chunks c
              ON c.message_id = m.id AND c.attachment_id IS NULL
            WHERE m.thread_id IS NOT NULL
            GROUP BY m.id
            ORDER BY m.id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no embedded message with thread_id available")
    row_id, original_thread, mid = row
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET thread_id = NULL WHERE id = %s", (row_id,)
        )
        conn.commit()
    try:
        yield mid
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET thread_id = %s WHERE id = %s",
                (original_thread, row_id),
            )
            conn.commit()


def test_tankekart_handles_null_thread_id_branch(conn, msg_temporarily_null_thread):
    """The `m.thread_id IS NULL OR …` branch should execute cleanly.

    Production thread_ids are always set by `embed_mail.py` (notmuch
    assigns one), so this scenario doesn't arise organically — but the
    SQL covers it, and the previous IndeterminateDatatype bug lived
    in exactly this branch's parameter typing. The fixture forces the
    condition by mutating a real row + restoring on teardown."""
    branches = tankekart(conn, msg_temporarily_null_thread, n_per_branch=3)
    assert isinstance(branches, list)
    # We have an embedded message, so there should be at least one branch
    # (the source mail has at least one chunk).
    assert branches, "expected at least one branch for an embedded message"


def test_tankekart_unknown_message_uses_live_embed_or_returns_empty(conn):
    """An unknown message_id can't be embedded live either (notmuch will
    fail to fetch raw), so the live-embed fallback returns []."""
    branches = tankekart(
        conn, "this-message-does-not-exist@example.invalid", n_per_branch=3,
    )
    assert branches == []


def _pick_themed_message_id(conn: psycopg.Connection) -> str | None:
    """Pick a mail with ≥1 tier-2 theme at current prompt_version."""
    from mail_reader import summarize
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.message_id
            FROM messages m
            JOIN summaries s ON s.message_id = m.id
                            AND s.prompt_version = %s
                            AND s.status = 'done'
            JOIN summary_themes st ON st.summary_id = s.id
            GROUP BY m.id, m.message_id
            HAVING COUNT(DISTINCT st.theme_id) >= 1
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (summarize.PROMPT_VERSION,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_tankekart_emergent_mode_returns_branches(conn):
    """emergent mode: cluster neighbours' themes. For any embedded mail,
    if there are themed neighbours, we should get branches back."""
    mid = _pick_themed_message_id(conn)
    if mid is None:
        pytest.skip("no message with tier-2 themes at current prompt_version")
    branches = tankekart(conn, mid, n_per_branch=3, mode="emergent")
    assert isinstance(branches, list)
    for b in branches:
        assert isinstance(b["label"], str) and b["label"]
        for leaf in b["leaves"]:
            assert leaf["message_id"]
            # emergent leaves preserve real semantic distances from the
            # chunks query, so similarity should be in [0, 1].
            assert 0.0 <= leaf["similarity"] <= 1.0


def test_tankekart_emergent_mode_for_unindexed_returns_empty(conn):
    """emergent mode doesn't have a live-embed fallback. An
    unknown message returns []."""
    assert tankekart(
        conn, "no-such-msg@example.invalid", n_per_branch=3, mode="emergent",
    ) == []
