"""Inbox listing via notmuch `tag:inbox` (kept in sync every 15 min by
the mail-sync timer).

Returns thread-level summaries, matching how Proton presents the inbox.
Clicking a thread opens the latest message in it (`latest_message_id_in_thread`).
"""
from __future__ import annotations

import json
import subprocess
from typing import TypedDict, cast

import psycopg

from .thread_id import ThreadId


class ThreadSummary(TypedDict, total=False):
    thread: ThreadId
    timestamp: int
    date_relative: str
    matched: int
    total: int
    authors: str
    subject: str
    tags: list[str]
    # Attached by the inbox endpoint after `list_inbox()` so the row can
    # render a summary card. None when the latest message isn't embedded.
    summary_status: str | None
    summary: str | None
    summary_error: str | None
    summary_mid_quoted: str | None


def list_inbox(limit: int = 50, query: str = "tag:inbox") -> list[ThreadSummary]:
    """Latest threads matching `query`, newest first."""
    out = subprocess.run(
        ["notmuch", "search", "--format=json", "--output=summary",
         "--sort=newest-first", f"--limit={limit}", query],
        check=True, capture_output=True, text=True,
    ).stdout
    raw: list[dict] = json.loads(out)
    # notmuch's JSON returns the bare form; wrap at the boundary so
    # downstream code never sees a raw str thread id.
    for row in raw:
        if "thread" in row:
            row["thread"] = ThreadId(row["thread"])
    return cast(list[ThreadSummary], raw)


def latest_message_id_in_thread(thread_id: ThreadId) -> str | None:
    """Newest Message-ID in a thread, without `<>` (notmuch's id: format)."""
    out = subprocess.run(
        ["notmuch", "search", "--format=json", "--output=messages",
         "--sort=newest-first", "--limit=1", thread_id.notmuch_query],
        check=True, capture_output=True, text=True,
    ).stdout
    ids: list[str] = json.loads(out)
    return ids[0] if ids else None


def thread_latest_mids(conn: psycopg.Connection,
                       thread_ids: list[ThreadId]) -> dict[ThreadId, str]:
    """Newest embedded message_id for each thread, in a single query.

    Used by the inbox view to attach a summary card to every visible
    thread without N+1 calls into notmuch. Threads with no embedded
    messages are omitted from the result (rather than mapped to None) —
    callers can branch on `tid in result` to render the no-summary case.
    """
    if not thread_ids:
        return {}
    db_to_tid: dict[str, ThreadId] = {tid.db_form: tid for tid in thread_ids}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (thread_id) thread_id, message_id
            FROM messages
            WHERE thread_id = ANY(%s)
            ORDER BY thread_id, date DESC
            """,
            (list(db_to_tid.keys()),),
        )
        rows = cur.fetchall()
    return {db_to_tid[db_form]: mid for db_form, mid in rows}
