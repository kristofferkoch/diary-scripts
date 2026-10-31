"""Adversarial and unprivileged integration tests for the trusted audit base."""
from __future__ import annotations

import datetime as dt
import grp
import json
import os
import pwd
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import diary_audit as audit


def event(**changes) -> dict:
    value = {
        "event": "broker.decision",
        "component": "test-broker",
        "operation": "status",
        "decision": "would_deny",
        "lock_state": "locked",
        "destination_ip": "2001:db8::1",
        "destination_port": 443,
        "protocol": "tcp",
    }
    value.update(changes)
    return value


def peer(**changes) -> dict[str, int]:
    value = {"pid": 1234, "uid": 1000, "gid": 1000}
    value.update(changes)
    return value


def service(tmp_path: Path, journal: Path, **kwargs) -> audit.AuditService:
    return audit.AuditService(
        audit.DedupStore(str(tmp_path / "dedup.json"), kwargs.pop("window_us", 60_000_000)),
        audit.JournalWriter(str(journal)),
        **kwargs,
    )


def datagram_sink(path: Path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(2)
    return sock


def fields(payload: bytes) -> dict[str, str]:
    return dict(line.split("=", 1) for line in payload.decode("ascii").splitlines())


def test_redacts_private_fields_before_native_journal_write(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    svc = service(tmp_path, tmp_path / "journal.sock")
    secret = "private body https://example.invalid/search?q=complete+query"
    result = svc.write(event(message=secret, url=secret, query=secret, body=secret), peer())
    payload = sink.recv(65_535)
    record = fields(payload)
    assert result["redacted"] == ["body", "message", "query", "url"]
    assert result["emitted"] is True
    assert secret.encode() not in payload
    assert record["MESSAGE"] == "diary-lock audit broker.decision would_deny"
    assert record["DIARY_AUDIT_REDACTED"] == "body,message,query,url"
    assert record["DIARY_AUDIT_WRITER_UID"] == "1000"
    assert record["DIARY_AUDIT_CALLER_UID"] == "1000"
    assert record["DIARY_AUDIT_DESTINATION_IP"] == "2001:db8::1"
    sink.close()


@pytest.mark.parametrize("field", ["message", "body", "text", "content", "url", "uri", "query", "search", "command", "cmd", "argv", "cwd", "headers", "payload"])
def test_sensitive_fields_are_omitted_not_fingerprinted(tmp_path: Path, field: str) -> None:
    first, _ = audit.normalize_event(event())
    second, redacted = audit.normalize_event(event(**{field: "secret"}))
    assert first == second
    assert redacted == [field]


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "x"},
        {"operation": "read private mail"},
        {"reason": "https://example.invalid/private"},
        {"destination_ip": "https://example.invalid"},
        {"dns_name": "https://example.invalid/search?q=secret"},
        {"destination_port": True},
        {"caller": {"pid": 1, "uid": 2}},
        {"caller": {"pid": 1, "uid": 2, "gid": 3, "asserted_user": "root"}},
    ],
)
def test_unknown_or_smuggled_content_fails_closed(change: dict) -> None:
    with pytest.raises(audit.AuditError):
        audit.normalize_event(event(**change))


def test_dns_idna_is_canonical_and_complete_urls_are_rejected() -> None:
    clean, _ = audit.normalize_event(event(dns_name="Bücher.Example."))
    assert clean["dns_name"] == "xn--bcher-kva.example"
    for value in ("", "bad..name", "-bad.example", "a" * 64 + ".example"):
        with pytest.raises(audit.AuditError):
            audit.normalize_event(event(dns_name=value))


def test_broker_caller_identity_is_resolved_server_side(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    svc = service(tmp_path, tmp_path / "journal.sock")
    caller = {"pid": 456, "uid": 1001, "gid": 1002, "unit": "agent-merge@x.service"}
    with patch.object(audit, "_user", side_effect=lambda uid: {1000: "writer", 1001: "caller"}[uid]):
        svc.write(event(caller=caller), peer())
    record = fields(sink.recv(65_535))
    assert record["DIARY_AUDIT_WRITER_USER"] == "writer"
    assert record["DIARY_AUDIT_CALLER_USER"] == "caller"
    assert record["DIARY_AUDIT_CALLER_PID"] == "456"
    assert record["DIARY_AUDIT_CALLER_UNIT"] == "agent-merge@x.service"
    sink.close()


def test_duplicate_rate_limit_and_cumulative_counts_survive_restart(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    now = [1_000_000_000_000]
    svc = service(tmp_path, tmp_path / "journal.sock", clock=lambda: now[0])
    assert svc.write(event(), peer())["count"] == 1
    first = fields(sink.recv(65_535))
    assert first["DIARY_AUDIT_COUNT"] == "1"
    assert first["DIARY_AUDIT_NEW_COUNT"] == "1"

    now[0] += 1_000_000
    second = svc.write(event(), peer())
    assert second == {"fingerprint": second["fingerprint"], "count": 2, "emitted": False, "ok": True, "redacted": []}
    restarted = service(tmp_path, tmp_path / "journal.sock", clock=lambda: now[0])
    third = restarted.write(event(), peer())
    assert third["count"] == 3 and third["emitted"] is False

    now[0] += 60_000_000
    fourth = restarted.write(event(), peer())
    emitted = fields(sink.recv(65_535))
    assert fourth["count"] == 4 and fourth["new_count"] == 3
    assert emitted["DIARY_AUDIT_COUNT"] == "4"
    assert emitted["DIARY_AUDIT_NEW_COUNT"] == "3"
    assert emitted["DIARY_AUDIT_SUPPRESSED_COUNT"] == "2"
    assert emitted["DIARY_AUDIT_FIRST_SEEN_US"] == first["DIARY_AUDIT_FIRST_SEEN_US"]
    assert int(emitted["DIARY_AUDIT_LAST_SEEN_US"]) == now[0]
    sink.close()


def test_write_failure_does_not_lose_or_mark_counter_emitted(tmp_path: Path) -> None:
    now = [1_000_000_000_000]
    svc = service(tmp_path, tmp_path / "missing-journal.sock", clock=lambda: now[0])
    with pytest.raises(OSError):
        svc.write(event(), peer())
    sink = datagram_sink(tmp_path / "journal.sock")
    restarted = service(tmp_path, tmp_path / "journal.sock", clock=lambda: now[0])
    result = restarted.write(event(), peer())
    record = fields(sink.recv(65_535))
    assert result["count"] == 2 and result["new_count"] == 2
    assert record["DIARY_AUDIT_NEW_COUNT"] == "2"
    sink.close()


def test_distinct_caller_or_destination_is_not_a_duplicate(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    svc = service(tmp_path, tmp_path / "journal.sock")
    svc.write(event(), peer())
    svc.write(event(destination_port=8443), peer())
    first = fields(sink.recv(65_535))
    second = fields(sink.recv(65_535))
    assert first["DIARY_AUDIT_FINGERPRINT"] != second["DIARY_AUDIT_FINGERPRINT"]
    sink.close()


def test_state_is_mode_0600_and_symlink_or_loose_state_fails(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    svc = service(tmp_path, tmp_path / "journal.sock")
    svc.write(event(), peer())
    sink.close()
    state = tmp_path / "dedup.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    saved = state.read_bytes()
    state.unlink()
    state.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(audit.AuditError):
        audit.DedupStore(str(state))
    state.unlink()
    state.write_bytes(saved)
    state.chmod(0o644)
    with pytest.raises(audit.AuditError):
        audit.DedupStore(str(state))


def test_report_sums_deltas_and_never_returns_raw_private_fields() -> None:
    base = {
        "DIARY_AUDIT_SCHEMA_VERSION": "1",
        "DIARY_AUDIT_FINGERPRINT": "a" * 64,
        "DIARY_AUDIT_EVENT": "network.flow",
        "DIARY_AUDIT_COMPONENT": "firewall",
        "DIARY_AUDIT_OPERATION": "connect",
        "DIARY_AUDIT_DECISION": "would_deny",
        "DIARY_AUDIT_LOCK_STATE": "locked",
        "DIARY_AUDIT_DESTINATION_IP": "192.0.2.1",
        "DIARY_AUDIT_CALLER_UID": "1001",
        "MESSAGE": "secret message body and complete search query",
        "URL": "https://example.invalid/private",
    }
    entries = [
        {**base, "DIARY_AUDIT_NEW_COUNT": "1", "DIARY_AUDIT_COUNT": "1", "DIARY_AUDIT_FIRST_SEEN_US": "1000000", "DIARY_AUDIT_LAST_SEEN_US": "1000000", "__REALTIME_TIMESTAMP": "1000000"},
        {**base, "DIARY_AUDIT_NEW_COUNT": "4", "DIARY_AUDIT_COUNT": "5", "DIARY_AUDIT_FIRST_SEEN_US": "1000000", "DIARY_AUDIT_LAST_SEEN_US": "2000000", "__REALTIME_TIMESTAMP": "2000000"},
    ]
    since = dt.datetime.fromtimestamp(0, dt.UTC)
    until = dt.datetime.fromtimestamp(3, dt.UTC)
    baseline = audit.summarize(entries, "baseline", since, until, 10_000)
    review = audit.summarize(entries, "review", since, until, 10_000)
    assert baseline["event_count"] == 5 and baseline["record_count"] == 2
    assert review["groups"][0]["repeated"] is True
    serialized = json.dumps(review)
    assert "secret" not in serialized and "https://" not in serialized and "MESSAGE" not in serialized


def test_review_excludes_routine_allows_and_marks_truncation() -> None:
    entry = {
        "DIARY_AUDIT_SCHEMA_VERSION": "1",
        "DIARY_AUDIT_FINGERPRINT": "b" * 64,
        "DIARY_AUDIT_DECISION": "allow",
        "DIARY_AUDIT_NEW_COUNT": "2",
        "__REALTIME_TIMESTAMP": "1000000",
    }
    now = dt.datetime.fromtimestamp(1, dt.UTC)
    report = audit.summarize([entry], "review", now, now, 1)
    assert report["event_count"] == 0 and report["groups"] == []
    assert report["truncated"] is True


def test_journal_query_uses_fixed_argv_without_shell() -> None:
    captured = {}

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"DIARY_AUDIT_SCHEMA_VERSION":"1"}\n')

    with patch.object(audit.subprocess, "run", side_effect=run):
        result = audit.read_journal("diary-lock", dt.datetime.fromtimestamp(1, dt.UTC), dt.datetime.fromtimestamp(2, dt.UTC), 50)
    assert captured["argv"][:4] == ["/usr/bin/journalctl", "--namespace=diary-lock", "--output=json", "--no-pager"]
    assert captured["argv"][-1] == "DIARY_AUDIT_SCHEMA_VERSION=1"
    assert "shell" not in captured
    assert result == [{"DIARY_AUDIT_SCHEMA_VERSION": "1"}]


@pytest.mark.parametrize("value", ["24h; journalctl --vacuum", "1h --output=cat", "-1h", "9999d", "select * from audit"])
def test_report_time_injection_is_rejected(value: str) -> None:
    with pytest.raises(audit.AuditError):
        audit.parse_time(value)


def test_real_socket_enforces_schema_peer_identity_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    journal_entry = {
        "DIARY_AUDIT_SCHEMA_VERSION": "1",
        "DIARY_AUDIT_FINGERPRINT": "c" * 64,
        "DIARY_AUDIT_DECISION": "would_deny",
        "DIARY_AUDIT_NEW_COUNT": "1",
        "__REALTIME_TIMESTAMP": "1000000",
    }
    svc = service(
        tmp_path,
        tmp_path / "journal.sock",
        journal_reader=lambda *_: [journal_entry],
    )
    group = grp.getgrgid(os.getegid()).gr_name
    server = audit.AuditDaemon(str(tmp_path / "audit.sock"), svc, group)
    monkeypatch.setattr(server, "authorized", lambda _: True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert stat.S_IMODE(os.stat(tmp_path / "audit.sock").st_mode) == 0o660
        reply = audit.request(str(tmp_path / "audit.sock"), {"cmd": "write", "event": event(message="never stored")})
        assert reply["ok"] is True
        record = fields(sink.recv(65_535))
        assert record["DIARY_AUDIT_WRITER_UID"] == str(os.geteuid())
        assert record["DIARY_AUDIT_CALLER_UID"] == str(os.geteuid())
        report = audit.request(str(tmp_path / "audit.sock"), {"cmd": "review", "since": "24h", "until": "now", "limit": 10})
        assert report["event_count"] == 1
        with pytest.raises(audit.AuditError, match="unknown audit request"):
            audit.request(str(tmp_path / "audit.sock"), {"cmd": "raw", "query": "secret"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        sink.close()


def test_authorization_rejects_unlisted_agent_and_pipeline_identities(tmp_path: Path) -> None:
    sink = datagram_sink(tmp_path / "journal.sock")
    current_group = grp.getgrgid(os.getegid()).gr_name
    server = audit.AuditDaemon(str(tmp_path / "audit.sock"), service(tmp_path, tmp_path / "journal.sock"), current_group)
    try:
        group = SimpleNamespace(gr_gid=900, gr_mem=["owner"])
        users = {1000: "owner", 1001: "agent", 1002: "diary-pipeline"}
        with (
            patch.object(audit.grp, "getgrnam", return_value=group),
            patch.object(audit.pwd, "getpwuid", side_effect=lambda uid: SimpleNamespace(pw_name=users[uid])),
        ):
            assert server.authorized(peer(uid=0, gid=0)) is True
            assert server.authorized(peer(uid=1000)) is True
            assert server.authorized(peer(uid=1001)) is False
            assert server.authorized(peer(uid=1002)) is False
            assert server.authorized(peer(uid=1001, gid=900)) is False
            assert server.authorized(peer(uid=1002, gid=900)) is False
    finally:
        server.server_close()
        sink.close()


def test_peer_credentials_come_from_kernel_socketpair() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        credentials = audit.peer_credentials(left)
        assert credentials["pid"] == os.getpid()
        assert credentials["uid"] == os.geteuid()
        assert credentials["gid"] == os.getegid()
    finally:
        left.close()
        right.close()


def test_daemon_cli_refuses_unprivileged_execution(monkeypatch) -> None:
    monkeypatch.setattr(audit.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        audit.daemon_main([])
