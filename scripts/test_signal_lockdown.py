"""Isolated PIPE-05 identity, failure, rate, execution, and replay tests."""
from __future__ import annotations

import hashlib
import json
import socket

import pytest

from scripts import signal_notify_proxy as proxy

PIPELINE_UID = 4200
GENERAL_UID = 4300
PIPELINE_CALLER = {"pid": 101, "uid": PIPELINE_UID, "gid": PIPELINE_UID}
GENERAL_CALLER = {"pid": 202, "uid": GENERAL_UID, "gid": GENERAL_UID}


def event_id(number=1):
    return f"evt_{number:064x}"


def notification_id(kind, event_ids):
    identity = json.dumps([kind, event_ids], separators=(",", ":")).encode("ascii")
    return "ntf_" + hashlib.sha256(identity).hexdigest()


def notification(number=1, **changes):
    item = event_id(number)
    value = {
        "schema_version": 1,
        "notification_id": notification_id("immediate", [item]),
        "kind": "immediate",
        "recipient": "note-to-self",
        "envelope": "diary-pipeline-v1",
        "body": (
            "Diary notification\n"
            f"Event: {item}\n"
            "Urgency: normal\n"
            "Summary: Sanitized summary\n"
            "Deadline: -\n"
            "Review: local-review"
        ),
        "event_ids": [item],
        "created_at": 1000.0,
    }
    value.update(changes)
    return value


def digest_notification():
    ids = [event_id(1), event_id(2)]
    return {
        "schema_version": 1,
        "notification_id": notification_id("digest", ids),
        "kind": "digest",
        "recipient": "note-to-self",
        "envelope": "diary-pipeline-v1",
        "body": (
            "Diary digest\nLow-priority events: 2\n\n"
            f"Event: {ids[0]}\nSummary: First\nDeadline: -\nReview: first\n\n"
            f"Event: {ids[1]}\nSummary: Second\nDeadline: -\nReview: second"
        ),
        "event_ids": ids,
        "created_at": 1000.0,
    }


def pipeline_request(value=None, **params):
    if not params:
        params = {"notification": notification() if value is None else value}
    return {
        "jsonrpc": "2.0", "id": 1,
        "method": proxy.PIPELINE_METHOD, "params": params,
    }


def general_request(message="general text"):
    return {
        "jsonrpc": "2.0", "id": 1, "method": "send",
        "params": {"noteToSelf": True, "message": message},
    }


def make_boundary(state="LOCKED", audit_request=None, pipeline_uid=PIPELINE_UID):
    states = {"value": state}
    events = []

    def lock_request(path, value, timeout):
        assert value == {"cmd": "status"}
        if states["value"] == "ERROR":
            raise OSError("unavailable")
        return {"ok": True, "state": states["value"]}

    def record(path, value):
        events.append(value)
        return {"ok": True}

    boundary = proxy.LockedSignalBoundary(
        pipeline_uid=pipeline_uid,
        lock_request=lock_request,
        audit_request=audit_request or record,
        notification_limiter=proxy.NotificationLimiter(burst=2),
    )
    return boundary, states, events


def authorize(boundary, request, caller=PIPELINE_CALLER):
    return boundary.authorize(request, caller, proxy.Limiter())


def test_trusted_pipeline_notification_is_forwarded_unchanged_and_audited_while_locked():
    boundary, _, events = make_boundary()
    clean, reason = authorize(boundary, pipeline_request())
    assert reason == ""
    assert clean == {
        "jsonrpc": "2.0", "id": 1, "method": "send",
        "params": {"noteToSelf": True, "message": notification()["body"]},
    }
    assert events[0]["decision"] == "allow"
    assert events[0]["reason"] == "validated_pipeline_notification"
    assert events[0]["caller"] == PIPELINE_CALLER
    assert events[0]["event_id"] == event_id()
    assert "body" not in events[0] and "message" not in events[0]


@pytest.mark.parametrize("state", ["LOCKING", "LOCKED"])
def test_general_agent_arbitrary_send_is_rejected_and_audited(state):
    boundary, _, events = make_boundary(state)
    clean, reason = authorize(boundary, general_request(), GENERAL_CALLER)
    assert clean is None
    assert "rejected while locked" in reason
    assert events[0]["decision"] == "deny"
    assert events[0]["reason"] == "locked_general_agent_send"
    assert events[0]["caller"] == GENERAL_CALLER


def test_attended_general_note_to_self_preserves_existing_behavior():
    boundary, _, events = make_boundary("ATTENDED")
    clean, reason = authorize(boundary, general_request(), GENERAL_CALLER)
    assert reason == ""
    assert clean is not None and clean["params"]["message"] == "general text"
    assert events[0]["reason"] == "attended_note_to_self"


def test_pipeline_identity_comes_from_kernel_peer_not_request_fields():
    boundary, _, events = make_boundary("ATTENDED")
    spoofed = pipeline_request(identity="diary-pipeline", notification=notification())
    clean, reason = authorize(boundary, spoofed, GENERAL_CALLER)
    assert clean is None
    assert "trusted pipeline identity" in reason
    assert events[0]["reason"] == "pipeline_identity_mismatch"


def test_missing_pipeline_identity_denies_even_with_valid_schema(monkeypatch):
    boundary, _, events = make_boundary(pipeline_uid=None)
    monkeypatch.setattr(proxy.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    clean, reason = authorize(boundary, pipeline_request())
    assert clean is None
    assert "identity unavailable" in reason
    assert events[0]["reason"] == "pipeline_identity_unavailable"


@pytest.mark.parametrize(
    "candidate",
    [
        pipeline_request({**notification(), "extra": "field"}),
        pipeline_request(notification(body="https://example.invalid")),
        pipeline_request({
            "event_id": event_id(), "urgency": "normal", "summary": "raw",
            "deadline": None, "review_reference": "local",
        }),
        pipeline_request(message="arbitrary", notification=notification()),
        pipeline_request(notification=notification(), recipient="anyone"),
    ],
)
def test_spoofed_or_arbitrary_pipeline_schema_is_denied(candidate):
    boundary, _, events = make_boundary()
    clean, _ = authorize(boundary, candidate)
    assert clean is None
    assert events[0]["decision"] == "deny"
    assert events[0]["reason"] == "invalid_pipeline_schema"


@pytest.mark.parametrize("state", ["ERROR", "UNKNOWN", "attended"])
def test_lock_uncertainty_denies_trusted_pipeline(state):
    boundary, _, events = make_boundary(state)
    clean, reason = authorize(boundary, pipeline_request())
    assert clean is None
    assert "lock state unavailable" in reason
    assert events[0]["lock_state"] == "unknown"
    assert events[0]["reason"] == "lock_unavailable"


def test_audit_uncertainty_denies_before_authorization_succeeds():
    def unavailable(path, value):
        raise OSError("unavailable")

    boundary, _, _ = make_boundary("ATTENDED", audit_request=unavailable)
    clean, reason = authorize(boundary, pipeline_request())
    assert clean is None
    assert reason == "audit unavailable; send denied"


def test_per_event_and_aggregate_rate_limits():
    now = [0.0]
    limiter = proxy.NotificationLimiter(
        burst=2, period=60.0, event_period=300.0, clock=lambda: now[0],
    )
    assert limiter.allow(("event-a",)) == ""
    assert "event rate" in limiter.allow(("event-a",))
    assert limiter.allow(("event-b",)) == ""
    assert "aggregate rate" in limiter.allow(("event-c",))
    now[0] = 60.0
    assert limiter.allow(("event-c",)) == ""
    now[0] = 300.0
    assert limiter.allow(("event-a",)) == ""


def test_pipeline_rate_rejection_is_audited_with_event_id():
    boundary, _, events = make_boundary()
    clean, _ = authorize(boundary, pipeline_request())
    assert clean is not None
    clean, reason = authorize(boundary, pipeline_request())
    assert clean is None
    assert "event rate limit" in reason
    assert events[-1]["decision"] == "deny"
    assert events[-1]["reason"] == "pipeline_rate_limited"
    assert events[-1]["event_id"] == event_id()


def test_digest_allow_and_rate_limit_audits_include_every_event_id():
    boundary, _, events = make_boundary()
    request = pipeline_request(digest_notification())
    clean, _ = authorize(boundary, request)
    assert clean is not None
    assert [event["event_id"] for event in events] == [event_id(1), event_id(2)]
    clean, reason = authorize(boundary, request)
    assert clean is None and "event rate limit" in reason
    assert [event["event_id"] for event in events[2:]] == [event_id(1), event_id(2)]
    assert all(event["decision"] == "deny" for event in events[2:])
    assert all(event["reason"] == "pipeline_rate_limited" for event in events[2:])


def handle_request(request, boundary, monkeypatch, caller, forwarded):
    monkeypatch.setattr(proxy.broker_audit, "peer_credentials", lambda conn: caller)
    monkeypatch.setattr(
        proxy, "forward_ok",
        lambda upstream, clean: forwarded.append(clean) or (True, ""),
    )
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.sendall(json.dumps(request).encode() + b"\n")
        proxy.handle(server, "unused-upstream", proxy.Limiter(), boundary=boundary)
        response = json.loads(client.recv(65536).split(b"\n", 1)[0])
    finally:
        client.close()
        server.close()
    return response


def test_rejected_send_never_executes_or_replays_after_unlock(monkeypatch):
    boundary, states, events = make_boundary("LOCKED")
    forwarded = []
    response = handle_request(
        general_request(), boundary, monkeypatch, GENERAL_CALLER, forwarded,
    )
    assert "error" in response
    assert forwarded == []
    states["value"] = "ATTENDED"
    assert forwarded == []
    assert len(events) == 1 and events[0]["decision"] == "deny"


def test_schema_and_audit_rejections_never_reach_upstream(monkeypatch):
    forwarded = []
    boundary, _, _ = make_boundary()
    response = handle_request(
        pipeline_request(notification(body="https://example.invalid")),
        boundary, monkeypatch, PIPELINE_CALLER, forwarded,
    )
    assert "error" in response and forwarded == []

    def unavailable(path, value):
        raise OSError("unavailable")

    boundary, _, _ = make_boundary(audit_request=unavailable)
    response = handle_request(
        pipeline_request(), boundary, monkeypatch, PIPELINE_CALLER, forwarded,
    )
    assert "error" in response and forwarded == []


def test_valid_pipeline_execution_occurs_once_after_audit(monkeypatch):
    boundary, _, events = make_boundary()
    forwarded = []
    response = handle_request(
        pipeline_request(), boundary, monkeypatch, PIPELINE_CALLER, forwarded,
    )
    assert "result" in response
    assert len(forwarded) == 1
    assert forwarded[0]["params"]["noteToSelf"] is True
    assert forwarded[0]["params"]["message"] == notification()["body"]
    assert len(events) == 1 and events[0]["decision"] == "allow"


def test_legacy_mode_does_not_query_lock_or_require_audit(monkeypatch):
    forwarded = []
    monkeypatch.setattr(
        proxy.diary_lock, "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("lock queried")),
    )
    monkeypatch.setattr(proxy.broker_audit, "audit", lambda *args: {})
    response = handle_request(
        general_request(), None, monkeypatch, GENERAL_CALLER, forwarded,
    )
    assert "result" in response
    assert len(forwarded) == 1
