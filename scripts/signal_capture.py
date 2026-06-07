#!/usr/bin/env python3
"""
Capture Signal messages from the signal-cli daemon's JSON-RPC socket into
structured JSONL under memory/signal/.

The `signal-mirror` daemon runs `signal-cli ... daemon --socket
--receive-mode on-start`, which receives continuously (keeping the linked
device alive) and pushes every received envelope to all connected JSON-RPC
clients as newline-delimited `{"method":"receive","params":{...}}`
notifications. This connects as one such client and appends a normalised
record per *conversation* message (incoming + the user's own sent messages
mirrored via syncMessage) to memory/signal/YYYY-MM-DD.jsonl.

Connecting to the *existing* daemon's socket is the sanctioned IPC — it is
NOT a second receiver and does not ACK independently (the daemon owns the
receive loop), so it does not violate the "never run a second `signal-cli
receive`" rule (network/docs/signal-cli-mirror.md). The journal stays as
the human/live feed and a backstop.

Push model caveat: a client only sees messages that arrive while it is
connected — signal-cli does not replay history to late clients (the daemon
already ACKed them server-side). So this must run continuously; it's a
Restart=always systemd --user service (signal-capture.service). Messages
received while it is down survive only in the journal.

Noise (typingMessage, receiptMessage, read receipts, reaction-only and
empty envelopes) is dropped — scope is conversations, not telemetry. The
full envelope is kept under "raw" so nothing is lost structurally.

Output record (memory/signal/YYYY-MM-DD.jsonl, one JSON object per line):
    {
      "kind": "message" | "sent",      # incoming vs the user's own (sync)
      "direction": "in" | "out",
      "ts": 1780835700497,             # message timestamp (epoch ms)
      "iso": "2026-06-07T12:35:00Z",
      "fetched_at": "...Z",
      "from": {"name": ..., "number": ..., "uuid": ...},
      "to":   {"name": ..., "number": ...} | null,   # sent: destination
      "group": {"id": ..., "name": ...} | null,
      "text": "...",
      "quote": {"author": ..., "text": ...} | null,
      "attachments": [{"contentType":..., "filename":..., "size":..., "id":...}],
      "raw": {<full envelope>}
    }

Examples:
    uv run signal-capture                 # run the sink (systemd does this)
    uv run signal-capture --print         # normalise to stdout, don't write (debug)
    uv run signal-capture --socket /path/to/socket
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mail_reader.config import workspace_root

PROJECT_ROOT = workspace_root()
OUT_DIR = PROJECT_ROOT / "memory" / "signal"


def default_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.environ.get("SIGNAL_CLI_SOCKET", f"{runtime}/signal-cli/socket")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_to_iso(ts: int) -> str:
    """Epoch-ms → UTC ISO-8601 with trailing Z.

    >>> ms_to_iso(1780835700497)
    '2026-06-07T12:35:00Z'
    >>> ms_to_iso(0)
    '1970-01-01T00:00:00Z'
    """
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _attachments(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten signal-cli attachment dicts to metadata only (no blobs).

    >>> _attachments({"attachments": [{"contentType": "image/jpeg",
    ...   "filename": "x.jpg", "size": 12, "id": "a1", "extra": "dropped"}]})
    [{'contentType': 'image/jpeg', 'filename': 'x.jpg', 'size': 12, 'id': 'a1'}]
    >>> _attachments({})
    []
    """
    out = []
    for a in msg.get("attachments") or []:
        out.append(
            {
                "contentType": a.get("contentType"),
                "filename": a.get("filename"),
                "size": a.get("size"),
                "id": a.get("id"),
            }
        )
    return out


def _quote(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a reply-quote's author + text, or None.

    >>> _quote({"quote": {"author": "+4700000000", "authorNumber": "+4700000000", "text": "hi"}})
    {'author': '+4700000000', 'text': 'hi'}
    >>> _quote({}) is None
    True
    """
    q = msg.get("quote")
    if not q:
        return None
    return {"author": q.get("authorNumber") or q.get("author"), "text": q.get("text")}


def _group(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Extract group id + name from a data/sent message, or None for 1:1.

    >>> _group({"groupInfo": {"groupId": "g1", "groupName": "Fam"}})
    {'id': 'g1', 'name': 'Fam'}
    >>> _group({"groupInfo": {"groupId": "g1"}})
    {'id': 'g1', 'name': None}
    >>> _group({}) is None
    True
    """
    g = msg.get("groupInfo")
    if not g:
        return None
    return {"id": g.get("groupId"), "name": g.get("groupName")}


def normalize(envelope: dict[str, Any], fetched_at: str) -> dict[str, Any] | None:
    """Turn a signal-cli receive envelope into a flat conversation record.

    Returns None for noise (typing/receipt/read, reaction-only, empty), so
    the caller writes only real conversation messages.

    Incoming text message:
    >>> env = {"sourceName": "Alice", "sourceNumber": "+4700000000", "sourceUuid": "u1",
    ...        "dataMessage": {"timestamp": 1780835700497, "message": "hi"}}
    >>> r = normalize(env, "2026-06-07T13:00:00Z")
    >>> r["kind"], r["direction"], r["text"], r["from"]["name"], r["iso"]
    ('message', 'in', 'hi', 'Alice', '2026-06-07T12:35:00Z')

    The user's own message, mirrored via syncMessage.sentMessage:
    >>> env = {"sourceName": "Me", "syncMessage": {"sentMessage": {
    ...        "timestamp": 1780835760000, "message": "think so",
    ...        "destinationNumber": "+4700000000", "destinationName": "Alice"}}}
    >>> r = normalize(env, "2026-06-07T13:00:00Z")
    >>> r["kind"], r["direction"], r["text"], r["to"]["name"]
    ('sent', 'out', 'think so', 'Alice')

    Typing / receipt / reaction-only / empty → dropped:
    >>> normalize({"typingMessage": {"action": "STARTED"}}, "z") is None
    True
    >>> normalize({"receiptMessage": {"isDelivery": True}}, "z") is None
    True
    >>> normalize({"dataMessage": {"timestamp": 1, "reaction": {"emoji": "👍"}}}, "z") is None
    True
    >>> normalize({"dataMessage": {"timestamp": 1, "message": None}}, "z") is None
    True
    """
    sync = envelope.get("syncMessage") or {}
    sent = sync.get("sentMessage")
    data = envelope.get("dataMessage")

    if sent is not None:
        msg = sent
        text = msg.get("message")
        atts = _attachments(msg)
        if not text and not atts:
            return None
        ts = msg.get("timestamp") or envelope.get("timestamp") or 0
        return {
            "kind": "sent",
            "direction": "out",
            "ts": ts,
            "iso": ms_to_iso(ts),
            "fetched_at": fetched_at,
            "from": {
                "name": envelope.get("sourceName"),
                "number": envelope.get("sourceNumber"),
                "uuid": envelope.get("sourceUuid"),
            },
            "to": {
                "name": msg.get("destinationName"),
                "number": msg.get("destinationNumber") or msg.get("destination"),
            },
            "group": _group(msg),
            "text": text,
            "quote": _quote(msg),
            "attachments": atts,
            "raw": envelope,
        }

    if data is not None:
        text = data.get("message")
        atts = _attachments(data)
        # reaction-only / empty data messages are not conversation content
        if not text and not atts:
            return None
        ts = data.get("timestamp") or envelope.get("timestamp") or 0
        return {
            "kind": "message",
            "direction": "in",
            "ts": ts,
            "iso": ms_to_iso(ts),
            "fetched_at": fetched_at,
            "from": {
                "name": envelope.get("sourceName"),
                "number": envelope.get("sourceNumber"),
                "uuid": envelope.get("sourceUuid"),
            },
            "to": None,
            "group": _group(data),
            "text": text,
            "quote": _quote(data),
            "attachments": atts,
            "raw": envelope,
        }

    # typingMessage, receiptMessage, readMessages-only syncs, etc.
    return None


def jsonl_for(iso: str) -> Path:
    """Day file (UTC) for a record's iso timestamp.

    >>> jsonl_for("2026-06-07T12:35:00Z").name
    '2026-06-07.jsonl'
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{iso[:10]}.jsonl"


def append_record(rec: dict[str, Any]) -> None:
    path = jsonl_for(rec["iso"])
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def handle_line(line: str, *, print_only: bool) -> None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if obj.get("method") != "receive":
        return
    envelope = (obj.get("params") or {}).get("envelope")
    if not envelope:
        return
    rec = normalize(envelope, utcnow_iso())
    if rec is None:
        return
    if print_only:
        print(json.dumps(rec, ensure_ascii=False))
    else:
        append_record(rec)


def run(sock_path: str, *, print_only: bool) -> int:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    print(f"signal-capture: connected to {sock_path}", file=sys.stderr)
    buf = b""
    with s:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                print("signal-capture: socket closed by daemon", file=sys.stderr)
                return 1  # let systemd Restart=always reconnect
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode("utf-8", errors="replace").strip()
                if line:
                    handle_line(line, print_only=print_only)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--socket", default=default_socket(),
                    help="signal-cli JSON-RPC UNIX socket (default $SIGNAL_CLI_SOCKET "
                         "or $XDG_RUNTIME_DIR/signal-cli/socket).")
    ap.add_argument("--print", action="store_true", dest="print_only",
                    help="Normalise to stdout; do NOT write JSONL (debug).")
    args = ap.parse_args(argv)
    try:
        return run(args.socket, print_only=args.print_only)
    except (ConnectionError, FileNotFoundError) as e:
        print(f"signal-capture: cannot connect to {args.socket}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
