"""Tests for mail_reader.summarize state machine.

Integration-style: needs `mailvec`. Mocks the Ollama call (`_ollama_chat`)
so tests don't hit the network.

**Isolation**: tests use a synthetic `TEST_MODEL` / `TEST_TIER` so the
live webapp workers (running against PASSES = qwen2.5:3b + qwen3.6:35b)
don't race the test fixtures by claiming and generating against the
test rows. The synthetic model is not in PASSES; workers ignore it.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import psycopg
import pytest

from mail_reader import extract, summarize


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
TEST_MODEL = "pytest-fake-model"
TEST_TIER = 99

# Tier-2 tests claim with the real MAX_TIER value so the structured-JSON
# branch in generate_and_store triggers (`tier == MAX_TIER`). A sentinel
# model name keeps the live qwen workers from picking these up — they
# filter the queue by (model, tier) jointly.
TEST_MODEL_TIER2 = "pytest-tier2-fake"


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
def fresh_msg(conn):
    """Pick an embedded message that has **no** summary rows at all —
    a fully virgin mid. read_state aggregates across all models, so if
    the live workers had previously written a real qwen row for this
    mid, every test would surface that instead of the test fixtures."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.message_id
            FROM messages m
            JOIN chunks c
              ON c.message_id = m.id AND c.attachment_id IS NULL
            LEFT JOIN summaries s
              ON s.message_id = m.id
            WHERE s.id IS NULL
            GROUP BY m.id, m.message_id
            ORDER BY m.id
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("no virgin embedded message available")
    mid = row[0]
    yield mid
    # Sweep every row we may have inserted for this mid. Pattern covers
    # the explicit sentinels plus any `pytest-*` model name tests use.
    # Cascading deletes wipe the side-table M2M links.
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND (model IN ('fast-model', 'slow-model')
                   OR model LIKE 'pytest-%%')
            """,
            (mid,),
        )
        conn.commit()


def test_claim_dedup_first_caller_wins(conn, fresh_msg):
    """The motivating regression: two callers race to summarise the same
    message. Exactly one should claim, the other should see the existing
    pending row and back off."""
    first = summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    second = summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    assert first is True
    assert second is False


def test_claim_for_unknown_message_returns_false(conn):
    """Message not in `messages` (e.g. not yet embedded) → no claim."""
    assert summarize.claim_for_generation(
        conn, "definitely-not-in-mailvec@example.invalid",
        model=TEST_MODEL, tier=TEST_TIER,
    ) is False


def test_read_state_after_claim_is_pending(conn, fresh_msg):
    summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "pending"
    assert state["short"] == ""
    assert state["error"] is None


def test_generate_and_store_flips_to_done(conn, fresh_msg):
    """With Ollama mocked to return a canned summary, generate_and_store
    should transition `pending` → `done` with the summary text stored."""
    summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    with patch.object(summarize, "_ollama_chat",
                      return_value="Oppsummering for testen."):
        summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL)
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "done"
    assert state["short"] == "Oppsummering for testen."
    assert state["error"] is None


def test_generate_and_store_records_failure(conn, fresh_msg):
    """If Ollama raises, the row should land in `failed` with the error
    string captured — not get stuck at `pending`."""
    summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    with patch.object(summarize, "_ollama_chat",
                      side_effect=TimeoutError("ollama unreachable")):
        summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL)
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "failed"
    assert state["error"] and "ollama" in state["error"].lower()


def test_generate_and_store_empty_response_marks_failed(conn, fresh_msg):
    summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    with patch.object(summarize, "_ollama_chat", return_value="  "):
        summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL)
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "failed"


def test_read_state_returns_current_version_when_present(conn, fresh_msg):
    """If a row exists at the configured PROMPT_VERSION, read_state returns
    it as plain `done` — not stale."""
    summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER)
    with patch.object(summarize, "_ollama_chat", return_value="current-version summary"):
        summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL)
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["short"] == "current-version summary"
    assert state["status"] == "done"


def test_read_state_returns_old_version_as_stale(conn, fresh_msg):
    """When only an older prompt_version row exists, read_state surfaces
    a composite `done_stale` status — content to show, regen needed."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status)
            VALUES (%s, %s, 'p-old', 'old summary text', 'done')
            """,
            (mrid, TEST_MODEL),
        )
        conn.commit()
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "done_stale"
    assert state["short"] == "old summary text"


def test_read_state_ignores_old_failed_rows(conn, fresh_msg):
    """An old `failed` row carries no usable content. read_state should
    treat it as if no row exists, so the caller claims fresh at the
    current version."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, error)
            VALUES (%s, %s, 'p-old', '', 'failed', 'old error')
            """,
            (mrid, TEST_MODEL),
        )
        conn.commit()
    state = summarize.read_state(conn, fresh_msg)
    assert state is None


def test_claim_succeeds_when_only_older_version_exists(conn, fresh_msg):
    """Atomic claim inserts at the CURRENT prompt_version; an older-version
    row is on a different unique key, so the claim still wins. This is
    what enables 'show old, regen new'."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status)
            VALUES (%s, %s, 'p-old', 'old', 'done')
            """,
            (mrid, TEST_MODEL),
        )
        conn.commit()
    # Old version present, current version absent → claim should succeed.
    assert summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER) is True
    # And a second claim for the same current version dedups.
    assert summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER) is False


def test_schedule_all_passes_inserts_one_pending_per_pass(conn, fresh_msg, monkeypatch):
    """schedule_all_passes claims one row per configured pass. Patches
    PASSES with synthetic models so the live webapp workers (configured
    for real qwen models) don't race the test."""
    TEST_PASSES = [
        {"model": "pytest-pass-fast", "tier": 91},
        {"model": "pytest-pass-slow", "tier": 92},
    ]
    monkeypatch.setattr(summarize, "PASSES", TEST_PASSES)
    n_first = summarize.schedule_all_passes(conn, fresh_msg)
    assert n_first == len(TEST_PASSES)
    n_second = summarize.schedule_all_passes(conn, fresh_msg)
    assert n_second == 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model, quality_tier, status::text
            FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND prompt_version = %s
              AND model LIKE 'pytest-pass-%%'
            ORDER BY quality_tier
            """,
            (fresh_msg, summarize.PROMPT_VERSION),
        )
        rows = cur.fetchall()
    assert len(rows) == len(TEST_PASSES)
    for (model, tier, status), pass_def in zip(rows, TEST_PASSES):
        assert model == pass_def["model"]
        assert tier == pass_def["tier"]
        assert status == "pending"
    # Cleanup any rows created here (the fresh_msg fixture only sweeps
    # TEST_MODEL / fast-model / slow-model).
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND model LIKE 'pytest-pass-%%'
            """,
            (fresh_msg,),
        )
        conn.commit()


def test_bump_priority_moves_row_to_now(conn, fresh_msg, monkeypatch):
    """bump_priority sets requested_at = now() for pending rows. Lets a
    fresh user request jump ahead of older backlog items."""
    TEST_PASSES = [
        {"model": "pytest-pass-fast", "tier": 91},
        {"model": "pytest-pass-slow", "tier": 92},
    ]
    monkeypatch.setattr(summarize, "PASSES", TEST_PASSES)
    summarize.schedule_all_passes(conn, fresh_msg)
    # Backdate the rows so we have something to bump against.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries SET requested_at = now() - interval '1 hour'
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND status = 'pending'
            """,
            (fresh_msg,),
        )
        conn.commit()
        cur.execute(
            """
            SELECT MAX(requested_at) FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND status = 'pending'
            """,
            (fresh_msg,),
        )
        before = cur.fetchone()[0]
    n = summarize.bump_priority(conn, [fresh_msg])
    assert n == len(TEST_PASSES)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MIN(requested_at) FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND status = 'pending'
            """,
            (fresh_msg,),
        )
        after = cur.fetchone()[0]
    assert after > before
    # Cleanup the pytest-pass-* rows.
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND model LIKE 'pytest-pass-%%'
            """,
            (fresh_msg,),
        )
        conn.commit()


def test_read_state_done_draft_when_lower_tier_done_and_higher_pending(conn, fresh_msg):
    """The combined-state contract: if a fast (low tier) pass is done but
    a slow (higher tier) pass is still in flight, read_state returns
    'done_draft' with the draft text — the UI shows it AND keeps
    polling for the better version."""
    mrid_q = "SELECT id FROM messages WHERE message_id = %s"
    with conn.cursor() as cur:
        cur.execute(mrid_q, (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        # Insert a done row at the lower tier and a pending row at the higher.
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, 'fast-model', %s, 'draft text', 'done', 1),
                   (%s, 'slow-model', %s, '', 'pending', 2)
            """,
            (mrid, summarize.PROMPT_VERSION, mrid, summarize.PROMPT_VERSION),
        )
        conn.commit()
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "done_draft"
    assert state["short"] == "draft text"


def test_read_state_done_when_max_tier_done(conn, fresh_msg):
    """When the highest configured tier is `done`, read_state returns
    plain `done` — no more polling needed."""
    mrid_q = "SELECT id FROM messages WHERE message_id = %s"
    with conn.cursor() as cur:
        cur.execute(mrid_q, (fresh_msg,))
        mrid = cur.fetchone()[0]
        # Both passes done. read_state should pick the max-tier one.
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, 'fast-model', %s, 'draft', 'done', 1),
                   (%s, 'slow-model', %s, 'final', 'done', 2)
            """,
            (mrid, summarize.PROMPT_VERSION, mrid, summarize.PROMPT_VERSION),
        )
        conn.commit()
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "done"
    assert state["short"] == "final"  # higher tier wins


def test_reclaim_of_stale_pending_row(conn, fresh_msg):
    """After a pending row sits idle past `RECLAIM_AFTER_SECONDS`, the
    next claim marks it `failed` (preserved as history) and inserts a
    fresh `pending` row — the partial-unique lock applies only to
    in-flight rows, so the failed one no longer blocks. Both rows
    coexist afterwards."""
    # First claim → row #1 pending
    assert summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER) is True
    # Backdate row #1 past reclaim threshold
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries SET updated_at = now() - make_interval(secs => %s)
            WHERE model = %s
              AND prompt_version = %s
              AND message_id = (SELECT id FROM messages WHERE message_id = %s)
            """,
            (summarize.RECLAIM_AFTER_SECONDS + 60,
             TEST_MODEL, summarize.PROMPT_VERSION, fresh_msg),
        )
        conn.commit()
    # Second claim: reclaims old (marks failed), INSERTs fresh pending → True
    assert summarize.claim_for_generation(conn, fresh_msg, model=TEST_MODEL, tier=TEST_TIER) is True
    # read_state surfaces the live pending row.
    state = summarize.read_state(conn, fresh_msg)
    assert state is not None
    assert state["status"] == "pending"
    # Two rows now exist: one 'failed' (reclaimed), one 'pending' (fresh).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status::text, error FROM summaries
            WHERE model = %s AND prompt_version = %s
              AND message_id = (SELECT id FROM messages WHERE message_id = %s)
            ORDER BY id
            """,
            (TEST_MODEL, summarize.PROMPT_VERSION, fresh_msg),
        )
        rows = cur.fetchall()
    statuses = {r[0] for r in rows}
    assert statuses == {"failed", "pending"}
    failed_errors = [r[1] for r in rows if r[0] == "failed"]
    assert any("abandoned" in (e or "") for e in failed_errors)


def test_reclaim_stale_streaming_clears_old_rows(conn, fresh_msg):
    """Reclaim sweeps streaming rows older than the cutoff and flips them
    to `failed`. Regression: 154 zombie rows at prompt_version='p2' that
    `_claim_one`'s per-pass reclaim could never reach (because the
    user-visible prompt version had moved to 'p5' by then). The sweep
    must be prompt_version-agnostic."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        # Insert one streaming row at a (deliberately) ancient prompt
        # version, then backdate updated_at past the threshold. The
        # trigger bumps updated_at on insert, so we backdate after.
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status)
            VALUES (%s, %s, 'p-ancient', '', 'streaming')
            RETURNING id
            """,
            (mrid, TEST_MODEL),
        )
        row = cur.fetchone()
        assert row is not None
        sid = row[0]
        cur.execute(
            "UPDATE summaries SET updated_at = now() - interval '2 hours' WHERE id = %s",
            (sid,),
        )
        conn.commit()
    n = summarize.reclaim_stale_streaming(conn, max_age_seconds=600)
    assert n >= 1
    with conn.cursor() as cur:
        cur.execute("SELECT status::text, error FROM summaries WHERE id = %s",
                    (sid,))
        row = cur.fetchone()
    assert row is not None
    status, error = row
    assert status == "failed"
    assert error and "abandoned" in error


def test_reclaim_stale_streaming_leaves_young_rows_alone(conn, fresh_msg):
    """A streaming row younger than the cutoff stays put — we don't want
    to kill rows that an active worker is currently processing."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status)
            VALUES (%s, %s, 'p-ancient', '', 'streaming')
            RETURNING id
            """,
            (mrid, TEST_MODEL),
        )
        row = cur.fetchone()
        assert row is not None
        sid = row[0]
        conn.commit()
    summarize.reclaim_stale_streaming(conn, max_age_seconds=600)
    with conn.cursor() as cur:
        cur.execute("SELECT status::text FROM summaries WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "streaming"


def test_reclaim_stale_streaming_max_age_zero_clears_all(conn, fresh_msg):
    """At worker startup we pass max_age=0 to clear any orphaned streaming
    rows — they're necessarily orphans since the only producer of
    streaming rows in this process hasn't started yet."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM messages WHERE message_id = %s", (fresh_msg,))
        mrid_row = cur.fetchone()
        assert mrid_row is not None
        mrid = mrid_row[0]
        cur.execute(
            """
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status)
            VALUES (%s, %s, 'p-ancient', '', 'streaming')
            RETURNING id
            """,
            (mrid, TEST_MODEL),
        )
        row = cur.fetchone()
        assert row is not None
        sid = row[0]
        conn.commit()
    n = summarize.reclaim_stale_streaming(conn, max_age_seconds=0)
    assert n >= 1
    with conn.cursor() as cur:
        cur.execute("SELECT status::text FROM summaries WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "failed"


def test_prompts_contain_no_copyable_concrete_facts():
    """Regression for the few-shot leak: qwen2.5 was parroting "Faktura
    nr 41463", "Eksempel Elektriske", "bekreft oppmøte innen tirsdag",
    and "Astrid" from the tier-1 system prompt's example block into
    unrelated mail summaries (notably a NATO news article got "Bekreft
    oppmøte innen tirsdag for faktura nr 41463 …"). Switching to
    abstract <placeholder>-style patterns removed the parroting fuel.

    If you re-introduce concrete facts in the prompt body, expect to
    see them in summaries — add a test for the new ones or rephrase
    them as `<placeholders>`."""
    forbidden = [
        "41463",
        "Eksempel Elektriske",
        "Astrid",
        "bekreft oppmøte innen tirsdag",
        "Acme Cloud",
    ]
    for fragment in forbidden:
        assert fragment not in summarize._SYSTEM, (
            f"tier-1 prompt contains copyable fact {fragment!r}"
        )
        assert fragment not in summarize._SYSTEM_TIER2, (
            f"tier-2 prompt contains copyable fact {fragment!r}"
        )


def test_tier1_prompt_has_no_sentence_templates():
    """Regression p4→p5: even abstract sentence templates with
    `<placeholder>` slots became hallucination recipes for qwen2.5 — a
    LinkedIn reaction notification got summarised as "Faktura på 3499
    EUR fra LinkedIn for februar, betaling innen 7 dager" because the
    prompt had a `«Faktura på [beløp] fra [avsender], forfall [dato].»`
    template. The small model treats any «…» quoted sentence with
    slots as a recipe to fill, not a shape to imitate. Rules only.

    Heuristic: any `«…»` block containing `[bracket]` or `<bracket>`
    placeholders is a template. Tier-1 prompt must not contain such
    blocks."""
    import re
    suspect = re.findall(
        r"«[^»]*[\[<][^»]*[\]>][^»]*»",
        summarize._SYSTEM,
    )
    assert not suspect, (
        f"tier-1 prompt contains sentence template(s): {suspect}. "
        "Rephrase as a rule, not a fill-in pattern."
    )


def test_queue_counts_returns_per_pass_in_pass_order(conn, fresh_msg, monkeypatch):
    """`queue_counts` returns one row per configured pass with its
    pending-or-streaming count, in the order PASSES declares them.
    Used by the topbar indicator."""
    TEST_PASSES: list[summarize.Pass] = [
        {"model": "pytest-pass-fast", "tier": 91, "label": "utkast"},
        {"model": "pytest-pass-slow", "tier": 92, "label": "endelig"},
    ]
    monkeypatch.setattr(summarize, "PASSES", TEST_PASSES)
    # Baseline before our inserts.
    baseline = {row["tier"]: row["count"] for row in summarize.queue_counts(conn)}
    summarize.schedule_all_passes(conn, fresh_msg)

    rows = summarize.queue_counts(conn)
    by_tier = {row["tier"]: row for row in rows}
    assert by_tier[91]["count"] == baseline.get(91, 0) + 1
    assert by_tier[92]["count"] == baseline.get(92, 0) + 1
    assert by_tier[91]["label"] == "utkast"
    assert by_tier[92]["label"] == "endelig"
    # Order matches PASSES iteration order.
    visible_tiers = [r["tier"] for r in rows if r["tier"] in (91, 92)]
    assert visible_tiers == [91, 92]

    # Cleanup the pytest-pass-* rows.
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM summaries
            WHERE message_id = (SELECT id FROM messages WHERE message_id = %s)
              AND model LIKE 'pytest-pass-%%'
            """,
            (fresh_msg,),
        )
        conn.commit()


# ----------------- tier-2 structured JSON path -----------------
#
# `generate_and_store` branches on `tier == MAX_TIER` for the structured
# JSON path. We claim with MAX_TIER but a sentinel model name so workers
# leave the row alone, then mock `_ollama_chat_json` to feed the parser
# without hitting the LLM. `extract.embed_batch` is also mocked since
# tier-2 success persists themes via bge-m3.
#
# Cleanup: `fresh_msg` deletes the summary row (CASCADE wipes the M2M
# links), but `themes` and `entities` themselves don't cascade. The
# `tier2_sideeffect_cleanup` fixture snapshots max(id) at start and
# deletes anything inserted during the test on teardown.


@pytest.fixture
def tier2_sideeffect_cleanup(conn):
    """Snapshot themes/entities max-id and clean up anything inserted
    during the test. Safe because no other process writes these tables
    while tests run."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM themes")
        max_theme_row = cur.fetchone()
        assert max_theme_row is not None
        max_theme_id = max_theme_row[0]
        cur.execute("SELECT coalesce(max(id), 0) FROM entities")
        max_entity_row = cur.fetchone()
        assert max_entity_row is not None
        max_entity_id = max_entity_row[0]
    yield
    # M2M rows still reference these themes/entities when we get here
    # (fresh_msg's teardown — which cascades the summary row — runs
    # later in teardown order). Wipe the links explicitly before the
    # FK targets so DELETE on themes/entities doesn't violate.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM summary_themes WHERE theme_id > %s",
                    (max_theme_id,))
        cur.execute("DELETE FROM summary_entities WHERE entity_id > %s",
                    (max_entity_id,))
        cur.execute("DELETE FROM themes WHERE id > %s", (max_theme_id,))
        cur.execute("DELETE FROM entities WHERE id > %s", (max_entity_id,))
        conn.commit()


def _vec(slot: int) -> list[float]:
    """Unit-length bge-m3-sized vector with a 1.0 at a single slot."""
    v = [0.0] * 1024
    v[slot] = 1.0
    return v


def _claim_tier2(conn, mid: str) -> None:
    """Insert a tier-MAX pending row under the sentinel model."""
    ok = summarize.claim_for_generation(
        conn, mid, model=TEST_MODEL_TIER2, tier=summarize.MAX_TIER,
    )
    assert ok


def _summary_row(conn, mid: str) -> tuple[int, str, str | None, bool, str | None]:
    """Read back (id, status, short, action_required, error) for the test row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.status::text, s.short, s.action_required, s.error
            FROM summaries s
            JOIN messages m ON m.id = s.message_id
            WHERE m.message_id = %s AND s.model = %s
            """,
            (mid, TEST_MODEL_TIER2),
        )
        row = cur.fetchone()
    assert row is not None, "tier-2 summary row missing"
    return row


def test_tier2_happy_path_persists_all_fields(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """The full structured response shreds into the side tables and
    the summary row lands in `done` with `action_required` set."""
    payload = {
        "short": "Test summary one liner.",
        "action_required": True,
        "temporal": [
            {"kind": "deadline", "occurs_at": "2026-06-15", "note": "forfall"},
            {"kind": "event",    "occurs_at": "2026-04-22", "note": "møte"},
        ],
        "themes": ["pytest-t2-theme-a", "pytest-t2-theme-b"],
        "entities": [
            {"kind": "org",   "value": "Pytest T2 Bank", "meta": {}},
            {"kind": "money", "value": "999 kr",
             "meta": {"amount": 999, "currency": "NOK"}},
        ],
    }
    monkeypatch.setattr(summarize, "_ollama_chat_json",
                        lambda *a, **kw: dict(payload))
    monkeypatch.setattr(extract, "embed_batch",
                        lambda texts: [_vec(i + 50) for i in range(len(texts))])

    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)

    sid, status, short, action_required, _ = _summary_row(conn, fresh_msg)
    assert status == "done"
    assert short == "Test summary one liner."
    assert action_required is True

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM summary_temporal WHERE summary_id = %s",
                    (sid,))
        n_temporal_row = cur.fetchone()
        assert n_temporal_row is not None
        cur.execute("SELECT count(*) FROM summary_themes WHERE summary_id = %s",
                    (sid,))
        n_themes_row = cur.fetchone()
        assert n_themes_row is not None
        cur.execute("SELECT count(*) FROM summary_entities WHERE summary_id = %s",
                    (sid,))
        n_entities_row = cur.fetchone()
        assert n_entities_row is not None
    assert n_temporal_row[0] == 2
    assert n_themes_row[0] == 2
    assert n_entities_row[0] == 2


def test_tier2_empty_short_marks_failed(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """JSON with empty `short` is invalid output — row goes to failed
    so the queue can be retried later."""
    monkeypatch.setattr(summarize, "_ollama_chat_json",
                        lambda *a, **kw: {"short": "", "action_required": False})
    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    _, status, _, _, _ = _summary_row(conn, fresh_msg)
    assert status == "failed"


def test_tier2_malformed_json_marks_failed(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """`_ollama_chat_json` raises ValueError on non-JSON — caller catches
    and marks the row failed (not stuck in pending)."""
    def boom(*a, **kw):
        raise ValueError("non-JSON response: 'lol'")
    monkeypatch.setattr(summarize, "_ollama_chat_json", boom)
    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    _, status, _, _, _ = _summary_row(conn, fresh_msg)
    assert status == "failed"


def test_tier2_network_error_marks_failed(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """A transient Ollama error on the structured path should also flip
    to failed — same recovery story as the free-text path."""
    def boom(*a, **kw):
        raise TimeoutError("ollama unreachable")
    monkeypatch.setattr(summarize, "_ollama_chat_json", boom)
    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    _, status, _, _, error = _summary_row(conn, fresh_msg)
    assert status == "failed"
    assert error and "ollama" in error.lower()


def test_tier2_partial_payload_persists_what_it_can(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """If the model omits temporal/themes/entities entirely, we still
    flip to done with the short — empty side tables are valid output."""
    monkeypatch.setattr(summarize, "_ollama_chat_json", lambda *a, **kw: {
        "short": "Only the short field.",
        "action_required": False,
    })
    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    sid, status, short, action_required, _ = _summary_row(conn, fresh_msg)
    assert status == "done"
    assert short == "Only the short field."
    assert action_required is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM summary_temporal WHERE summary_id = %s",
                    (sid,))
        n_row = cur.fetchone()
    assert n_row is not None and n_row[0] == 0


def test_tier2_skips_bad_temporal_keeps_good_ones(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """Mangled date strings on individual temporal rows are dropped, but
    valid sibling rows still land. The summary itself stays `done`."""
    monkeypatch.setattr(summarize, "_ollama_chat_json", lambda *a, **kw: {
        "short": "Summary with one good and one bad date.",
        "action_required": True,
        "temporal": [
            {"kind": "deadline", "occurs_at": "neste tirsdag", "note": "skipped"},
            {"kind": "deadline", "occurs_at": "2026-06-15",    "note": "good"},
        ],
        "themes": [],
        "entities": [],
    })
    monkeypatch.setattr(extract, "embed_batch", lambda texts: [])
    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    sid, status, _, _, _ = _summary_row(conn, fresh_msg)
    assert status == "done"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT note FROM summary_temporal WHERE summary_id = %s",
            (sid,),
        )
        notes = [r[0] for r in cur.fetchall()]
    assert notes == ["good"]


def test_tier2_finalize_rolls_back_on_side_table_error(
    conn, fresh_msg, tier2_sideeffect_cleanup, monkeypatch
):
    """If a side-table write raises, the whole finalize transaction
    rolls back and the row lands in `failed` — no half-applied state."""
    monkeypatch.setattr(summarize, "_ollama_chat_json", lambda *a, **kw: {
        "short": "Should never get committed.",
        "action_required": True,
        "temporal": [],
        "themes": ["pytest-t2-doomed"],
        "entities": [],
    })

    def boom_embed(_texts):
        raise RuntimeError("simulated embed failure")
    monkeypatch.setattr(extract, "embed_batch", boom_embed)

    _claim_tier2(conn, fresh_msg)
    summarize.generate_and_store(conn, fresh_msg, model=TEST_MODEL_TIER2)
    sid, status, _, _, _ = _summary_row(conn, fresh_msg)
    assert status == "failed"
    # No side-table rows: the finalize rolled back before committing.
    with conn.cursor() as cur:
        for tbl in ("summary_temporal", "summary_themes", "summary_entities"):
            cur.execute(f"SELECT count(*) FROM {tbl} WHERE summary_id = %s",
                        (sid,))
            n_row = cur.fetchone()
            assert n_row is not None
            assert n_row[0] == 0, f"{tbl} should be empty after rollback"


def test_tier1_still_uses_free_text_path(
    conn, fresh_msg, monkeypatch
):
    """A tier-1 row goes through `_ollama_chat` (free text), never
    touches `_ollama_chat_json`. The structured side tables stay empty."""
    json_called = []
    monkeypatch.setattr(summarize, "_ollama_chat_json",
                        lambda *a, **kw: json_called.append(a) or {})
    monkeypatch.setattr(summarize, "_ollama_chat",
                        lambda *a, **kw: "Tier-1 free text summary.")

    # Tier-1 claim — sentinel model + tier=1
    model = "pytest-tier1-fake"
    ok = summarize.claim_for_generation(conn, fresh_msg, model=model, tier=1)
    assert ok
    try:
        summarize.generate_and_store(conn, fresh_msg, model=model)
        assert json_called == [], "tier-1 must not call _ollama_chat_json"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.status::text, s.short, s.action_required
                FROM summaries s
                JOIN messages m ON m.id = s.message_id
                WHERE m.message_id = %s AND s.model = %s
                """,
                (fresh_msg, model),
            )
            row = cur.fetchone()
        assert row is not None
        status, short, action_required = row
        assert status == "done"
        assert short == "Tier-1 free text summary."
        # Tier-1 leaves action_required at its default (False).
        assert action_required is False
    finally:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM summaries
                WHERE model = %s
                  AND message_id = (SELECT id FROM messages WHERE message_id = %s)
                """,
                (model, fresh_msg),
            )
            conn.commit()
