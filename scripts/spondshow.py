#!/usr/bin/env python3
"""
Read Spond JSONL records from memory/spond/ as readable text.

Examples:
    scripts/spondshow.py --since-cursor                    # everything new since cursor
    scripts/spondshow.py --since-cursor --kind chat
    scripts/spondshow.py --since 2026-05-20 --kind event
    scripts/spondshow.py --headers-only --since-cursor     # one-line summary per record
    scripts/spondshow.py --chat <chat-id>                  # all records for a specific chat

Mirrors `mailshow.py --since-cursor` — the cursor is
memory/spond-state.json's `last_successful_run`, written by `spond_sync.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "memory" / "spond-state.json"
JSONL_DIR = PROJECT_ROOT / "memory" / "spond"


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
    fetched = rec.get("fetched_at", "")
    data = rec.get("data") or {}
    if kind == "chat":
        name = data.get("name") or data.get("title") or "(no name)"
        last = data.get("lastMessage") or {}
        text = last.get("text") or last.get("message") or ""
        cid = data.get("id") or data.get("chatId") or "?"
        return f"[chat ] {fetched} id={cid} name={_trim(str(name), 40)!r} last={_trim(str(text))!r}"
    if kind == "event":
        name = data.get("heading") or data.get("name") or "(no name)"
        start = data.get("startTimestamp") or data.get("start") or ""
        eid = data.get("id") or data.get("uid") or "?"
        return f"[event] {fetched} id={eid} start={start} name={_trim(str(name))!r}"
    if kind == "post":
        text = data.get("text") or data.get("message") or ""
        pid = data.get("id") or data.get("uid") or "?"
        return f"[post ] {fetched} id={pid} text={_trim(str(text))!r}"
    return f"[{kind}] {fetched} data={_trim(json.dumps(data, ensure_ascii=False), 120)}"


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

    count = 0
    for rec in iter_records(since, kinds):
        data = rec.get("data") or {}
        rec_id = data.get("id") or data.get("uid") or data.get("chatId")
        if args.chat and rec.get("kind") == "chat" and rec_id != args.chat:
            continue
        if args.event and rec.get("kind") == "event" and rec_id != args.event:
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
