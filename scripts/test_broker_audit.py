"""AUD-04 policy, trusted caller, and best-effort audit client tests."""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from scripts import broker_audit
from scripts import diary_audit


CALLER = {"pid": 123, "uid": 1001, "gid": 1001}


@pytest.mark.parametrize(
    ("component", "operation", "expected", "reason"),
    [
        ("signal-notify", "getAttachment", "allow", "read_only"),
        ("signal-notify", "send", "would_deny", "external_send"),
        ("diary-sync", "ping", "allow", "read_only"),
        ("diary-sync", "status", "allow", "read_only"),
        ("diary-sync", "start", "would_deny", "service_start"),
        ("diary-sync", "sync", "would_deny", "external_sync"),
        ("agent-merge", "diary", "would_deny", "publication_request"),
        ("agent-merge", "scripts", "would_deny", "publication_request"),
        ("sudo-broker", "execute", "would_deny", "privileged_execute"),
    ],
)
def test_locked_policy(component: str, operation: str, expected: str, reason: str) -> None:
    event = broker_audit.build_event(component, operation, CALLER)
    assert event == {
        "event": "broker.decision",
        "component": component,
        "operation": operation,
        "decision": expected,
        "lock_state": "locked",
        "reason": reason,
        "caller": CALLER,
    }
    normalized, redacted = diary_audit.normalize_event(event)
    assert normalized == event and redacted == []


@pytest.mark.parametrize(
    ("caller_uid", "owner_uid", "decision", "reason"),
    [
        (1000, 1000, "allow", "owner_approval"),
        (1001, 1000, "would_deny", "owner_required"),
        (1001, None, "would_deny", "owner_required"),
    ],
)
def test_agent_merge_approval_requires_trusted_owner_uid(
    caller_uid: int, owner_uid: int | None, decision: str, reason: str,
) -> None:
    caller = {**CALLER, "uid": caller_uid}
    event = broker_audit.build_event("agent-merge", "approve", caller, owner_uid)
    assert event["operation"] == "approve"
    assert event["decision"] == decision
    assert event["reason"] == reason
    assert event["caller"] == caller
    normalized, redacted = diary_audit.normalize_event(event)
    assert normalized == event and redacted == []


@pytest.mark.parametrize(
    ("component", "operation"),
    [
        ("signal-notify", "receive"),
        ("diary-sync", "exec"),
        ("agent-merge", "force-push"),
        ("sudo-broker", "break-glass"),
        ("future-broker", "anything"),
    ],
)
def test_unknown_and_future_operations_would_deny(component: str, operation: str) -> None:
    event = broker_audit.build_event(component, operation, CALLER)
    assert event["operation"] == "unknown"
    assert event["decision"] == "would_deny"
    assert event["reason"] == "unknown_operation"
    assert operation not in json.dumps(event) or operation == "unknown"


def test_real_audit_socket_receives_parseable_event_and_no_content(tmp_path: Path) -> None:
    path = tmp_path / "audit.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    received: list[dict] = []

    def serve() -> None:
        conn, _ = server.accept()
        data = b""
        while not data.endswith(b"\n"):
            data += conn.recv(65536)
        received.append(json.loads(data))
        conn.sendall(b'{"ok":true}\n')
        conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    event = broker_audit.audit("diary-sync", "status", CALLER, str(path))
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert received == [{"cmd": "write", "event": event}]
    assert received[0]["event"]["caller"] == CALLER


def test_audit_failure_is_single_shot_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def unavailable(path: str, event: dict) -> dict:
        calls.append((path, event))
        raise OSError("unavailable")

    monkeypatch.setattr(broker_audit, "request", unavailable)
    event = broker_audit.audit("sudo-broker", "execute", CALLER)
    assert event["decision"] == "would_deny"
    assert len(calls) == 1


def test_cli_uses_kernel_peer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    events = []
    monkeypatch.setattr(
        broker_audit, "request", lambda path, event: events.append(event) or {"ok": True},
    )
    try:
        assert broker_audit.main([
            "--component", "agent-merge", "--operation", "diary",
            "--peer-fd", str(left.fileno()),
        ]) == 0
    finally:
        left.close()
        right.close()
    assert events[0]["decision"] == "would_deny"
    assert events[0]["caller"] == {
        "pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid(),
    }


def test_cli_nonowner_approval_is_would_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    monkeypatch.setattr(broker_audit, "peer_credentials_fd", lambda fd: CALLER)
    monkeypatch.setattr(broker_audit.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        broker_audit, "request", lambda path, event: events.append(event) or {"ok": True},
    )
    assert broker_audit.main([
        "--component", "agent-merge", "--operation", "approve", "--peer-fd", "0",
    ]) == 0
    assert events[0]["decision"] == "would_deny"
    assert events[0]["reason"] == "owner_required"


def test_peer_credentials_come_from_socketpair() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert broker_audit.peer_credentials(left) == {
            "pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid(),
        }
    finally:
        left.close()
        right.close()
