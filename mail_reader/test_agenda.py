"""Tests for the agenda query.

Integration-style: needs `mailvec`. The agenda query reads
`summary_temporal + summaries + messages` and dedupes across thread
replies. We insert synthetic rows under a sentinel `prompt_version`
that doesn't collide with the live `PROMPT_VERSION`, then patch
`summarize.PROMPT_VERSION` in the test so the agenda module's filter
matches our fixtures and ignores everything else.
"""
from __future__ import annotations

import datetime
import os
import uuid

import psycopg
import pytest

from mail_reader import agenda, summarize
from mail_reader.thread_id import ThreadId


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
TEST_PROMPT = "pytest-agenda"


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


@pytest.fixture
def isolated_prompt(monkeypatch):
    """Patch `summarize.PROMPT_VERSION` so the agenda query's filter only
    matches our test rows. Tests INSERT summaries at this prompt version
    so they're invisible to the live UI and only visible to the test."""
    monkeypatch.setattr(summarize, "PROMPT_VERSION", TEST_PROMPT)


@pytest.fixture
def fixture_thread(conn):
    """Create one synthetic message in `messages` (and pre-clean any prior
    test rows). Returns (thread_id, message_row_id, message_id_string).

    Cleans up on teardown so reruns are idempotent. We never INSERT into
    `chunks` — the agenda query joins only summaries/temporal/messages."""
    thread_id = f"thread:pytest-agenda-{uuid.uuid4().hex[:12]}"
    msg_uid = f"pytest-agenda-{uuid.uuid4().hex[:12]}@example.invalid"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (thread_id, message_id, subject, from_addr, date, tier)
            VALUES (%s, %s, %s, %s, now(), 1)
            RETURNING id
            """,
            (thread_id, msg_uid, "Fixture: pytest-agenda thread",
             "fixture@example.invalid"),
        )
        row = cur.fetchone()
        assert row is not None
        mrid = row[0]
        conn.commit()
    yield thread_id, mrid, msg_uid
    with conn.cursor() as cur:
        # CASCADE wipes summaries; summaries CASCADE wipes summary_temporal.
        cur.execute("DELETE FROM messages WHERE id = %s", (mrid,))
        conn.commit()


def _insert_summary(conn, mrid: int, *, model: str = "pytest-fake") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, %s, %s, 'fixture summary', 'done', 2)
            RETURNING id
            """,
            (mrid, model, TEST_PROMPT),
        )
        row = cur.fetchone()
        assert row is not None
        conn.commit()
        return row[0]


def _insert_temporal(conn, sid: int, kind: str, occurs_at: datetime.date,
                     note: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
            VALUES (%s, %s, %s, %s)
            """,
            (sid, kind, occurs_at, note),
        )
        conn.commit()


def test_returns_upcoming_within_window(conn, isolated_prompt, fixture_thread):
    """Items inside [today, today+days) come back; items outside don't."""
    _, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    today = datetime.date.today()
    _insert_temporal(conn, sid, "deadline", today + datetime.timedelta(days=3),
                     "in-window")
    _insert_temporal(conn, sid, "deadline", today + datetime.timedelta(days=30),
                     "out-of-window")
    _insert_temporal(conn, sid, "deadline", today - datetime.timedelta(days=1),
                     "past")
    items = agenda.list_upcoming(conn, days=14)
    notes = [it["note"] for it in items]
    assert "in-window" in notes
    assert "out-of-window" not in notes
    assert "past" not in notes


def test_today_is_included(conn, isolated_prompt, fixture_thread):
    """An item dated today is in the agenda — `>= current_date`."""
    _, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    _insert_temporal(conn, sid, "deadline", datetime.date.today(), "today")
    items = agenda.list_upcoming(conn, days=14)
    assert any(it["note"] == "today" for it in items)


def test_mentioned_kind_filtered_out(conn, isolated_prompt, fixture_thread):
    """`mentioned` is generic chatter — not agenda fodder."""
    _, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    today = datetime.date.today()
    _insert_temporal(conn, sid, "mentioned", today + datetime.timedelta(days=2),
                     "ignore me")
    _insert_temporal(conn, sid, "event", today + datetime.timedelta(days=2),
                     "keep me")
    items = agenda.list_upcoming(conn, days=14)
    notes = [it["note"] for it in items]
    assert "ignore me" not in notes
    assert "keep me" in notes


def test_dedupes_across_thread_replies(conn, isolated_prompt, fixture_thread):
    """When the same (kind, date) appears in multiple summaries within
    one thread (e.g. fliser-bestillingsfrist quoted in every reply),
    the agenda strip should show one card, not six."""
    thread_id, mrid, _ = fixture_thread
    # Add three more messages in the same thread (different reply IDs,
    # different timestamps) and a summary per message, each with the
    # same (kind, occurs_at) row in summary_temporal. Message IDs are
    # uuid'd so the messages_message_id_key unique constraint doesn't
    # collide when the same test runs twice (cleanup happens via the
    # CASCADE in the fixture teardown only for the root mrid).
    target = datetime.date.today() + datetime.timedelta(days=5)
    reply_mrids: list[int] = []
    with conn.cursor() as cur:
        for i in range(3):
            cur.execute(
                """
                INSERT INTO messages (thread_id, message_id, subject, from_addr, date, tier)
                VALUES (%s, %s, %s, %s, now() + (interval '1 minute' * %s), 1)
                RETURNING id
                """,
                (thread_id, f"reply-{uuid.uuid4().hex[:12]}@example.invalid",
                 "Fixture: pytest-agenda thread",
                 "fixture@example.invalid", i + 1),
            )
            row = cur.fetchone()
            assert row is not None
            reply_mrid = row[0]
            reply_mrids.append(reply_mrid)
            cur.execute(
                """
                INSERT INTO summaries
                    (message_id, model, prompt_version, short, status, quality_tier)
                VALUES (%s, %s, %s, %s, 'done', 2)
                RETURNING id
                """,
                (reply_mrid, "pytest-fake", TEST_PROMPT, f"reply {i} summary"),
            )
            sid_row = cur.fetchone()
            assert sid_row is not None
            sid_reply = sid_row[0]
            cur.execute(
                """
                INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
                VALUES (%s, 'deadline', %s, %s)
                """,
                (sid_reply, target, f"frist note (reply {i})"),
            )
        # And one on the original fixture message.
        sid = _insert_summary(conn, mrid)
        cur.execute(
            """
            INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
            VALUES (%s, 'deadline', %s, %s)
            """,
            (sid, target, "frist note (root)"),
        )
        conn.commit()
    try:
        items = agenda.list_upcoming(conn, days=14)
        # agenda returns ThreadId; fixture stores the prefixed-form string.
        expected_tid = ThreadId(thread_id)
        hits = [it for it in items if it["thread_id"] == expected_tid
                and it["occurs_at"] == target and it["kind"] == "deadline"]
        assert len(hits) == 1, f"expected 1 dedup'd row, got {len(hits)}: {hits}"
        # And the surviving row should be from the latest reply (highest m.date).
        assert hits[0]["note"] == "frist note (reply 2)"
    finally:
        # Clean up the reply messages; fixture only cascades the root mrid.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE id = ANY(%s)",
                        (reply_mrids,))
            conn.commit()


def test_skips_non_current_prompt_version(conn, isolated_prompt, fixture_thread):
    """The strip filters to the current prompt version so a stale-prompt
    summary (e.g. from a regen cycle in flight) doesn't leak through."""
    _, mrid, _ = fixture_thread
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, %s, 'pytest-stale-version', 'stale', 'done', 2)
            RETURNING id
            """,
            (mrid, "pytest-fake-stale"),
        )
        row = cur.fetchone()
        assert row is not None
        stale_sid = row[0]
        cur.execute(
            """
            INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
            VALUES (%s, 'deadline', %s, %s)
            """,
            (stale_sid, datetime.date.today() + datetime.timedelta(days=3),
             "should not appear (stale prompt)"),
        )
        conn.commit()
    items = agenda.list_upcoming(conn, days=14)
    assert not any(it["note"] == "should not appear (stale prompt)"
                   for it in items)


def test_skips_unfinished_summaries(conn, isolated_prompt, fixture_thread):
    """A streaming/pending summary shouldn't put items in the agenda —
    its temporal rows may be from a half-parsed response."""
    _, mrid, _ = fixture_thread
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, %s, %s, '', 'pending', 2)
            RETURNING id
            """,
            (mrid, "pytest-fake-pending", TEST_PROMPT),
        )
        row = cur.fetchone()
        assert row is not None
        pending_sid = row[0]
        cur.execute(
            """
            INSERT INTO summary_temporal (summary_id, kind, occurs_at, note)
            VALUES (%s, 'event', %s, %s)
            """,
            (pending_sid, datetime.date.today() + datetime.timedelta(days=2),
             "should not appear (pending)"),
        )
        conn.commit()
    items = agenda.list_upcoming(conn, days=14)
    assert not any(it["note"] == "should not appear (pending)"
                   for it in items)


def test_thread_id_is_bare_no_thread_prefix(conn, isolated_prompt,
                                              fixture_thread):
    """Regression: messages.thread_id is stored 'thread:XXXX' in the DB
    (prefixed), but the /t/{thread_id} route passes the value straight
    to notmuch which re-prefixes it → 'thread:thread:XXXX' → 404.

    agenda items must return the bare form (matching inbox.py's shape)
    so url_for('get_thread', thread_id=...) builds a URL the route
    actually resolves."""
    _, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    _insert_temporal(
        conn, sid, "deadline",
        datetime.date.today() + datetime.timedelta(days=2),
        "bare-thread-id-check",
    )
    items = agenda.list_upcoming(conn, days=14)
    hits = [it for it in items if it["note"] == "bare-thread-id-check"]
    assert len(hits) == 1
    # ThreadId normalizes to bare internally; str(tid) gives the
    # bare form used in URLs (no `thread:` prefix).
    assert not str(hits[0]["thread_id"]).startswith("thread:"), (
        f"thread_id should be bare, got {hits[0]['thread_id']!r}"
    )
    # And it must be a ThreadId, not a raw str — that's the whole guard.
    assert isinstance(hits[0]["thread_id"], ThreadId)


def test_sorts_by_date_then_kind(conn, isolated_prompt, fixture_thread):
    """Output is ordered: occurs_at ASC, then kind ASC."""
    _, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    today = datetime.date.today()
    _insert_temporal(conn, sid, "event",
                     today + datetime.timedelta(days=2), "B")
    _insert_temporal(conn, sid, "deadline",
                     today + datetime.timedelta(days=2), "A")
    _insert_temporal(conn, sid, "deadline",
                     today + datetime.timedelta(days=1), "first")
    items = agenda.list_upcoming(conn, days=14)
    relevant = [it["note"] for it in items
                if it["note"] in ("A", "B", "first")]
    assert relevant == ["first", "A", "B"]


def test_dismiss_suppresses_item(conn, isolated_prompt, fixture_thread):
    """A dismissed (thread_id, kind, occurs_at) tuple disappears from
    `list_upcoming`. Cleanup at the end so test reruns are idempotent."""
    thread_id, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    when = datetime.date.today() + datetime.timedelta(days=4)
    _insert_temporal(conn, sid, "deadline", when, "to be dismissed")
    _insert_temporal(conn, sid, "event", when, "keep me")
    try:
        before = agenda.list_upcoming(conn, days=14)
        assert any(it["note"] == "to be dismissed" for it in before)

        agenda.dismiss(conn, ThreadId(thread_id), "deadline", when.isoformat())

        after = agenda.list_upcoming(conn, days=14)
        assert not any(it["note"] == "to be dismissed" for it in after), (
            "dismissed item should no longer appear"
        )
        # Other kinds on the same (thread, date) stay — dismissal is per-kind.
        assert any(it["note"] == "keep me" for it in after)
    finally:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM agenda_dismissed
                WHERE thread_id = %s AND occurs_at = %s
                """,
                (ThreadId(thread_id).db_form, when),
            )
            conn.commit()


def test_dismiss_is_idempotent(conn, isolated_prompt, fixture_thread):
    """Calling dismiss twice for the same key is a no-op the second time
    — first call returns True (inserted), second returns False (already
    there)."""
    thread_id, mrid, _ = fixture_thread
    sid = _insert_summary(conn, mrid)
    when = datetime.date.today() + datetime.timedelta(days=6)
    _insert_temporal(conn, sid, "deadline", when, "double-dismiss")
    tid = ThreadId(thread_id)
    try:
        first = agenda.dismiss(conn, tid, "deadline", when.isoformat())
        second = agenda.dismiss(conn, tid, "deadline", when.isoformat())
        assert first is True
        assert second is False
    finally:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM agenda_dismissed
                WHERE thread_id = %s AND occurs_at = %s
                """,
                (tid.db_form, when),
            )
            conn.commit()
