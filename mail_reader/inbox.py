"""Inbox listing via notmuch `tag:inbox` (kept in sync every 15 min by
the mail-sync timer).

Returns thread-level summaries, matching how Proton presents the inbox.
Clicking a thread opens the latest message in it (`latest_message_id_in_thread`).
"""
from __future__ import annotations

import json
import subprocess
from typing import TypedDict

import psycopg


class ThreadSummary(TypedDict, total=False):
    thread: str
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
    return json.loads(out)


def latest_message_id_in_thread(thread_id: str) -> str | None:
    """Newest Message-ID in a thread, without `<>` (notmuch's id: format)."""
    out = subprocess.run(
        ["notmuch", "search", "--format=json", "--output=messages",
         "--sort=newest-first", "--limit=1", f"thread:{thread_id}"],
        check=True, capture_output=True, text=True,
    ).stdout
    ids: list[str] = json.loads(out)
    return ids[0] if ids else None


def thread_latest_mids(conn: psycopg.Connection,
                       thread_ids: list[str]) -> dict[str, str]:
    """Newest embedded message_id for each thread, in a single query.

    Used by the inbox view to attach a summary card to every visible
    thread without N+1 calls into notmuch. Threads with no embedded
    messages are omitted from the result (rather than mapped to None) —
    callers can branch on `tid in result` to render the no-summary case.

    Accepts both notmuch's bare form (`00000000000348fc`) and the DB's
    prefixed form (`thread:00000000000348fc`). Keys in the returned
    dict match the *input* form so callers can look up by whatever they
    passed in.
    """
    if not thread_ids:
        return {}
    # Build a lookup back from prefixed → caller-provided form.
    prefixed_to_input: dict[str, str] = {}
    for tid in thread_ids:
        prefixed = tid if tid.startswith("thread:") else f"thread:{tid}"
        prefixed_to_input[prefixed] = tid
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (thread_id) thread_id, message_id
            FROM messages
            WHERE thread_id = ANY(%s)
            ORDER BY thread_id, date DESC
            """,
            (list(prefixed_to_input.keys()),),
        )
        rows = cur.fetchall()
    return {prefixed_to_input[tid]: mid for tid, mid in rows}
