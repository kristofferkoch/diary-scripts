"""Tests for signal_capture: receive-notification filtering and JSONL write."""
from __future__ import annotations

import json

from scripts import signal_capture as cap


def _notif(envelope: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": "receive",
                       "params": {"envelope": envelope, "account": "+4700000000"}})


def test_handle_line_writes_only_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "OUT_DIR", tmp_path)

    # an incoming text message → written
    cap.handle_line(_notif({
        "sourceName": "Alice", "sourceNumber": "+4700000001",
        "dataMessage": {"timestamp": 1780835700497, "message": "hi"},
    }), print_only=False)
    # a typing notification → dropped
    cap.handle_line(_notif({"typingMessage": {"action": "STARTED"}}), print_only=False)
    # a non-receive JSON-RPC line (e.g. a method response) → ignored
    cap.handle_line(json.dumps({"jsonrpc": "2.0", "result": {"version": "x"}, "id": 1}),
                    print_only=False)
    # malformed line → ignored, no crash
    cap.handle_line("not json at all", print_only=False)

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "2026-06-07.jsonl"  # day file is by message UTC date
    rows = [json.loads(l) for l in files[0].read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["text"] == "hi"
    assert rows[0]["from"]["name"] == "Alice"
    assert rows[0]["direction"] == "in"


def test_sent_message_records_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "OUT_DIR", tmp_path)
    cap.handle_line(_notif({
        "sourceName": "Me",
        "syncMessage": {"sentMessage": {
            "timestamp": 1780835760000, "message": "ok",
            "destinationName": "Alice", "destinationNumber": "+4700000001",
        }},
    }), print_only=False)
    rows = [json.loads(l) for l in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert rows[0]["direction"] == "out"
    assert rows[0]["to"]["name"] == "Alice"


def test_reaction_only_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "OUT_DIR", tmp_path)
    cap.handle_line(_notif({
        "sourceName": "Alice",
        "dataMessage": {"timestamp": 1, "reaction": {"emoji": "👍", "isRemove": False}},
    }), print_only=False)
    assert list(tmp_path.glob("*.jsonl")) == []
