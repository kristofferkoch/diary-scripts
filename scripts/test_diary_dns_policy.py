"""Fail-closed policy and real-socket contract tests for the AUD-03 DNS proxy."""
from __future__ import annotations

import grp
import json
import os
import socket
import stat
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import diary_audit as audit
from scripts import diary_dns_policy as policy


class FakeLock:
    def __init__(self, state: str = "LOCKED") -> None:
        self.current = state
        self.calls = 0
        self.error: Exception | None = None

    def state(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.current


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.error: Exception | None = None

    def write(self, event: dict) -> None:
        if self.error is not None:
            raise self.error
        normalized, redacted = audit.normalize_event(event)
        assert redacted == []
        self.events.append(normalized)


def service(state: str = "LOCKED") -> tuple[policy.DNSPolicyService, FakeLock, FakeAudit]:
    lock = FakeLock(state)
    events = FakeAudit()
    return policy.DNSPolicyService(lock, events, 1001, 1002), lock, events


@pytest.mark.parametrize("state", ["LOCKING", "LOCKED"])
def test_locked_agent_query_is_refused_and_logged_before_any_upstream(state: str) -> None:
    svc, lock, events = service(state)
    reply = svc.authorize({"dns_name": "Example.TEST.", "protocol": "udp"})
    assert reply.allow is False
    assert reply.rcode == policy.REFUSED
    assert reply.decision == "deny"
    assert reply.lock_state == state.lower()
    assert reply.reason == state.lower()
    assert lock.calls == 1
    assert events.events == [{
        "event": "dns.query",
        "component": "diary-dns-policyd",
        "operation": "query",
        "decision": "deny",
        "lock_state": state.lower(),
        "reason": state.lower(),
        "dns_name": "example.test",
        "protocol": "udp",
        "caller": {"pid": 0, "uid": 1001, "gid": 1002},
    }]


def test_attended_query_is_the_only_allowance() -> None:
    svc, _, events = service("ATTENDED")
    reply = svc.authorize({"dns_name": "example.test", "protocol": "tcp"})
    assert reply.allow is True and reply.rcode == policy.NOERROR
    assert reply.decision == "allow" and reply.lock_state == "attended"
    assert events.events[0]["protocol"] == "tcp"


@pytest.mark.parametrize("error", [policy.DNSPolicyError("bad state"), OSError("missing socket")])
def test_lock_uncertainty_is_refused_and_audited_without_retry(error: Exception) -> None:
    svc, lock, events = service("ATTENDED")
    lock.error = error
    reply = svc.authorize({"dns_name": "example.test", "protocol": "udp"})
    assert reply.allow is False and reply.rcode == policy.REFUSED
    assert reply.decision == "error" and reply.reason == "lock_unavailable"
    assert reply.lock_state == "unknown"
    assert lock.calls == 1
    assert events.events[0]["decision"] == "error"


def test_unknown_controller_state_never_falls_back_to_attended() -> None:
    svc, _, events = service("BROKEN")
    reply = svc.authorize({"dns_name": "example.test", "protocol": "udp"})
    assert reply.allow is False and reply.reason == "lock_unavailable"
    assert events.events[0]["lock_state"] == "unknown"


def test_audit_failure_denies_attended_upstream() -> None:
    svc, _, events = service("ATTENDED")
    events.error = policy.DNSPolicyError("audit unavailable")
    reply = svc.authorize({"dns_name": "example.test", "protocol": "udp"})
    assert reply.allow is False and reply.rcode == policy.REFUSED
    assert reply.decision == "error" and reply.reason == "audit_unavailable"
    assert reply.lock_state == "attended"


def test_locked_denial_remains_closed_when_its_audit_write_fails() -> None:
    svc, _, events = service("LOCKED")
    events.error = OSError("audit unavailable")
    reply = svc.authorize({"dns_name": "example.test", "protocol": "udp"})
    assert reply.allow is False and reply.rcode == policy.REFUSED
    assert reply.decision == "error" and reply.reason == "audit_unavailable"


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"dns_name": "example.test"},
        {"dns_name": "example.test", "protocol": "udp", "uid": 0},
        {"dns_name": "example.test", "protocol": "udp", "identity": "attended"},
        {"dns_name": "bad..name", "protocol": "udp"},
        {"dns_name": "example.test", "protocol": "doh"},
    ],
)
def test_proxy_cannot_select_identity_state_or_alternate_transport(value: object) -> None:
    svc, _, events = service("ATTENDED")
    with pytest.raises(policy.DNSPolicyError):
        svc.authorize(value)
    assert events.events == []


def test_policy_client_treats_every_transport_failure_as_refused(tmp_path: Path) -> None:
    client = policy.PolicyClient(str(tmp_path / "missing.sock"))
    reply = client.authorize("example.test", "udp")
    assert reply == policy.REFUSE_UNAVAILABLE
    assert reply.allow is False and reply.rcode == policy.REFUSED


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": False},
        {"ok": True, "allow": True},
        {"ok": True, "allow": True, "decision": "allow", "lock_state": "locked", "reason": "locked", "rcode": 0},
        {"ok": True, "allow": False, "decision": "allow", "lock_state": "attended", "reason": "attended", "rcode": 5},
        {"ok": True, "allow": False, "decision": "deny", "lock_state": "locked", "reason": "locked", "rcode": 0},
    ],
)
def test_policy_client_rejects_inconsistent_or_malformed_answers(reply: dict) -> None:
    client = policy.PolicyClient()
    with patch.object(policy, "request", return_value=reply):
        assert client.authorize("example.test", "udp") == policy.REFUSE_UNAVAILABLE


def test_real_socket_enforces_fixed_proxy_and_agent_identity(tmp_path: Path) -> None:
    svc, _, events = service("LOCKED")
    server = policy.PolicyDaemon(str(tmp_path / "policy.sock"), svc, os.geteuid(), os.getegid())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert stat.S_IMODE(os.stat(tmp_path / "policy.sock").st_mode) == 0o660
        reply = policy.PolicyClient(str(tmp_path / "policy.sock")).authorize("example.test", "udp")
        assert reply.allow is False and reply.rcode == policy.REFUSED
        assert events.events[-1]["caller"] == {"pid": 0, "uid": 1001, "gid": 1002}

        server.proxy_uid = os.geteuid() + 1
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        client.connect(str(tmp_path / "policy.sock"))
        client.sendall(b'{"dns_name":')
        response = json.loads(client.makefile("rb").readline(4096))
        client.close()
        assert response["ok"] is False and "configured DNS proxy" in response["error"]
        assert len(events.events) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_socket_uses_bounded_json_and_never_forwards_upstream(tmp_path: Path) -> None:
    svc, lock, events = service("LOCKED")
    server = policy.PolicyDaemon(str(tmp_path / "policy.sock"), svc, os.geteuid(), os.getegid())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reply = policy.PolicyClient(str(tmp_path / "policy.sock")).authorize("example.test", "tcp")
        assert reply.as_dict() == {
            "ok": True,
            "allow": False,
            "decision": "deny",
            "lock_state": "locked",
            "reason": "locked",
            "rcode": 5,
        }
        assert lock.calls == 1 and len(events.events) == 1
        assert set(events.events[0]) <= audit.ALLOWED_FIELDS
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_policy_daemon_refuses_unprivileged_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        policy.daemon_main([])


def test_socket_group_comes_from_configured_proxy_identity(tmp_path: Path) -> None:
    svc, _, _ = service()
    group = grp.getgrgid(os.getegid())
    server = policy.PolicyDaemon(str(tmp_path / "policy.sock"), svc, os.geteuid(), group.gr_gid)
    try:
        assert os.stat(tmp_path / "policy.sock").st_gid == group.gr_gid
    finally:
        server.server_close()
