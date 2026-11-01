"""PIPE-05 decisions through the real audit client and daemon request path."""
from __future__ import annotations

import grp
import os
import socket
import threading

import pytest

from scripts import broker_audit, diary_audit
from scripts import signal_notify_proxy as proxy
from scripts.test_signal_lockdown import (
    GENERAL_CALLER,
    PIPELINE_CALLER,
    PIPELINE_UID,
    general_request,
    pipeline_request,
)


def journal_fields(payload):
    return dict(line.split("=", 1) for line in payload.decode("ascii").splitlines())


@pytest.fixture
def audit_daemon(tmp_path, monkeypatch):
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(tmp_path / "journal.sock"))
    sink.settimeout(2.0)
    store = diary_audit.DedupStore(str(tmp_path / "dedup.json"), 60_000_000)
    service = diary_audit.AuditService(
        store, diary_audit.JournalWriter(str(tmp_path / "journal.sock")),
    )
    group = grp.getgrgid(os.getegid()).gr_name
    server = diary_audit.AuditDaemon(str(tmp_path / "audit.sock"), service, group)
    monkeypatch.setattr(server, "authorized", lambda peer: True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield str(tmp_path / "audit.sock"), sink
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        sink.close()


def boundary(audit_socket):
    def lock_request(path, value, timeout):
        return {"ok": True, "state": "LOCKED"}

    return proxy.LockedSignalBoundary(
        pipeline_uid=PIPELINE_UID,
        lock_request=lock_request,
        audit_socket=audit_socket,
        audit_request=broker_audit.request,
    )


def test_valid_and_denied_calls_are_accepted_by_real_audit_daemon(audit_daemon):
    audit_socket, sink = audit_daemon
    locked = boundary(audit_socket)
    clean, reason = locked.authorize(
        pipeline_request(), PIPELINE_CALLER, proxy.Limiter(),
    )
    assert clean is not None and reason == ""
    allowed = journal_fields(sink.recv(65_535))
    assert allowed["DIARY_AUDIT_DECISION"] == "allow"
    assert allowed["DIARY_AUDIT_REASON"] == "validated_pipeline_notification"
    assert allowed["DIARY_AUDIT_EVENT_ID"] == pipeline_request()["params"]["notification"]["event_ids"][0]

    clean, reason = locked.authorize(
        general_request(), GENERAL_CALLER, proxy.Limiter(),
    )
    assert clean is None and "rejected while locked" in reason
    denied = journal_fields(sink.recv(65_535))
    assert denied["DIARY_AUDIT_DECISION"] == "deny"
    assert denied["DIARY_AUDIT_REASON"] == "locked_general_agent_send"


def test_real_audit_client_failure_denies_valid_pipeline_send(tmp_path):
    locked = boundary(str(tmp_path / "missing-audit.sock"))
    clean, reason = locked.authorize(
        pipeline_request(), PIPELINE_CALLER, proxy.Limiter(),
    )
    assert clean is None
    assert reason == "audit unavailable; send denied"
