#!/usr/bin/env python3
"""
Read Spond JSONL records from memory/spond/ as readable text.

Examples:
    scripts/spondshow.py --since-cursor                    # everything new since cursor
    scripts/spondshow.py --since-cursor --kind chat
    scripts/spondshow.py --since 2026-05-20 --kind event
    scripts/spondshow.py --headers-only --since-cursor     # one-line summary per record
    scripts/spondshow.py --chat <chat-id>                  # all records for a specific chat
    scripts/spondshow.py --kind event --future             # only future events
    SPOND_RSVP_MEMBER_ID=<id> scripts/spondshow.py ...     # surface RSVP markers

Mirrors `mailshow.py --since-cursor` — the cursor is
memory/spond-state.json's `last_successful_run`, written by `spond_sync.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "memory" / "spond-state.json"
JSONL_DIR = PROJECT_ROOT / "memory" / "spond"
RSVP_ENV = "SPOND_RSVP_MEMBER_ID"

# Module-level — set in main(); read by render_header() when rendering events.
_RSVP_MEMBER_ID: str | None = None

RSVP_MARK = {
    "accepted":    "✓",
    "declined":    "✗",
    "unanswered":  "?",
    "waitinglist": "W",
    "unconfirmed": "u",
}


def rsvp_status(event_data: dict[str, Any], member_id: str) -> str | None:
    """Return Spond response status for `member_id` in an event record, or None if not listed.

    >>> ev = {"responses": {"acceptedIds": ["A"], "declinedIds": ["B"], "unansweredIds": ["C"]}}
    >>> rsvp_status(ev, "A")
    'accepted'
    >>> rsvp_status(ev, "B")
    'declined'
    >>> rsvp_status(ev, "C")
    'unanswered'
    >>> rsvp_status(ev, "Z") is None
    True
    >>> rsvp_status({}, "A") is None
    True
    """
    responses = event_data.get("responses") or {}
    for status, key in (
        ("accepted", "acceptedIds"),
        ("declined", "declinedIds"),
        ("unanswered", "unansweredIds"),
        ("waitinglist", "waitinglistIds"),
        ("unconfirmed", "unconfirmedIds"),
    ):
        if member_id in (responses.get(key) or []):
            return status
    return None


def parse_iso(s: str) -> datetime:
    """Parse ISO-8601 with trailing Z, normalising to UTC.

    >>> parse_iso("2026-05-26T11:30:00Z").isoformat()
    '2026-05-26T11:30:00+00:00'
    >>> parse_iso("2026-05-20").isoformat()
    '2026-05-20T00:00:00+00:00'
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_cursor() -> datetime | None:
    if not STATE_PATH.exists():
        return None
    raw = json.loads(STATE_PATH.read_text()).get("last_successful_run")
    return parse_iso(raw) if raw else None


def iter_records(since: datetime | None, kinds: set[str] | None) -> Iterator[dict[str, Any]]:
    if not JSONL_DIR.exists():
        return
    for fp in sorted(JSONL_DIR.glob("*.jsonl")):
        if since is not None:
            try:
                file_date = datetime.strptime(fp.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date.date() < since.date():
                    continue
            except ValueError:
                pass
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds and rec.get("kind") not in kinds:
                continue
            if since is not None:
                fetched = rec.get("fetched_at")
                if fetched:
                    try:
                        if parse_iso(fetched) < since:
                            continue
                    except ValueError:
                        pass
            yield rec


def _trim(s: str, n: int = 80) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_header(rec: dict[str, Any]) -> str:
    kind = rec.get("kind", "?")
    data = rec.get("data") or {}
    if kind == "chat":
        name = data.get("name") or "(no name)"
        newest = data.get("newestTimestamp") or rec.get("fetched_at", "")
        unread = "U " if data.get("unread") else "  "
        msg = data.get("message") or {}
        mtype = msg.get("type") or ""
        # message bodies live in msg.text for TEXT; RENAME has msg.newName, etc.
        snippet = msg.get("text") or msg.get("newName") or ""
        cid = data.get("id") or "?"
        return f"[chat ] {unread}{newest} id={cid} name={_trim(str(name), 40)!r} msg[{mtype}]={_trim(str(snippet))!r}"
    if kind == "event":
        name = data.get("heading") or "(no name)"
        start = data.get("startTimestamp") or ""
        loc = (data.get("location") or {}).get("feature", "")
        eid = data.get("id") or "?"
        if _RSVP_MEMBER_ID:
            s = rsvp_status(data, _RSVP_MEMBER_ID)
            mark = RSVP_MARK.get(s or "", "—")
            return f"[event {mark}] {start} id={eid} loc={_trim(str(loc), 30)!r} name={_trim(str(name))!r}"
        return f"[event] {start} id={eid} loc={_trim(str(loc), 30)!r} name={_trim(str(name))!r}"
    if kind == "post":
        title = data.get("title") or ""
        timestamp = data.get("timestamp") or rec.get("fetched_at", "")
        unread = "U " if data.get("unread") else "  "
        pid = data.get("id") or "?"
        return f"[post ] {unread}{timestamp} id={pid} title={_trim(str(title))!r}"
    return f"[{kind}] {rec.get('fetched_at','')} data={_trim(json.dumps(data, ensure_ascii=False), 120)}"


def render_full(rec: dict[str, Any], max_chars: int) -> str:
    blob = json.dumps(rec.get("data", {}), ensure_ascii=False, indent=2)
    if max_chars and len(blob) > max_chars:
        blob = blob[:max_chars] + f"\n…[truncated at {max_chars} chars]"
    return render_header(rec) + "\n" + blob


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since-cursor", action="store_true",
                    help="Start from memory/spond-state.json's last_successful_run.")
    ap.add_argument("--since", type=str, default=None,
                    help="ISO date or datetime; only records fetched at-or-after this point.")
    ap.add_argument("--kind", action="append",
                    choices=sorted(["chat", "event", "post"]), default=None,
                    help="Restrict to one or more kinds (repeat the flag).")
    ap.add_argument("--chat", default=None, help="Only records whose data.id matches this chat id.")
    ap.add_argument("--event", default=None, help="Only records whose data.id matches this event id.")
    ap.add_argument("--future", action="store_true",
                    help="For events: only those with startTimestamp >= now. No effect on chats/posts.")
    ap.add_argument("--rsvp-as", default=os.environ.get(RSVP_ENV), metavar="MEMBER_ID",
                    help=f"Show RSVP status for this Spond member-id in event headers "
                         f"(default: ${RSVP_ENV} if set). Markers: ✓ accepted, ✗ declined, "
                         f"? unanswered, W waitinglist, u unconfirmed, — not on guest list.")
    ap.add_argument("--headers-only", action="store_true",
                    help="One-line summary per record; no JSON body.")
    ap.add_argument("--max-chars", type=int, default=4000,
                    help="Truncate per-record JSON body at N chars (default 4000, 0=no truncation).")
    args = ap.parse_args(argv)

    since: datetime | None
    if args.since_cursor:
        since = load_cursor()
        if since is None:
            print("(no cursor yet — run spond_sync.py first)", file=sys.stderr)
            return 1
        print(f"(cursor: {since.isoformat()})", file=sys.stderr)
    elif args.since:
        since = parse_iso(args.since)
    else:
        since = None

    kinds = set(args.kind) if args.kind else None
    now = datetime.now(timezone.utc) if args.future else None
    global _RSVP_MEMBER_ID
    _RSVP_MEMBER_ID = args.rsvp_as

    count = 0
    for rec in iter_records(since, kinds):
        data = rec.get("data") or {}
        rec_id = data.get("id") or data.get("uid") or data.get("chatId")
        if args.chat and rec.get("kind") == "chat" and rec_id != args.chat:
            continue
        if args.event and rec.get("kind") == "event" and rec_id != args.event:
            continue
        if now is not None and rec.get("kind") == "event":
            start = data.get("startTimestamp")
            if not start:
                continue
            try:
                if parse_iso(start) < now:
                    continue
            except ValueError:
                continue
        if args.headers_only:
            print(render_header(rec))
        else:
            print(render_full(rec, args.max_chars))
            print()
        count += 1

    if count == 0:
        print("(no matching records)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
