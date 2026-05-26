#!/usr/bin/env python3
"""
Pull new activity from Spond and append raw JSONL to memory/spond/.

Surfaces fetched (Olen/Spond public API):
  chat   — chat-level metadata + last message (the upstream lib does NOT
           expose the full per-chat message history; this captures "new
           activity in chat X" signals)
  event  — kamper/treninger (Olen/Spond default: future-only)
  post   — gruppevegg (klubb-feed); kept raw, deliberately low-priority

Payments / Spond Pay are NOT supported by the upstream library; receipts
arrive over mail anyway and are already captured by the mail pipeline.

State cursor: memory/spond-state.json
  {
    "seen_chat_activity": { "<chatId>": "<lastMessage.id or updatedAt>" },
    "seen_event_ids":     [ "<eventId>", ... ],
    "seen_post_ids":      [ "<postId>", ... ],
    "last_successful_run": "2026-05-26T11:30:00Z"
  }

Raw output: memory/spond/YYYY-MM-DD.jsonl
  One JSON object per line:
    {"kind": "chat"|"event"|"post", "fetched_at": "...Z", "data": {...}}

Auth: SPOND_USERNAME from env; password from `pass show spond/user`
(override with $SPOND_PASSWORD_CMD).

Examples:
    uv run scripts/spond_sync.py --once
    uv run scripts/spond_sync.py --once --kinds chat,event
    uv run scripts/spond_sync.py --once --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "memory" / "spond-state.json"
OUT_DIR = PROJECT_ROOT / "memory" / "spond"
DEFAULT_PASSWORD_CMD = "pass show spond/user"

VALID_KINDS = {"chat", "event", "post"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "seen_chat_activity": {},
            "seen_event_ids": [],
            "seen_post_ids": [],
            "last_successful_run": None,
        }
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_successful_run"] = utcnow_iso()
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def get_password(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
    pw = result.stdout.strip()
    if not pw:
        raise RuntimeError(f"password command produced empty output: {cmd!r}")
    return pw


def today_jsonl() -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{today}.jsonl"


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def chat_activity_key(chat: dict[str, Any]) -> str:
    """Cursor key per chat — bumps whenever last message changes.

    >>> chat_activity_key({"id": "c1", "lastMessage": {"id": "m1"}})
    'm1'
    >>> chat_activity_key({"id": "c1", "updatedAt": "2026-01-02T00:00:00Z"})
    '2026-01-02T00:00:00Z'
    >>> chat_activity_key({"id": "c1"})
    'c1'
    """
    last = chat.get("lastMessage") or {}
    return (
        last.get("id")
        or last.get("uid")
        or chat.get("updatedAt")
        or chat.get("modifiedTime")
        or chat.get("id", "")
    )


async def fetch_chats(s: Any, state: dict[str, Any], out: list[dict[str, Any]]) -> int:
    chats = await s.get_messages(max_chats=100) or []
    seen = state.setdefault("seen_chat_activity", {})
    fetched_at = utcnow_iso()
    new = 0
    for c in chats:
        cid = c.get("id") or c.get("chatId")
        if cid is None:
            continue
        key = chat_activity_key(c)
        if seen.get(cid) == key:
            continue
        out.append({"kind": "chat", "fetched_at": fetched_at, "data": c})
        seen[cid] = key
        new += 1
    return new


async def fetch_events(s: Any, state: dict[str, Any], out: list[dict[str, Any]]) -> int:
    events = await s.get_events() or []
    seen = set(state.setdefault("seen_event_ids", []))
    fetched_at = utcnow_iso()
    new = 0
    for ev in events:
        eid = ev.get("id") or ev.get("uid")
        if eid is None or eid in seen:
            continue
        out.append({"kind": "event", "fetched_at": fetched_at, "data": ev})
        seen.add(eid)
        new += 1
    state["seen_event_ids"] = sorted(seen)
    return new


async def fetch_posts(s: Any, state: dict[str, Any], out: list[dict[str, Any]]) -> int:
    posts = await s.get_posts(max_posts=50, include_comments=True) or []
    seen = set(state.setdefault("seen_post_ids", []))
    fetched_at = utcnow_iso()
    new = 0
    for p in posts:
        pid = p.get("id") or p.get("uid")
        if pid is None or pid in seen:
            continue
        out.append({"kind": "post", "fetched_at": fetched_at, "data": p})
        seen.add(pid)
        new += 1
    state["seen_post_ids"] = sorted(seen)
    return new


async def run_once(
    kinds: set[str], state: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], dict[str, int]]:
    # Late import — keeps `--help` and tests cheap if spond isn't installed.
    from spond import spond

    user = os.environ.get("SPOND_USERNAME")
    if not user:
        raise SystemExit("SPOND_USERNAME env var not set")
    pw_cmd = os.environ.get("SPOND_PASSWORD_CMD", DEFAULT_PASSWORD_CMD)
    pw = get_password(pw_cmd)

    s = spond.Spond(username=user, password=pw)
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    try:
        if "chat" in kinds:
            counts["chat"] = await fetch_chats(s, state, records)
        if "event" in kinds:
            counts["event"] = await fetch_events(s, state, records)
        if "post" in kinds:
            counts["post"] = await fetch_posts(s, state, records)
    finally:
        await s.clientsession.close()
    return sum(counts.values()), records, counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--once", action="store_true",
                    help="One-shot fetch (only mode for now; no daemon).")
    ap.add_argument("--kinds", default="chat,event,post",
                    help=f"Comma-separated subset of {sorted(VALID_KINDS)}.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print new records to stdout; do NOT write JSONL or update state.")
    args = ap.parse_args(argv)

    if not args.once:
        ap.error("--once is required (no daemon mode yet)")

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    bad = kinds - VALID_KINDS
    if bad:
        ap.error(f"unknown kinds: {sorted(bad)}; valid: {sorted(VALID_KINDS)}")

    state = load_state()
    total, records, counts = asyncio.run(run_once(kinds, state))
    print(f"new records: total={total}, by_kind={counts}", file=sys.stderr)

    if args.dry_run:
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    if total > 0:
        append_jsonl(today_jsonl(), records)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
