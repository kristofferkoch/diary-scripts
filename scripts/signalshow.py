#!/usr/bin/env python3
"""
Read captured Signal messages from memory/signal/ as readable text.

The conversation JSONL is written by `signal-capture` (the always-on sink
that reads the signal-cli JSON-RPC socket) and is NOT in git — it lives
beside the repo like the maildir. This is the sjekk-flow reader: it
replaces grepping `journalctl -u signal-mirror`, which is the daemon's
human stdout and loses messages to line-splitting and journald retention.

Cursor: memory/signal-state.json's `cursor` (an ISO timestamp). Unlike
spond's auto-bumped cursor, this is hand-advanced (like the mail cursor):
read with --since-cursor, triage, then `signalshow --bump` to set the
high-water mark to the newest message shown. Single writer (this tool);
signal-capture never touches the state file.

Examples:
    uv run signalshow --since-cursor                 # everything new since cursor
    uv run signalshow --since-cursor --headers-only  # one line per message
    uv run signalshow --since 2026-06-07             # from a date
    uv run signalshow --from alice                    # filter by sender name/number
    uv run signalshow --group                        # only group messages
    uv run signalshow --bump                         # advance cursor to newest shown
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mail_reader.config import workspace_root

PROJECT_ROOT = workspace_root()
STATE_PATH = PROJECT_ROOT / "memory" / "signal-state.json"
JSONL_DIR = PROJECT_ROOT / "memory" / "signal"


def parse_iso(s: str) -> datetime:
    """Parse ISO-8601 with trailing Z, normalising to UTC.

    >>> parse_iso("2026-06-07T12:35:00Z").isoformat()
    '2026-06-07T12:35:00+00:00'
    >>> parse_iso("2026-06-07").isoformat()
    '2026-06-07T00:00:00+00:00'
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
    raw = json.loads(STATE_PATH.read_text()).get("cursor")
    return parse_iso(raw) if raw else None


def save_cursor(iso: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"cursor": iso}, indent=2, ensure_ascii=False) + "\n"
    )


def iter_records(since: datetime | None) -> Iterator[dict[str, Any]]:
    if not JSONL_DIR.exists():
        return
    for fp in sorted(JSONL_DIR.glob("*.jsonl")):
        if since is not None:
            try:
                file_date = datetime.strptime(fp.stem, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
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
            if since is not None and rec.get("iso"):
                try:
                    if parse_iso(rec["iso"]) <= since:
                        continue
                except ValueError:
                    pass
            yield rec


def peer_label(rec: dict[str, Any]) -> str:
    """Who the message is with — the other party in a 1:1, or the group.

    >>> peer_label({"direction": "in", "from": {"name": "Alice"}, "group": None})
    'Alice'
    >>> peer_label({"direction": "out", "to": {"name": "Alice"}, "group": None})
    'Alice'
    >>> peer_label({"group": {"name": "Fam", "id": "g1"}})
    'Fam'
    >>> peer_label({"group": {"name": None, "id": "g1xyz"}})
    'group:g1xyz'
    >>> peer_label({"direction": "in", "from": {"name": None, "number": "+4700000000"}})
    '+4700000000'
    """
    g = rec.get("group")
    if g:
        return g.get("name") or f"group:{g.get('id')}"
    if rec.get("direction") == "out":
        to = rec.get("to") or {}
        return to.get("name") or to.get("number") or "?"
    frm = rec.get("from") or {}
    return frm.get("name") or frm.get("number") or "?"


def sender_label(rec: dict[str, Any]) -> str:
    """Display name of who wrote the message ('me' for the user's own sent).

    >>> sender_label({"direction": "out"})
    'me'
    >>> sender_label({"direction": "in", "from": {"name": "Alice"}})
    'Alice'
    """
    if rec.get("direction") == "out":
        return "me"
    frm = rec.get("from") or {}
    return frm.get("name") or frm.get("number") or "?"


def _trim(s: str, n: int = 200) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render(rec: dict[str, Any], *, headers_only: bool) -> str:
    arrow = "→" if rec.get("direction") == "out" else "←"
    peer = peer_label(rec)
    who = sender_label(rec)
    iso = rec.get("iso", "")
    text = rec.get("text") or ""
    atts = rec.get("attachments") or []
    att_note = ""
    if atts:
        kinds = ", ".join(a.get("contentType") or "?" for a in atts)
        att_note = f" [📎 {len(atts)}: {kinds}]"
    quote = rec.get("quote")
    q_note = ""
    if quote and quote.get("text"):
        q_note = f" (re: {_trim(quote['text'], 40)!r})"
    line = f"{iso} {arrow} {peer:<18.18} {who}: {_trim(text)}{q_note}{att_note}"
    if headers_only:
        return line
    return line  # bodies are short; full == header for now


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since-cursor", action="store_true",
                    help="Only messages after memory/signal-state.json's cursor.")
    ap.add_argument("--since", type=str, default=None,
                    help="ISO date/datetime; only messages strictly after this point.")
    ap.add_argument("--from", dest="from_", default=None, metavar="NEEDLE",
                    help="Only messages from a sender whose name or number contains this "
                         "(case-insensitive).")
    ap.add_argument("--with", dest="with_", default=None, metavar="NEEDLE",
                    help="Only messages whose peer (other party or group) matches this "
                         "(case-insensitive); includes your own replies in that thread.")
    ap.add_argument("--group", action="store_true", help="Only group messages.")
    ap.add_argument("--no-group", action="store_true", help="Only 1:1 (non-group) messages.")
    ap.add_argument("--headers-only", action="store_true",
                    help="One line per message (default rendering is already one line).")
    ap.add_argument("--bump", action="store_true",
                    help="After printing, set the cursor to the newest message shown.")
    args = ap.parse_args(argv)

    since: datetime | None
    if args.since_cursor:
        since = load_cursor()
        if since is not None:
            print(f"(cursor: {since.isoformat()})", file=sys.stderr)
        else:
            print("(no cursor yet — showing everything)", file=sys.stderr)
    elif args.since:
        since = parse_iso(args.since)
    else:
        since = None

    from_n = args.from_.lower() if args.from_ else None
    with_n = args.with_.lower() if args.with_ else None

    rows: list[dict[str, Any]] = []
    for rec in iter_records(since):
        if args.group and not rec.get("group"):
            continue
        if args.no_group and rec.get("group"):
            continue
        if from_n:
            frm = rec.get("from") or {}
            hay = f"{frm.get('name') or ''} {frm.get('number') or ''}".lower()
            if from_n not in hay:
                continue
        if with_n and with_n not in peer_label(rec).lower():
            continue
        rows.append(rec)

    rows.sort(key=lambda r: r.get("ts", 0))
    for rec in rows:
        print(render(rec, headers_only=args.headers_only))

    if not rows:
        print("(no matching messages)", file=sys.stderr)

    if args.bump and rows:
        newest = max(rows, key=lambda r: r.get("ts", 0)).get("iso")
        if newest:
            save_cursor(newest)
            print(f"(cursor bumped to {newest})", file=sys.stderr)

    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
