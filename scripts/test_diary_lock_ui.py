"""Isolated review, authorization, replay, expiry, and attendance UI tests."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts import diary_fresh_unlock as fresh
from scripts import diary_lock_ui as ui
from scripts.test_diary_fresh_unlock import FakeAutheliaHandler, FakeAutheliaState, begin

SESSION_COOKIE = "diary_lock_authelia_session"
FIRST_GROUP = "a" * 64
SECOND_GROUP = "b" * 64
THIRD_GROUP = "c" * 64
ROUTINE_GROUP = "d" * 64


def group(fingerprint: str, event: str, decision: str, count: int = 1) -> dict[str, Any]:
    value: dict[str, Any] = {
        "fingerprint": fingerprint,
        "event": event,
        "component": "test",
        "operation": "connect" if event.startswith("network") else "request",
        "decision": decision,
        "lock_state": "locked",
        "reason": "audit_only",
        "unit": "test-agent.service",
        "count": count,
        "records": 1,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-01-01T00:00:00+00:00",
    }
    if decision in ui.ATTENTION_DECISIONS:
        value["repeated"] = count > 1
    if event.startswith("network."):
        value.update({"destination_ip": "192.0.2.1", "destination_port": 443, "protocol": "tcp"})
    elif event.startswith("dns."):
        value["dns_name"] = "example.test"
    return value


class FakeController:
    def __init__(self) -> None:
        self.state = "LOCKED"
        self.review_since = 1_700_000_000.0
        self.extensions = 0
        self.unlocks = 0
        self.fail_unlock = False
        self.calls: list[str] = []

    def call(self, command: str) -> dict[str, Any]:
        self.calls.append(command)
        if command == "status":
            return {
                "ok": True,
                "state": self.state,
                "remaining_seconds": 900 if self.state == "ATTENDED" else None,
                "expires_at": None,
                "review_since": self.review_since,
                "boot_id": "test-boot",
            }
        if command == "lock":
            self.state = "LOCKED"
        elif command == "extend":
            if self.state != "ATTENDED":
                raise ui.ServiceUnavailable("attendance is not active")
            self.extensions += 1
        elif command == "unlock":
            if self.fail_unlock:
                raise ui.ServiceUnavailable("injected controller failure")
            self.state = "ATTENDED"
            self.unlocks += 1
        return self.call("status")


class FakeReports:
    def __init__(self) -> None:
        self.fingerprints = [FIRST_GROUP, SECOND_GROUP]
        self.incomplete = False

    def review(self) -> dict[str, Any]:
        review_id = hashlib.sha256(
            json.dumps(self.fingerprints, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return {
            "review_id": review_id,
            "since": "2026-01-01T00:00:00+00:00",
            "generated_at": "2026-01-01T00:01:00+00:00",
            "incomplete": self.incomplete,
            "required_groups": [
                {**group(self.fingerprints[0], "network.flow", "deny"), "category": "network", "attention": "novel"},
                {**group(self.fingerprints[1], "broker.decision", "would_deny", 2), "category": "broker", "attention": "suspicious"},
                *([
                    {**group(THIRD_GROUP, "dns.query", "deny"), "category": "dns", "attention": "novel"}
                ] if len(self.fingerprints) == 3 else []),
            ],
            "routine_groups": [
                {**group(ROUTINE_GROUP, "dns.query", "observed"), "category": "dns"},
            ],
        }


@dataclass
class Services:
    auth_state: FakeAutheliaState
    auth_server: ThreadingHTTPServer
    fresh_server: fresh.FreshUnlockServer
    ui_server: ui.OwnerUIServer
    controller: FakeController
    reports: FakeReports
    now: list[float]


@pytest.fixture
def services(tmp_path: Path) -> Iterator[Services]:
    auth_state = FakeAutheliaState()
    handler = type("BoundFakeAutheliaHandler", (FakeAutheliaHandler,), {"state": auth_state})
    auth_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    config = fresh.Config(
        owner_username="owner",
        public_origin="https://example.test",
        authelia_backend=f"http://localhost:{auth_server.server_port}",
        session_cookie=SESSION_COOKIE,
        transaction_ttl=300,
    )
    fresh_server = fresh.FreshUnlockServer(("127.0.0.1", 0), config, tmp_path / "transactions.sqlite3")
    ui_config = ui.Config(
        owner_username="owner",
        public_origin="https://example.test",
        authelia_backend=config.authelia_backend,
        fresh_backend="http" + f"://127.0.0.1:{fresh_server.server_port}",
        session_cookie=SESSION_COOKIE,
    )
    controller = FakeController()
    reports = FakeReports()
    now = [float(int(time.time()))]
    fresh_server.transactions.clock = lambda: now[0]
    ui_server = ui.OwnerUIServer(
        ("127.0.0.1", 0), ui_config, controller, reports, acknowledgement_clock=lambda: now[0],
    )
    servers = [auth_server, fresh_server, ui_server]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    yield Services(auth_state, auth_server, fresh_server, ui_server, controller, reports, now)
    for server in reversed(servers):
        server.shutdown()
        server.server_close()
    for thread in reversed(threads):
        thread.join(timeout=5)


def request(
    server: ThreadingHTTPServer, method: str, path: str,
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    all_headers = dict(headers or {})
    if cookies:
        all_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode("ascii")
        all_headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
    connection.request(method, path, headers=all_headers, body=encoded)
    response = connection.getresponse()
    raw = response.read()
    is_json = response.getheader("Content-Type", "").startswith("application/json")
    result = response.status, json.loads(raw) if raw and is_json else raw
    connection.close()
    return result


def action(name: str) -> dict[str, str]:
    return {"Origin": "https://example.test", "Sec-Fetch-Site": "same-origin", name: "yes"}


def authorize(services: Services, session: str = "new") -> dict[str, str]:
    services.auth_state.add("old")
    transaction, _ = begin(services.fresh_server, {SESSION_COOKIE: "old"})
    services.auth_state.add_fresh(session)
    cookies = {SESSION_COOKIE: session, fresh.TRANSACTION_COOKIE: transaction}
    status, _ = request(services.fresh_server, "GET", "/lock/unlock/return", cookies)
    assert status == 200
    return cookies


def acknowledge(services: Services, cookies: dict[str, str], groups: list[str]) -> tuple[int, dict[str, Any]]:
    report = services.reports.review()
    return request(
        services.ui_server, "POST", "/lock/unlock/acknowledge", cookies,
        action("X-Diary-Acknowledge-Review"),
        {"review_id": report["review_id"], "groups": groups},
    )


def confirm(services: Services, cookies: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return request(
        services.ui_server, "POST", "/lock/unlock/confirm", cookies,
        action("X-Diary-Unlock-Confirm"),
    )


def test_full_flow_requires_group_acknowledgement_and_rejects_replay(services: Services) -> None:
    cookies = authorize(services)
    status, _ = confirm(services, cookies)
    assert status == 409
    assert services.controller.unlocks == 0

    assert acknowledge(services, cookies, [FIRST_GROUP])[0] == 200
    assert confirm(services, cookies)[0] == 409
    assert acknowledge(services, cookies, [SECOND_GROUP])[0] == 200
    status, result = confirm(services, cookies)
    assert status == 200 and result["state"] == "ATTENDED"
    assert services.controller.unlocks == 1

    services.controller.state = "LOCKED"
    status, _ = confirm(services, cookies)
    assert status == 403
    assert services.controller.unlocks == 1


def test_stolen_agent_session_and_spoofed_headers_have_no_authority(services: Services) -> None:
    for cookies in (None, {"dsh_session": "stolen"}, {"kimi_token": "stolen"}):
        status, _ = request(
            services.ui_server, "GET", "/lock/workflow.js", cookies,
            {"Remote-User": "owner", "Remote-Groups": "lock-owner"},
        )
        assert status == 403
    services.auth_state.add("agent-session", username="agent", groups="agent")
    status, _ = request(
        services.ui_server, "GET", "/lock/workflow.js", {SESSION_COOKIE: "agent-session"},
    )
    assert status == 403
    assert services.controller.calls == []


def test_pending_stale_and_substituted_sessions_cannot_unlock(services: Services) -> None:
    services.auth_state.add("old")
    transaction, _ = begin(services.fresh_server, {SESSION_COOKIE: "old"})
    pending = {SESSION_COOKIE: "old", fresh.TRANSACTION_COOKIE: transaction}
    assert confirm(services, pending)[0] == 403

    services.auth_state.add_fresh("new")
    services.auth_state.add_fresh("alternate")
    cookies = {SESSION_COOKIE: "new", fresh.TRANSACTION_COOKIE: transaction}
    assert request(services.fresh_server, "GET", "/lock/unlock/return", cookies)[0] == 200
    assert acknowledge(services, cookies, [FIRST_GROUP, SECOND_GROUP])[0] == 200
    substituted = {SESSION_COOKIE: "alternate", fresh.TRANSACTION_COOKIE: transaction}
    assert confirm(services, substituted)[0] == 403
    assert services.controller.unlocks == 0


def test_expired_transaction_cannot_unlock_even_after_acknowledgement(services: Services) -> None:
    cookies = authorize(services)
    assert acknowledge(services, cookies, [FIRST_GROUP, SECOND_GROUP])[0] == 200
    acknowledgement_time = services.now[0]
    services.ui_server.acknowledgements.clock = lambda: acknowledgement_time
    services.now[0] += 301
    assert confirm(services, cookies)[0] == 403
    assert services.controller.unlocks == 0


def test_failed_unlock_stays_locked_and_consumed_proof_cannot_be_retried(services: Services) -> None:
    cookies = authorize(services)
    assert acknowledge(services, cookies, [FIRST_GROUP, SECOND_GROUP])[0] == 200
    services.controller.fail_unlock = True
    status, result = confirm(services, cookies)
    assert status == 502
    assert "restrictions remain" in result["error"]
    assert services.controller.state == "LOCKED"
    services.controller.fail_unlock = False
    assert confirm(services, cookies)[0] == 403
    assert services.controller.state == "LOCKED"
    assert services.controller.unlocks == 0


def test_new_or_truncated_report_invalidates_acknowledgement(services: Services) -> None:
    cookies = authorize(services)
    assert acknowledge(services, cookies, [FIRST_GROUP, SECOND_GROUP])[0] == 200
    services.reports.fingerprints.append(THIRD_GROUP)
    assert confirm(services, cookies)[0] == 409
    assert acknowledge(services, cookies, [FIRST_GROUP, SECOND_GROUP, THIRD_GROUP])[0] == 200
    services.reports.incomplete = True
    assert acknowledge(services, cookies, [THIRD_GROUP])[0] == 409
    assert confirm(services, cookies)[0] == 409
    assert services.controller.unlocks == 0


def test_only_explicit_owner_ui_post_extends_attendance(services: Services) -> None:
    services.controller.state = "ATTENDED"
    services.auth_state.add("owner")
    cookies = {SESSION_COOKIE: "owner"}
    for _ in range(5):
        assert request(services.ui_server, "GET", "/lock/workflow.js", cookies)[0] == 200
    assert services.controller.extensions == 0

    assert request(services.ui_server, "POST", "/lock/api/extend", cookies)[0] == 403
    assert request(
        services.ui_server, "POST", "/lock/api/extend", cookies,
        {"Origin": "https://example.invalid", "X-Diary-Extend-Attendance": "yes"},
    )[0] == 403
    assert services.controller.extensions == 0
    assert request(
        services.ui_server, "POST", "/lock/api/extend", cookies,
        action("X-Diary-Extend-Attendance"),
    )[0] == 200
    assert services.controller.extensions == 1

    assert request(
        services.ui_server, "POST", "/lock/api/lock", cookies, action("X-Diary-Lock-Now"),
    )[0] == 200
    assert services.controller.state == "LOCKED"


def test_acknowledgement_store_expires_and_bounds_records() -> None:
    now = [100.0]
    store = ui.AcknowledgementStore(lambda: now[0])
    transaction = "x" * 32
    store.add(transaction, "a" * 64, {FIRST_GROUP})
    assert store.complete(transaction, "a" * 64, {FIRST_GROUP})
    now[0] += 301
    assert not store.complete(transaction, "a" * 64, {FIRST_GROUP})


def test_audit_provider_requires_attention_groups_and_fails_on_truncation() -> None:
    controller = FakeController()
    review = {
        "ok": True,
        "report": "review",
        "truncated": False,
        "groups": [group(FIRST_GROUP, "network.flow", "deny")],
    }
    with patch("scripts.diary_lock_ui._unix_request", return_value=review) as request_report:
        result = ui.AuditReportProvider(controller, "/unused", reader=request_report).review()
    assert [item["fingerprint"] for item in result["required_groups"]] == [FIRST_GROUP]
    assert result["required_groups"][0]["attention"] == "novel"
    assert "routine_groups" not in result
    assert result["incomplete"] is False
    assert request_report.call_args.args[1]["cmd"] == "review"

    truncated = {**review, "truncated": True}
    with patch("scripts.diary_lock_ui._unix_request", return_value=truncated) as request_report:
        result = ui.AuditReportProvider(controller, "/unused", reader=request_report).review()
    assert result["incomplete"] is True


def test_owner_classified_routine_groups_do_not_block_but_novel_cannot_be_hidden() -> None:
    controller = FakeController()
    review = {
        "ok": True,
        "report": "review",
        "truncated": False,
        "groups": [group(FIRST_GROUP, "network.flow", "deny")],
    }
    expected_destination = {
        "schema_version": 1,
        "expected_destinations": [{
            "match": {
                "event": "network.flow", "destination_ip": "192.0.2.1",
                "reason": "audit_only", "unit": "test-agent.service",
            },
            "reference": "owner-reviewed-destination",
        }],
        "routine_expected_failures": [],
    }
    with patch("scripts.diary_lock_ui._unix_request", return_value=review) as request_report:
        result = ui.AuditReportProvider(controller, "/unused", expected_destination, request_report).review()
    assert result["required_groups"] == []

    routine_rule_only = {
        "schema_version": 1,
        "expected_destinations": [],
        "routine_expected_failures": [{
            "match": {"event": "network.flow", "destination_ip": "192.0.2.1"},
            "reference": "owner-reviewed-routine-rule",
        }],
    }
    with patch("scripts.diary_lock_ui._unix_request", return_value=review) as request_report:
        result = ui.AuditReportProvider(controller, "/unused", routine_rule_only, request_report).review()
    assert [item["fingerprint"] for item in result["required_groups"]] == [FIRST_GROUP]
    assert result["required_groups"][0]["reason"] == "audit_only"
    assert result["required_groups"][0]["unit"] == "test-agent.service"


def test_workflow_asset_does_not_duplicate_dashboard_or_use_websockets(services: Services) -> None:
    services.auth_state.add("owner")
    cookies = {SESSION_COOKIE: "owner"}
    assert request(services.ui_server, "GET", "/lock/workflow.js", cookies)[0] == 200
    for path in ("/lock/", "/lock/api/status", "/lock/api/report"):
        assert request(services.ui_server, "GET", path, cookies)[0] == 404
    assert "https://" not in ui.WORKFLOW_JS
    assert "WebSocket" not in ui.WORKFLOW_JS
    assert "setInterval" not in ui.WORKFLOW_JS
    assert "innerHTML" not in ui.WORKFLOW_JS
    assert "DiaryLockWorkflow" in ui.WORKFLOW_JS
