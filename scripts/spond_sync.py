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
    "seen_chat_activity":  { "<chatId>":  "<lastMessage.id or updatedAt>" },
    "seen_event_activity": { "<eventId>": "<activity-key>" },
    "seen_post_ids":       [ "<postId>", ... ],
    "last_successful_run": "2026-05-26T11:30:00Z"
  }

Events are tracked by a *content key* (see `event_activity_key`), not a
flat id-set, so a re-fetch re-emits an event when it is rescheduled,
cancelled, or the tracked member's RSVP changes. (Earlier versions used a
`seen_event_ids` set keyed on id alone — that silently swallowed RSVP
changes on already-seen events. Lesson 2026-05-29.) A legacy
`seen_event_ids` value is dropped on first run, which re-emits current
events once with their proper keys to re-baseline.

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

from mail_reader.config import cfg, workspace_root

PROJECT_ROOT = workspace_root()
STATE_PATH = PROJECT_ROOT / "memory" / "spond-state.json"
OUT_DIR = PROJECT_ROOT / "memory" / "spond"
DEFAULT_PASSWORD_CMD = "pass show spond/user"

VALID_KINDS = {"chat", "event", "post"}

# Same buckets spondshow.rsvp_status reads; ordered most→least common.
RSVP_ID_KEYS = (
    "acceptedIds",
    "declinedIds",
    "unansweredIds",
    "waitinglistIds",
    "unconfirmedIds",
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "seen_chat_activity": {},
            "seen_event_activity": {},
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


def member_rsvp(event: dict[str, Any], member_id: str | None) -> str:
    """Return the RSVP-bucket name holding `member_id` for this event, or ''.

    Mirrors spondshow.rsvp_status but returns the raw bucket key (stable for
    use in a cursor key) and never None. Empty when no member is tracked or
    the member isn't on the guest list.

    >>> ev = {"responses": {"acceptedIds": ["A"], "declinedIds": ["B"]}}
    >>> member_rsvp(ev, "A")
    'acceptedIds'
    >>> member_rsvp(ev, "B")
    'declinedIds'
    >>> member_rsvp(ev, "Z")
    ''
    >>> member_rsvp(ev, None)
    ''
    """
    if not member_id:
        return ""
    responses = event.get("responses") or {}
    for key in RSVP_ID_KEYS:
        if member_id in (responses.get(key) or []):
            return key
    return ""


def event_activity_key(event: dict[str, Any], member_id: str | None) -> str:
    """Cursor key per event — bumps on reschedule, cancellation, or the
    tracked member's RSVP change.

    Only the tracked member's own response is folded in (not the full
    responses object), so other parents answering doesn't re-emit a 40-member
    team's events on every sync. When no member is tracked the key still
    catches reschedule/cancellation, but not RSVP — run with
    $SPOND_RSVP_MEMBER_ID set to catch your own accept/decline.

    >>> event_activity_key({"startTimestamp": "2026-05-30T08:30:00Z"}, None)
    '2026-05-30T08:30:00Z||active|'
    >>> ev = {"startTimestamp": "2026-05-30T08:30:00Z",
    ...       "responses": {"unansweredIds": ["H"]}}
    >>> event_activity_key(ev, "H")
    '2026-05-30T08:30:00Z||active|unansweredIds'
    >>> ev["responses"] = {"acceptedIds": ["H"]}   # member accepts
    >>> event_activity_key(ev, "H")
    '2026-05-30T08:30:00Z||active|acceptedIds'
    >>> event_activity_key({"cancelled": True}, None)
    '||cancelled|'
    """
    return "|".join((
        str(event.get("startTimestamp") or ""),
        str(event.get("endTimestamp") or ""),
        "cancelled" if event.get("cancelled") else "active",
        member_rsvp(event, member_id),
    ))


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


async def fetch_events(
    s: Any, state: dict[str, Any], out: list[dict[str, Any]], member_id: str | None
) -> int:
    events = await s.get_events() or []
    # Drop the legacy id-only set (re-baselines current events with content keys).
    state.pop("seen_event_ids", None)
    seen = state.setdefault("seen_event_activity", {})
    fetched_at = utcnow_iso()
    new = 0
    for ev in events:
        eid = ev.get("id") or ev.get("uid")
        if eid is None:
            continue
        key = event_activity_key(ev, member_id)
        if seen.get(eid) == key:
            continue
        out.append({"kind": "event", "fetched_at": fetched_at, "data": ev})
        seen[eid] = key
        new += 1
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

    # Env wins; fall back to [spond] in config/local.toml so these survive a
    # restore (they used to be ephemeral shell exports — see
    # backup/RESTORE-2026-06-22.md). password_cmd stays a `pass` reference.
    user = os.environ.get("SPOND_USERNAME") or cfg("spond.username", None)
    if not user:
        raise SystemExit("SPOND_USERNAME not set (env or [spond].username in config)")
    pw_cmd = os.environ.get("SPOND_PASSWORD_CMD") or cfg("spond.password_cmd", DEFAULT_PASSWORD_CMD)
    pw = get_password(pw_cmd)

    member_id = os.environ.get("SPOND_RSVP_MEMBER_ID") or cfg("spond.rsvp_member_id", None)
    if "event" in kinds and not member_id:
        print(
            "!! SPOND_RSVP_MEMBER_ID unset — RSVP changes on already-seen "
            "events will NOT be detected (only reschedule/cancellation).",
            file=sys.stderr,
        )

    s = spond.Spond(username=user, password=pw)
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    try:
        if "chat" in kinds:
            counts["chat"] = await fetch_chats(s, state, records)
        if "event" in kinds:
            counts["event"] = await fetch_events(s, state, records, member_id)
        if "post" in kinds:
            counts["post"] = await fetch_posts(s, state, records)
    finally:
        await s.clientsession.close()
    return sum(counts.values()), records, counts


def main(argv: list[str] | None = None) -> int:
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
