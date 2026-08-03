"""Read-only access to stored per-mail summaries.

RETIRED (2026-08-02): summary *generation* is gone — the LLM chat endpoint
it depended on was decommissioned, and the workers / enqueue machinery
(`workers.py`, `summarize_inbox.py`, the Ollama chat calls) were removed in
the same pass. The `summaries` DB table and all historical rows stay, and
the webapp still renders already-stored `done` summaries; nothing new is
ever scheduled, so `pending` / `streaming` rows are legacy residue.

`done_stale` and `done_draft` are *computed* statuses returned by
`read_state` based on what's available across tiers and prompt versions
— the DB enum only carries the four transient values (see DESIGN.md §11).
"""
from __future__ import annotations

import os
from typing import TypedDict

import psycopg


# Frozen at the last prompt version that ever generated rows. Read-side
# queries (entities, agenda, this module) filter on it so only rows from
# the final extraction schema are surfaced.
# p3: tier-2 returns structured JSON (short + action_required + temporal
# + themes + entities). Tier-1 is unchanged free-text.
# p4: replace concrete few-shot examples with abstract patterns — qwen2.5
# was parroting the example facts ("nr 41463", "Eksempel Elektriske",
# "bekreft oppmøte innen tirsdag") into unrelated summaries.
# p5: drop the patterns from tier-1 too. The small model treated even
# abstract <placeholder> patterns as recipes and *hallucinated* invoices
# from LinkedIn notification mails. Rules-only prompt instead.
PROMPT_VERSION = os.environ.get("SUMMARY_PROMPT_VERSION", "p5")


class SummaryState(TypedDict):
    # Composite status, possibly broader than the DB enum:
    #   'done'        — best configured tier is done at current prompt.
    #   'done_draft'  — lower-tier done, higher tier still in flight (poll!)
    #   'done_stale'  — only an older-prompt-version done row exists.
    #   'pending' / 'streaming' / 'failed' — no done content yet.
    status: str
    short: str | None
    error: str | None
    # Only the tier-2 structured pass sets this; tier-1 / pending / failed
    # rows default to False. See migration 007.
    action_required: bool


def read_state(conn: psycopg.Connection, notmuch_msg_id: str) -> SummaryState | None:
    """Pick the best summary state to render for a mail. Order:
      1. Best `done` at current prompt_version → 'done' if its tier
         is MAX_TIER, else 'done_draft' if a higher tier is in flight,
         else 'done' (no better pass configured/expected).
      2. Latest in-flight (any pass) at current prompt_version → its raw
         status ('pending' or 'streaming').
      3. Old-prompt-version `done` (any tier) → 'done_stale'.
      4. Latest failed at current prompt_version → 'failed'.
      5. None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        mrid = row[0]

        # 1. best done at current prompt
        cur.execute(
            """
            SELECT short, quality_tier, action_required
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status = 'done'
            ORDER BY quality_tier DESC, generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        done = cur.fetchone()

        # any in-flight at current prompt, AND tier of highest in-flight
        cur.execute(
            """
            SELECT MAX(quality_tier)
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status IN ('pending', 'streaming')
            """,
            (mrid, PROMPT_VERSION),
        )
        inflight_max = cur.fetchone()
        max_inflight_tier = inflight_max[0] if inflight_max else None

        if done is not None:
            short, done_tier, action_required = done
            if max_inflight_tier is not None and max_inflight_tier > done_tier:
                status = "done_draft"
            else:
                status = "done"
            return {
                "status": status,
                "short": short,
                "error": None,
                "action_required": bool(action_required),
            }

        if max_inflight_tier is not None:
            # Pending text not done yet — return the most-recent pending
            # row's status so the UI shows the right placeholder.
            cur.execute(
                """
                SELECT status::text
                FROM summaries
                WHERE message_id = %s
                  AND prompt_version = %s
                  AND status IN ('pending', 'streaming')
                ORDER BY requested_at DESC, id DESC
                LIMIT 1
                """,
                (mrid, PROMPT_VERSION),
            )
            in_row = cur.fetchone()
            return {
                "status": in_row[0] if in_row else "pending",
                "short": "",
                "error": None,
                "action_required": False,
            }

        # 3. stale: latest done at an older prompt version. The
        # action_required column was added in p3; pre-p3 rows have the
        # column-default False, which is the right thing to render.
        cur.execute(
            """
            SELECT short, action_required
            FROM summaries
            WHERE message_id = %s
              AND status = 'done'
              AND prompt_version <> %s
            ORDER BY quality_tier DESC, generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        stale = cur.fetchone()
        if stale is not None:
            return {
                "status": "done_stale",
                "short": stale[0],
                "error": None,
                "action_required": bool(stale[1]),
            }

        # 4. failed at current
        cur.execute(
            """
            SELECT short, error
            FROM summaries
            WHERE message_id = %s
              AND prompt_version = %s
              AND status = 'failed'
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (mrid, PROMPT_VERSION),
        )
        failed = cur.fetchone()
        if failed is not None:
            return {
                "status": "failed",
                "short": failed[0],
                "error": failed[1],
                "action_required": False,
            }

    return None
