"""Isolated UI, API, authentication, and read-only authorization tests for CTL-09."""
from __future__ import annotations

import datetime as dt
import grp
import http.client
import json
import os
import pwd
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts import diary_audit as audit
from scripts import diary_lock as lock
from scripts import diary_lock_dashboard as dashboard

CONTROLLER_SOCKET = "/scratch/controller.sock"
AUDIT_SOCKET = "/scratch/audit.sock"
NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)


def controller_reply(state: str = "ATTENDED") -> dict[str, Any]:
    attended = state == "ATTENDED"
    return {
        "ok": True,
        "state": state,
        "expires_at": 1_793_553_600.0 if attended else None,
        "remaining_seconds": 900 if attended else None,
        "boot_id": "01234567-0123-0123-0123-012345678901",
    }


def finding(fingerprint: str, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "fingerprint": fingerprint * 64,
        "count": 2,
        "records": 1,
        "first_seen": "2026-11-01T11:00:00+00:00",
        "last_seen": "2026-11-01T11:01:00+00:00",
        "event": "network.flow",
        "component": "network-observer",
        "operation": "connect",
        "decision": "would_deny",
        "lock_state": "locked",
        "destination_ip": "192.0.2.10",
        "destination_port": 443,
        "protocol": "tcp",
        "caller_uid": 1001,
    }
    value.update(changes)
    return value


def report_reply() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "report": "baseline",
        "namespace": "diary-lock",
        "since": "2026-10-31T12:00:00+00:00",
        "until": "2026-11-01T12:00:00+00:00",
        "generated_at": "2026-11-01T12:00:00+00:00",
        "event_count": 8,
        "record_count": 4,
        "duplicate_record_count": 0,
        "truncated": False,
        "groups": [
            finding("a"),
            finding("b", event="dns.query", component="dns-observer", destination_ip=None),
            finding("c", event="broker.decision", component="example-broker", operation="send"),
            finding(
                "d",
                event="controller.break_glass",
                component="diary-lockd",
                operation="attended",
                destination_ip=None,
                destination_port=None,
                protocol=None,
            ),
        ],
    }


def clean_report() -> dict[str, Any]:
    report = report_reply()
    for group in report["groups"]:
        for key, value in list(group.items()):
            if value is None:
                del group[key]
    return report


class FakeSources:
    def __init__(self, controller: Any = None, report: Any = None) -> None:
        self.controller = controller_reply() if controller is None else controller
        self.report = clean_report() if report is None else report
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, value: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, value))
        if path == CONTROLLER_SOCKET:
            if isinstance(self.controller, Exception):
                raise self.controller
            return self.controller
        if path == AUDIT_SOCKET:
            if isinstance(self.report, Exception):
                raise self.report
            return self.report
        raise AssertionError(f"unexpected socket {path}")


def reader(sources: FakeSources) -> dashboard.SnapshotReader:
    return dashboard.SnapshotReader(
        CONTROLLER_SOCKET,
        AUDIT_SOCKET,
        sources,
        clock=lambda: NOW,
    )


def test_snapshot_uses_only_fixed_read_requests_and_groups_findings() -> None:
    sources = FakeSources()
    snapshot = reader(sources).snapshot()
    assert sources.calls == [
        (CONTROLLER_SOCKET, {"cmd": "status"}),
        (AUDIT_SOCKET, {"cmd": "baseline", "since": "24h", "until": "now", "limit": 10_000}),
    ]
    assert snapshot["controller"]["state"] == "ATTENDED"
    assert snapshot["enforcement"] == {
        "mode": "dark",
        "label": "Enforcement: dark / audit-only",
        "active": False,
    }
    assert [len(snapshot["report"]["groups"][kind]) for kind in dashboard.GROUP_KINDS] == [1, 1, 1, 1]
    assert all(component["available"] for component in snapshot["components"])


def test_actual_summarize_output_normalizes_journal_numeric_strings() -> None:
    entry = {
        "DIARY_AUDIT_SCHEMA_VERSION": "1",
        "DIARY_AUDIT_EVENT": "network.flow",
        "DIARY_AUDIT_COMPONENT": "network-observer",
        "DIARY_AUDIT_OPERATION": "connect",
        "DIARY_AUDIT_DECISION": "would_deny",
        "DIARY_AUDIT_LOCK_STATE": "locked",
        "DIARY_AUDIT_DESTINATION_IP": "192.0.2.10",
        "DIARY_AUDIT_DESTINATION_PORT": "443",
        "DIARY_AUDIT_PROTOCOL": "tcp",
        "DIARY_AUDIT_CALLER_UID": "1001",
        "DIARY_AUDIT_FINGERPRINT": "e" * 64,
        "DIARY_AUDIT_EMISSION_ID": "f" * 32,
        "DIARY_AUDIT_NEW_COUNT": "2",
        "DIARY_AUDIT_FIRST_SEEN_US": "1000000",
        "DIARY_AUDIT_LAST_SEEN_US": "2000000",
        "__REALTIME_TIMESTAMP": "2000000",
    }
    summarized = audit.summarize([entry], "baseline", NOW - dt.timedelta(days=1), NOW, 100)
    summarized["ok"] = True
    assert summarized["groups"][0]["destination_port"] == "443"
    assert summarized["groups"][0]["caller_uid"] == "1001"
    snapshot = reader(FakeSources(report=summarized)).snapshot()
    assert snapshot["report"]["available"] is True
    group = snapshot["report"]["groups"]["network"][0]
    assert group["destination_port"] == 443
    assert group["caller_uid"] == 1001


@pytest.mark.parametrize("state", ["LOCKING", "LOCKED", "ATTENDED"])
def test_authoritative_states_and_attendance_are_preserved(state: str) -> None:
    snapshot = reader(FakeSources(controller=controller_reply(state))).snapshot()
    assert snapshot["controller"]["state"] == state
    assert (snapshot["controller"]["expires_at"] is not None) == (state == "ATTENDED")


@pytest.mark.parametrize("source", ["controller", "report"])
def test_unavailable_source_is_visible_and_does_not_erase_the_other(source: str) -> None:
    sources = FakeSources(**{source: dashboard.DashboardError("injected")})
    snapshot = reader(sources).snapshot()
    assert snapshot["controller"]["available"] is (source != "controller")
    assert snapshot["report"]["available"] is (source != "report")
    page = dashboard.render(snapshot)
    assert "UNAVAILABLE" in page if source == "controller" else "ATTENDED" in page
    if source == "report":
        assert page.count("Findings unavailable") == 4
        assert "No findings in this 24-hour report" not in page
    assert "Enforcement: dark / audit-only" in page


@pytest.mark.parametrize(
    "reply",
    [
        {"state": "SAFE"},
        controller_reply("ATTENDED") | {"expires_at": None},
        controller_reply("LOCKED") | {"remaining_seconds": 0},
        controller_reply() | {"boot_id": ""},
    ],
)
def test_malformed_controller_reply_fails_visibly(reply: dict[str, Any]) -> None:
    snapshot = reader(FakeSources(controller=reply)).snapshot()
    assert snapshot["controller"]["available"] is False


def test_malformed_report_and_group_fail_visibly() -> None:
    malformed = clean_report()
    malformed["truncated"] = "no"
    assert reader(FakeSources(report=malformed)).snapshot()["report"]["available"] is False
    malformed = clean_report()
    malformed["groups"][0]["count"] = -1
    assert reader(FakeSources(report=malformed)).snapshot()["report"]["available"] is False
    malformed = clean_report()
    malformed["groups"][0]["destination_port"] = "443x"
    assert reader(FakeSources(report=malformed)).snapshot()["report"]["available"] is False


def test_truncated_report_warns_and_does_not_imply_categories_are_empty() -> None:
    report = clean_report()
    report["groups"] = []
    report["event_count"] = 0
    report["record_count"] = 10_000
    report["truncated"] = True
    page = dashboard.render(reader(FakeSources(report=report)).snapshot())
    assert '<p class="truncation"><strong>Report truncated:</strong>' in page
    assert "Only returned records are shown; omitted records may contain additional findings." in page
    assert page.count("No findings in the returned records.") == 4
    assert page.count("omitted records may contain findings") == 4
    assert "No findings in this 24-hour report" not in page
    assert "10000 returned records" in page


def test_html_escapes_metadata_and_has_no_state_changing_controls() -> None:
    report = clean_report()
    report["groups"][1]["dns_name"] = "<script>alert(1)</script>"
    page = dashboard.render(reader(FakeSources(report=report)).snapshot())
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<form" not in page and "<button" not in page
    assert "Network findings" in page and "DNS findings" in page and "Broker findings" in page
    assert "Enforcement: dark / audit-only" in page


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=5)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.socket_path)


class RunningServer:
    def __init__(
        self,
        tmp_path: Path,
        sources: FakeSources,
        caddy_uid: int | None = None,
    ) -> None:
        self.server = dashboard.DashboardServer(
            str(tmp_path / "dashboard.sock"),
            reader(sources),
            os.geteuid() if caddy_uid is None else caddy_uid,
            os.getegid(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = UnixHTTPConnection(self.server.path)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        connection.close()
        return result

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def web(tmp_path: Path) -> Any:
    sources = FakeSources()
    server = RunningServer(tmp_path, sources)
    try:
        yield server, sources
    finally:
        server.close()


def owner_headers() -> dict[str, str]:
    return {"Remote-User": "kek", "Remote-Groups": "other,lock-owner"}


def test_unix_socket_denies_forged_headers_from_non_caddy_peer(tmp_path: Path) -> None:
    sources = FakeSources()
    server = RunningServer(tmp_path, sources, caddy_uid=os.geteuid() + 1)
    try:
        info = os.stat(server.server.path)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert info.st_gid == os.getegid()
        status, _, _ = server.request("GET", "/lock/api/status", owner_headers())
        assert status == 403
        status, _, _ = server.request("POST", "/lock/api/status", owner_headers())
        assert status == 403
        assert sources.calls == []
    finally:
        server.close()


def test_http_api_and_ui_require_independent_owner_headers(web: Any) -> None:
    server, sources = web
    for headers in (
        {},
        {"Remote-User": "agent", "Remote-Groups": "lock-owner"},
        {"Remote-User": "kek", "Remote-Groups": "other"},
        {"Cookie": "dsh_session=stolen; kimi_token=stolen"},
    ):
        status, _, _ = server.request("GET", "/lock/api/status", headers)
        assert status == 403
    assert sources.calls == []

    status, headers, body = server.request("GET", "/lock/api/status", owner_headers())
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    snapshot = json.loads(body)
    assert snapshot["controller"]["state"] == "ATTENDED"
    assert snapshot["enforcement"]["label"] == "Enforcement: dark / audit-only"

    status, headers, body = server.request("GET", "/lock/", owner_headers())
    assert status == 200 and b"Lockdown status" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "form-action 'none'" in headers["Content-Security-Policy"]


def test_http_is_read_only_and_unknown_authorization_routes_do_not_reach_sources(web: Any) -> None:
    server, sources = web
    for method, path in (
        ("POST", "/lock/"),
        ("POST", "/lock/api/status"),
        ("PUT", "/lock/api/status"),
        ("DELETE", "/lock/"),
        ("GET", "/lock/unlock/consume"),
        ("POST", "/lock/unlock/consume"),
    ):
        before = list(sources.calls)
        status, _, _ = server.request(method, path, owner_headers())
        assert status in {404, 405}
        assert sources.calls == before


def test_head_returns_headers_without_a_body(web: Any) -> None:
    server, _ = web
    status, headers, body = server.request("HEAD", "/lock/api/status", owner_headers())
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


class FakeModes:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def activate(self, mode: str) -> None:
        self.calls.append(mode)


class FakeAudit:
    def emit(
        self,
        event: str,
        operation: str,
        decision: str,
        state: str,
        reason: str,
        caller: dict[str, int] | None = None,
    ) -> None:
        pass

    def ping(self) -> None:
        pass


def dashboard_peer_patch(module: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    current_uid = os.geteuid()
    current_name = pwd.getpwuid(current_uid).pw_name
    group_name = grp.getgrgid(os.getegid()).gr_name
    monkeypatch.setattr(
        module.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="diary-lock-dashboard" if uid == current_uid else current_name),
    )
    return group_name


def test_controller_socket_dashboard_peer_cannot_change_or_extend_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = dashboard_peer_patch(lock, monkeypatch)
    controller = lock.Controller(lock.StateStore(str(tmp_path / "state.json"), "boot-test"), FakeModes(), FakeAudit())
    controller.startup()
    server = lock.LockDaemon(
        str(tmp_path / "lock.sock"),
        controller,
        group,
        os.geteuid() + 1,
        "diary-lock-dashboard",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        before = controller.status()
        assert lock.request(str(tmp_path / "lock.sock"), {"cmd": "status"})["state"] == "LOCKED"
        for value in ({"cmd": "lock"}, {"cmd": "break_glass"}, {"cmd": "status", "uid": 0}):
            with pytest.raises(lock.LockError, match="read-only"):
                lock.request(str(tmp_path / "lock.sock"), value)
        assert controller.status() == before
        assert server.fatal_error is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class FakeReportService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handle(self, value: object, peer: dict[str, int]) -> dict[str, Any]:
        assert isinstance(value, dict)
        value = cast(dict[str, Any], value)
        self.calls.append(value)
        return {"ok": True, "report": value["cmd"], "groups": []}


def test_audit_socket_dashboard_peer_cannot_write_or_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = dashboard_peer_patch(audit, monkeypatch)
    service = FakeReportService()
    server = audit.AuditDaemon(
        str(tmp_path / "audit.sock"),
        cast(audit.AuditService, service),
        group,
        "diary-lock-dashboard",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for command in ("baseline", "review"):
            reply = audit.request(str(tmp_path / "audit.sock"), {"cmd": command})
            assert reply["report"] == command
        for value in (
            {"cmd": "ping"},
            {"cmd": "write", "event": {"event": "audit.selftest"}},
        ):
            with pytest.raises(audit.AuditError, match="read-only"):
                audit.request(str(tmp_path / "audit.sock"), value)
        assert [call["cmd"] for call in service.calls] == ["baseline", "review"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_name_cannot_be_selected_as_dashboard_reader(tmp_path: Path) -> None:
    group = grp.getgrgid(os.getegid()).gr_name
    controller = lock.Controller(lock.StateStore(str(tmp_path / "state.json"), "boot-test"), FakeModes(), FakeAudit())
    server = lock.LockDaemon(
        str(tmp_path / "lock.sock"), controller, group, os.geteuid() + 1, "agent",
    )
    try:
        peer = {"pid": 1, "uid": os.geteuid(), "gid": os.getegid()}
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(lock.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="agent"))
            assert server.read_only(peer) is False
            assert server.authorized(peer) is False
    finally:
        server.server_close()
