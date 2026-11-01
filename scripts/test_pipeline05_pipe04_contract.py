"""Cross-contract tests using the actual concurrent PIPE-04 implementation."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from scripts import signal_notify_proxy as proxy

_PIPE04_ROOT = Path(os.environ.get(
    "DIARY_PIPE04_CHECKOUT",
    str(Path(__file__).resolve().parents[2] / "pipe04-safe-rendering-scripts"),
))
_PIPE04_MODULE = _PIPE04_ROOT / "scripts" / "pipeline_notifications.py"
if not _PIPE04_MODULE.is_file():
    pytest.skip("PIPE-04 checkout is unavailable for cross-contract tests", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("scripts.pipeline_notifications", _PIPE04_MODULE)
assert _spec is not None and _spec.loader is not None
pipe04 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pipe04
_spec.loader.exec_module(pipe04)

CALLER = {"pid": 101, "uid": 4200, "gid": 4200}


def event_id(number):
    return f"evt_{number:064x}"


def notification(kind, event_ids, body):
    return pipe04.Notification(
        notification_id=pipe04._notification_id(kind, tuple(event_ids)),
        kind=kind,
        recipient=pipe04.FIXED_RECIPIENT,
        envelope=pipe04.FIXED_ENVELOPE,
        body=body,
        event_ids=tuple(event_ids),
        created_at=1000.0,
    )


def authorize(notification):
    events = []

    def lock_request(path, value, timeout):
        return {"ok": True, "state": "LOCKED"}

    def audit_request(path, event):
        events.append(event)
        return {"ok": True}

    boundary = proxy.LockedSignalBoundary(
        pipeline_uid=CALLER["uid"],
        lock_request=lock_request,
        audit_request=audit_request,
    )
    request = {
        "jsonrpc": "2.0", "id": 1, "method": proxy.PIPELINE_METHOD,
        "params": {"notification": notification.as_dict()},
    }
    clean, reason = boundary.authorize(request, CALLER, proxy.Limiter())
    assert reason == ""
    assert clean is not None
    return clean, events


def test_real_pipe04_immediate_notification_crosses_pipe05_boundary():
    ids = [event_id(1)]
    body = (
        "Diary notification\n"
        f"Event: {ids[0]}\n"
        "Urgency: high\n"
        "Summary: Sanitized summary\n"
        "Deadline: 2026-11-02\n"
        "Review: local-review-1"
    )
    value = notification("immediate", ids, body)
    clean, events = authorize(value)
    assert clean["params"] == {"noteToSelf": True, "message": value.body}
    assert clean["params"]["message"] == value.as_dict()["body"]
    assert [event["event_id"] for event in events] == ids


def test_real_pipe04_multi_event_digest_crosses_pipe05_boundary():
    ids = [event_id(1), event_id(2)]
    body = (
        "Diary digest\nLow-priority events: 2\n\n"
        f"Event: {ids[0]}\nSummary: First\nDeadline: -\nReview: first\n\n"
        f"Event: {ids[1]}\nSummary: Second\nDeadline: -\nReview: second"
    )
    value = notification("digest", ids, body)
    clean, events = authorize(value)
    assert clean["params"] == {"noteToSelf": True, "message": value.body}
    assert [event["event_id"] for event in events] == ids
