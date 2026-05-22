"""Inbox listing via notmuch `tag:inbox` (kept in sync every 15 min by
the mail-sync timer).

Returns thread-level summaries, matching how Proton presents the inbox.
Clicking a thread opens the latest message in it (`latest_message_id_in_thread`).
"""
from __future__ import annotations

import json
import subprocess
from typing import TypedDict


class ThreadSummary(TypedDict):
    thread: str
    timestamp: int
    date_relative: str
    matched: int
    total: int
    authors: str
    subject: str
    tags: list[str]


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
