"""Isolated tests for the merge/deny console host and its fixed-argv executor."""
from __future__ import annotations

import http.client
import json
import os
import socket
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import agent_merge_console as console
from scripts import diary_fresh_unlock as fresh
from scripts import diary_lock_console_ui as ui
from scripts import diary_merge_console_service as executor
from scripts.test_diary_fresh_unlock import FakeAutheliaHandler, FakeAutheliaState

SESSION_COOKIE = "diary_lock_authelia_session"
QUEUE_OID = "a" * 40
BASE_OID = "b" * 40
OTHER_OID = "c" * 40


# --------------------------------------------------------------------------- fakes


class FakeExecutor:
    """Stand-in for the kek-run executor; records requests, returns canned replies."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.snapshot: dict[str, Any] = {
            "pending": True,
            "repo": "scripts",
            "queue_oid": QUEUE_OID,
            "base_oid": BASE_OID,
            "commits": [{"oid": QUEUE_OID, "subject": "queued change"}],
            "diff": "+++ b/file\n+<script>alert(1)</script>",
            "advisory": {"status": "missing", "text": "none"},
        }
        self.reply: ui.ExecutorReply = ui.ExecutorReply(True, "", None, "merged")

    def request(self, value: dict[str, Any]) -> ui.ExecutorReply:
        self.requests.append(value)
        if value["cmd"] == "snapshot":
            snapshot = dict(self.snapshot)
            snapshot["repo"] = value.get("repo", "scripts")
            return ui.ExecutorReply(True, "", snapshot, "")
        return self.reply


@dataclass
class Services:
    auth_state: FakeAutheliaState
    fresh_server: fresh.FreshUnlockServer
    ui_server: ui.ConsoleServer
    executor: FakeExecutor
    now: list[float] = field(default_factory=lambda: [float(int(time.time()))])


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
    fake = FakeExecutor()
    ui_config = ui.Config(
        owner_username="owner",
        public_origin="https://example.test",
        authelia_backend=config.authelia_backend,
        fresh_backend="http" + f"://127.0.0.1:{fresh_server.server_port}",
        session_cookie=SESSION_COOKIE,
    )
    ui_server = ui.ConsoleServer(
        ("127.0.0.1", 0), ui_config, fake,  # type: ignore[arg-type]
        ui.FreshGrantClient(ui_config.public_origin, ui_config.fresh_backend, SESSION_COOKIE),
    )
    servers = [auth_server, fresh_server, ui_server]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    yield Services(auth_state, fresh_server, ui_server, fake)
    for server in reversed(servers):
        server.shutdown()
        server.server_close()
    for thread in reversed(threads):
        thread.join(timeout=5)


def http_request(
    server: Any, method: str, path: str,
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
) -> tuple[int, bytes]:
    all_headers = dict(headers or {})
    if cookies:
        all_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    if isinstance(body, str):
        body = body.encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
    connection.request(method, path, headers=all_headers, body=body)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, raw


def browser_headers(action: str | None = None) -> dict[str, str]:
    headers = {"Origin": "https://example.test", "Sec-Fetch-Site": "same-origin"}
    if action:
        headers[action] = "yes"
    return headers


def authorized(services: Services, action: str = "merge", repo: str = "scripts") -> dict[str, str]:
    """Complete a fresh owner ceremony for one merge/deny decision."""
    services.auth_state.add("old")
    body = json.dumps(
        {"action": action, "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "repo": repo}
    )
    connection = http.client.HTTPConnection("127.0.0.1", services.fresh_server.server_port, timeout=6)
    connection.request(
        "POST", "/lock/merge/begin",
        headers={
            **browser_headers("X-Diary-Merge-Begin"), "Content-Type": "application/json",
            "Cookie": f"{SESSION_COOKIE}=old", "Content-Length": str(len(body)),
        },
        body=body.encode(),
    )
    response = connection.getresponse()
    assert response.status == 303
    cookie = None
    for name, value in response.getheaders():
        if name.lower() == "set-cookie" and value.startswith("diary_lock_fresh_unlock="):
            cookie = value.split(";", 1)[0].split("=", 1)[1]
    response.read()
    connection.close()
    assert cookie is not None
    services.auth_state.add_fresh("new")
    cookies = {SESSION_COOKIE: "new", fresh.TRANSACTION_COOKIE: cookie}
    status, _ = http_request(services.fresh_server, "GET", "/lock/merge/return", cookies=cookies)
    assert status == 200
    return cookies


def page_token(services: Services, cookies: dict[str, str], repo: str = "scripts") -> str:
    status, raw = http_request(
        services.ui_server, "GET", f"/lock/merge/?repo={repo}", cookies=cookies,
    )
    assert status == 200
    match = re.search(rb'name="csrf" value="([^"]+)"', raw)
    assert match is not None
    return match.group(1).decode()


def decide(
    services: Services, cookies: dict[str, str], token: str,
    action: str = "merge", origin: str = "https://example.test",
    queue_oid: str = QUEUE_OID, repo: str = "scripts",
) -> tuple[int, bytes]:
    body = urllib.parse.urlencode(
        {
            "repo": repo, "action": action, "queue_oid": queue_oid,
            "base_oid": BASE_OID, "csrf": token, "confirm": action,
        }
    )
    headers = browser_headers()
    headers["Origin"] = origin
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    return http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies, headers=headers, body=body,
    )


# -------------------------------------------------------------------------- routes


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/lock/workflow.js"),
        ("GET", "/lock/api/status"),
        ("POST", "/lock/api/lock"),
        ("POST", "/lock/api/extend"),
        ("POST", "/lock/unlock/begin"),
        ("GET", "/lock/unlock/return"),
        ("POST", "/lock/unlock/consume"),
        ("POST", "/lock/unlock/acknowledge"),
        ("POST", "/lock/unlock/confirm"),
    ],
)
def test_console_registers_no_unlock_acknowledge_attendance_or_dashboard_routes(
    services: Services, method: str, path: str,
) -> None:
    status, raw = http_request(
        services.ui_server, method, path,
        cookies={SESSION_COOKIE: "new"}, headers=browser_headers("X-Diary-Lock-Now"),
    )
    assert status == 404, (method, path, raw)
    assert b'"not found"' in raw


def test_page_renders_only_the_console_and_escapes_untrusted_diff(services: Services) -> None:
    cookies = authorized(services)
    status, raw = http_request(services.ui_server, "GET", "/lock/merge/", cookies=cookies)
    body = raw.decode()
    assert status == 200
    assert QUEUE_OID in body and BASE_OID in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)</script>" not in body
    assert body.count('name="csrf"') == 2


def test_empty_queue_shows_a_page_without_decision_forms(services: Services) -> None:
    services.executor.snapshot = {"pending": False}
    cookies = authorized(services)
    status, raw = http_request(services.ui_server, "GET", "/lock/merge/", cookies=cookies)
    assert status == 200
    assert b"Both queues are empty" in raw
    assert b'name="action"' not in raw


# --------------------------------------------------------------------- CSRF/origin


def test_decision_requires_same_origin_explicit_form_and_current_token(services: Services) -> None:
    cookies = authorized(services)
    token = page_token(services, cookies)
    calls = len(services.executor.requests)

    status, _ = decide(services, cookies, token, origin="https://evil.example")
    assert status == 403

    # A stale token is a stale form, not an attack: the console re-renders the
    # same reviewed pair with a fresh token and executes nothing.
    status, page = decide(services, cookies, "not-the-token")
    assert status == 200
    assert b"expired before it was submitted" in page

    # A token issued to one owner session must not authorize another.
    other = dict(cookies)
    services.auth_state.add_fresh("other")
    other[SESSION_COOKIE] = "other"
    status, page = decide(services, other, token)
    assert status == 200
    assert b"expired before it was submitted" in page

    assert all(request["cmd"] == "snapshot" for request in services.executor.requests[calls:])


def test_decision_requires_a_fresh_merge_transaction(services: Services) -> None:
    cookies = authorized(services)
    token = page_token(services, cookies)
    status, raw = decide(services, {SESSION_COOKIE: cookies[SESSION_COOKIE]}, token)
    assert status == 200
    assert b'action="/lock/merge/begin"' in raw
    assert all(request["cmd"] == "snapshot" for request in services.executor.requests)


# ----------------------------------------------------------------- grant/replay


def test_merge_consumes_the_grant_and_calls_the_executor_with_the_reviewed_pair(services: Services) -> None:
    cookies = authorized(services)
    token = page_token(services, cookies)
    status, raw = decide(services, cookies, token)
    assert status == 200
    assert b"Merged" in raw
    decision = services.executor.requests[-1]
    assert decision["cmd"] == "decide"
    assert decision["action"] == "merge"
    assert decision["queue_oid"] == QUEUE_OID
    assert decision["base_oid"] == BASE_OID
    assert decision["transaction"] == cookies[fresh.TRANSACTION_COOKIE].split(".")[0]


def test_a_consumed_grant_cannot_be_replayed(services: Services) -> None:
    cookies = authorized(services)
    token = page_token(services, cookies)
    first_status, _ = decide(services, cookies, token)
    assert first_status == 200
    decisions = [request for request in services.executor.requests if request["cmd"] == "decide"]
    assert len(decisions) == 1

    second_token = page_token(services, cookies)
    second_status, second_raw = decide(services, cookies, second_token)
    assert second_status == 200
    assert b'action="/lock/merge/begin"' in second_raw
    assert len([request for request in services.executor.requests if request["cmd"] == "decide"]) == 1


def test_deny_is_reported_as_a_denial_without_publication(services: Services) -> None:
    cookies = authorized(services, action="deny")
    services.executor.reply = ui.ExecutorReply(True, "", None, "denied")
    token = page_token(services, cookies)
    status, raw = decide(services, cookies, token, action="deny")
    assert status == 200
    assert b"Denied" in raw
    assert b"Nothing was merged" in raw
    assert services.executor.requests[-1]["action"] == "deny"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("queue_changed", 409), ("base_changed", 409), ("unauthorized", 403),
     ("unavailable", 503), ("merge_failed", 502)],
)
def test_executor_refusals_and_failures_map_to_explicit_statuses(
    services: Services, code: str, expected: int,
) -> None:
    cookies = authorized(services)
    services.executor.reply = ui.ExecutorReply(False, code, None, "")
    token = page_token(services, cookies)
    status, raw = decide(services, cookies, token)
    assert status == expected
    if code in {"queue_changed", "base_changed"}:
        assert b"changed after you reviewed" in raw
    if code == "merge_failed":
        assert b"Unknown outcome" in raw
    if code == "unavailable":
        assert b"nothing was merged" in raw


# ------------------------------------------------------------------- executor unit


def _paths_by_repo(tmp_path: Path) -> dict[str, Any]:
    return {"scripts": _console_paths(tmp_path)}


def _console_paths(tmp_path: Path):
    return executor.queue.QueuePaths(
        integration=tmp_path / "integration",
        approved=tmp_path / "approved",
        queue=tmp_path / "queue",
        worktrees=tmp_path / "worktrees",
        lock=tmp_path / "queue.lock",
    )


class FakeVerifier:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str, str, str, str]] = []

    def verify(
        self, repo: str, action: str, queue_oid: str, base_oid: str, transaction: str,
    ) -> bool:
        self.calls.append((repo, action, queue_oid, base_oid, transaction))
        return self.allow


def test_executor_forwards_the_exact_binding_and_requires_a_consumed_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = FakeVerifier(allow=False)
    recorded: list[Any] = []

    def fake_console(paths, verifier_arg, authorizer, *, auditor, publisher, lock_state, repo="scripts"):
        recorded.append(authorizer)

        class FakeConsole:
            def snapshot(self):
                return None

            def decide(self, action, queue_oid, base_oid):
                authorizer(action, queue_oid, base_oid)
                return console.DecisionResult(action, queue_oid, base_oid, "merged")

        return FakeConsole()

    monkeypatch.setattr(executor, "_console", fake_console)
    request = {
        "cmd": "decide", "repo": "scripts", "action": "merge", "queue_oid": QUEUE_OID,
        "base_oid": BASE_OID, "transaction": "tx-1",
    }
    with pytest.raises(console.OwnerAuthorizationRequired):
        executor.handle_request(request, _paths_by_repo(tmp_path), verifier, auditor=object())  # type: ignore[arg-type]
    assert verifier.calls == [("scripts", "merge", QUEUE_OID, BASE_OID, "tx-1")]

    verifier.allow = True
    reply = executor.handle_request(request, _paths_by_repo(tmp_path), verifier, auditor=object())  # type: ignore[arg-type]
    assert reply == {"ok": True, "action": "merge", "outcome": "merged"}


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": "shell", "argv": ["sh"]},
        {"cmd": "decide", "repo": "scripts", "action": "merge", "queue_oid": QUEUE_OID, "base_oid": BASE_OID},
        {"cmd": "decide", "repo": "scripts", "action": "push", "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "transaction": "t"},
        {"cmd": "decide", "repo": "scripts", "action": "merge", "queue_oid": "abc", "base_oid": BASE_OID, "transaction": "t"},
        {"cmd": "decide", "repo": "scripts", "action": "merge", "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "transaction": "t", "extra": 1},
        {"cmd": "snapshot", "repo": "scripts", "queue_oid": OTHER_OID},
    ],
)
def test_executor_rejects_anything_outside_its_two_fixed_verbs(
    tmp_path: Path, payload: dict[str, Any],
) -> None:
    with pytest.raises(executor.ExecutorError):
        executor.handle_request(payload, _paths_by_repo(tmp_path), FakeVerifier(), auditor=object())  # type: ignore[arg-type]


def test_executor_snapshot_holds_no_decision_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fake_console(paths, verifier, authorizer, *, auditor, publisher, lock_state, repo="scripts"):
        seen.append(authorizer)

        class FakeConsole:
            def snapshot(self):
                return None

        return FakeConsole()

    monkeypatch.setattr(executor, "_console", fake_console)
    reply = executor.handle_request({"cmd": "snapshot", "repo": "scripts"}, _paths_by_repo(tmp_path), FakeVerifier(), auditor=object())  # type: ignore[arg-type]
    assert reply == {"ok": True, "snapshot": {"pending": False}}
    with pytest.raises(console.OwnerAuthorizationRequired):
        seen[0]("merge", QUEUE_OID, BASE_OID)


def test_executor_maps_audit_failure_to_no_repository_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAudit:
        def record(self, record):
            raise console.ConsoleError("audit unavailable")

    def fake_console(paths, verifier, authorizer, *, auditor, publisher, lock_state, repo="scripts"):
        class FakeQueueConsole:
            def snapshot(self):
                return None

            def decide(self, action, queue_oid, base_oid):
                auditor.record(object())
                raise AssertionError("must not decide when the pre-merge audit fails")

        return FakeQueueConsole()

    monkeypatch.setattr(executor, "_console", fake_console)
    request = {"cmd": "decide", "repo": "scripts", "action": "deny", "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "transaction": "t"}
    with pytest.raises(console.ConsoleError):
        executor.handle_request(request, _paths_by_repo(tmp_path), FakeVerifier(), auditor=FailingAudit())


# --------------------------------------------------------------- fresh service binding


def test_merge_grant_is_bound_to_action_and_oids_and_cannot_be_used_as_unlock(services: Services) -> None:
    cookies = authorized(services)
    transaction_id = cookies[fresh.TRANSACTION_COOKIE].split(".")[0]
    store = services.fresh_server.transactions

    assert store.verify_consumed(transaction_id, "scripts", "merge", QUEUE_OID, BASE_OID) is None
    assert store.verify_consumed(transaction_id, "scripts", "merge", QUEUE_OID, OTHER_OID) is None
    assert store.verify_consumed(transaction_id, "scripts", "deny", QUEUE_OID, BASE_OID) is None

    token = page_token(services, cookies)
    assert decide(services, cookies, token)[0] == 200
    assert store.verify_consumed(transaction_id, "scripts", "merge", QUEUE_OID, BASE_OID) is not None
    assert store.verify_consumed(transaction_id, "scripts", "deny", QUEUE_OID, BASE_OID) is None


def test_begin_rejects_an_invalid_action_or_incomplete_oids(services: Services) -> None:
    services.auth_state.add("old")
    for body in (
        {"repo": "scripts", "action": "push", "queue_oid": QUEUE_OID, "base_oid": BASE_OID},
        {"repo": "scripts", "action": "merge", "queue_oid": "abc", "base_oid": BASE_OID},
        {"action": "merge", "queue_oid": QUEUE_OID},
    ):
        status, _ = http_request(
            services.fresh_server, "POST", "/lock/merge/begin",
            cookies={SESSION_COOKIE: "old"},
            headers={**browser_headers("X-Diary-Merge-Begin"), "Content-Type": "application/json"},
            body=json.dumps(body),
        )
        assert status == 403


def test_merge_begin_requires_a_live_owner_session(services: Services) -> None:
    body = json.dumps({"repo": "scripts", "action": "merge", "queue_oid": QUEUE_OID, "base_oid": BASE_OID})
    headers = {**browser_headers("X-Diary-Merge-Begin"), "Content-Type": "application/json"}
    status, _ = http_request(
        services.fresh_server, "POST", "/lock/merge/begin", headers=headers, body=body,
    )
    assert status == 403
    services.auth_state.add("first-factor-only", level=1)
    status, _ = http_request(
        services.fresh_server, "POST", "/lock/merge/begin",
        cookies={SESSION_COOKIE: "first-factor-only"}, headers=headers, body=body,
    )
    assert status == 403


def test_merge_begin_accepts_the_console_form_without_script(services: Services) -> None:
    services.auth_state.add("old")
    body = urllib.parse.urlencode(
        {"repo": "scripts", "action": "deny", "queue_oid": QUEUE_OID, "base_oid": BASE_OID}
    )
    connection = http.client.HTTPConnection("127.0.0.1", services.fresh_server.server_port, timeout=6)
    connection.request(
        "POST", "/lock/merge/begin",
        headers={
            **browser_headers(), "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"{SESSION_COOKIE}=old", "Content-Length": str(len(body)),
        },
        body=body.encode(),
    )
    response = connection.getresponse()
    response.read()
    cookies = [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
    connection.close()
    assert response.status == 303
    assert any(value.startswith(f"{fresh.TRANSACTION_COOKIE}=") for value in cookies)


def test_return_page_shows_the_decision_bound_to_the_ceremony(services: Services) -> None:
    services.auth_state.add("old")
    body = json.dumps({"repo": "scripts", "action": "deny", "queue_oid": QUEUE_OID, "base_oid": BASE_OID})
    connection = http.client.HTTPConnection("127.0.0.1", services.fresh_server.server_port, timeout=6)
    connection.request(
        "POST", "/lock/merge/begin",
        headers={
            **browser_headers("X-Diary-Merge-Begin"), "Content-Type": "application/json",
            "Cookie": f"{SESSION_COOKIE}=old", "Content-Length": str(len(body)),
        },
        body=body.encode(),
    )
    response = connection.getresponse()
    cookie = None
    for name, value in response.getheaders():
        if name.lower() == "set-cookie" and value.startswith(f"{fresh.TRANSACTION_COOKIE}="):
            cookie = value.split(";", 1)[0].split("=", 1)[1]
    response.read()
    connection.close()
    assert cookie is not None
    services.auth_state.add_fresh("new")
    status, raw = http_request(
        services.fresh_server, "GET", "/lock/merge/return",
        cookies={SESSION_COOKIE: "new", fresh.TRANSACTION_COOKIE: cookie},
    )
    page = raw.decode()
    assert status == 200
    assert "Deny (record only" in page
    assert QUEUE_OID in page and BASE_OID in page
    assert 'href="/lock/merge/"' in page


def test_decision_without_a_grant_offers_the_authorization_form(services: Services) -> None:
    services.auth_state.add("old")
    cookies = {SESSION_COOKIE: "old"}
    status, raw = http_request(services.ui_server, "GET", "/lock/merge/", cookies=cookies)
    assert status == 200
    match = re.search(rb'name="csrf" value="([^"]+)"', raw)
    assert match is not None
    status, raw = decide(services, cookies, match.group(1).decode())
    page = raw.decode()
    assert status == 200
    assert 'action="/lock/merge/begin"' in page
    assert QUEUE_OID in page and BASE_OID in page
    assert all(item["cmd"] == "snapshot" for item in services.executor.requests)


def test_unlock_transaction_cannot_be_consumed_as_a_merge_grant(services: Services) -> None:
    services.auth_state.add("old")
    status, _ = http_request(
        services.fresh_server, "POST", "/lock/merge/consume",
        cookies={SESSION_COOKIE: "old"}, headers=browser_headers("X-Diary-Merge-Confirm"),
    )
    assert status == 403


def test_socket_activated_stdio_mode_checks_the_peer_uid_and_answers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConsole:
        def snapshot(self):
            return None

    def fake_console(paths, verifier, authorizer, *, auditor, publisher, lock_state, repo="scripts"):
        return FakeConsole()

    monkeypatch.setattr(executor, "_console", fake_console)
    paths = _paths_by_repo(tmp_path)

    client, server = socket.socketpair()
    try:
        client.sendall(b'{"cmd":"snapshot","repo":"scripts"}\n')
        code = executor.serve_stdio(
            paths, FakeVerifier(), os.geteuid(), auditor=object(), connection=server,  # type: ignore[arg-type]
        )
        assert code == 0
        assert json.loads(client.recv(65536)) == {"ok": True, "snapshot": {"pending": False}}
    finally:
        client.close()
        server.close()

    client, server = socket.socketpair()
    try:
        client.sendall(b'{"cmd":"snapshot","repo":"scripts"}\n')
        code = executor.serve_stdio(
            paths, FakeVerifier(), os.geteuid() + 1, auditor=object(), connection=server,  # type: ignore[arg-type]
        )
        assert code == 1
        assert b"not authorized" in client.recv(65536)
    finally:
        client.close()
        server.close()


def test_anonymous_page_asks_for_authorization_instead_of_reporting_an_outage(
    services: Services,
) -> None:
    """A missing session is an authorization state, not a service outage."""
    status, body = http_request(services.ui_server, "GET", "/lock/merge/")

    assert status == 403
    assert b"Authorization required" in body


def test_first_factor_session_is_refused_as_unauthorized(services: Services) -> None:
    """Password-only Authelia sessions must read as 403, not 503."""
    services.auth_state.add("first", level=1, second=0)
    status, body = http_request(
        services.ui_server, "GET", "/lock/merge/",
        cookies={SESSION_COOKIE: "first"},
    )

    assert status == 403
    assert b"Authorization required" in body


def test_stale_review_form_re_renders_instead_of_dead_ending(services: Services) -> None:
    """A form rendered before a ceremony must not fail as "Refused"."""
    services.auth_state.add("owner")
    body = (
        "repo=scripts&action=merge"
        f"&queue_oid={QUEUE_OID}&base_oid={BASE_OID}&csrf=stale-token"
    )
    status, page = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies={SESSION_COOKIE: "owner"},
        headers={**browser_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )

    assert status == 200
    assert b"expired before it was submitted" in page
    assert b'name="csrf" value="' in page


# ----------------------------------------------------------------- diary queue (CTL-12)


def test_page_shows_which_queue_is_under_review(services: Services) -> None:
    cookies = authorized(services)
    status, raw = http_request(
        services.ui_server, "GET", "/lock/merge/?repo=diary", cookies=cookies,
    )

    assert status == 200
    assert b'<code id="repo">diary</code>' in raw
    assert b"diary repository" in raw
    assert any(request.get("repo") == "diary" for request in services.executor.requests)


def test_unknown_repository_is_refused(services: Services) -> None:
    cookies = authorized(services)

    status, _ = http_request(
        services.ui_server, "GET", "/lock/merge/?repo=elsewhere", cookies=cookies,
    )
    assert status == 403

    body = urllib.parse.urlencode(
        {"repo": "elsewhere", "action": "merge", "queue_oid": QUEUE_OID,
         "base_oid": BASE_OID, "csrf": page_token(services, cookies)},
    )
    status, _ = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies,
        headers={**browser_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    assert status == 403


def test_a_scripts_grant_cannot_authorize_the_diary_queue(services: Services) -> None:
    """The grant binds the repository, so the pair cannot be moved across queues."""
    cookies = authorized(services, repo="scripts")
    token = page_token(services, cookies, repo="diary")
    before = len(services.executor.requests)

    status, raw = decide(services, cookies, token, repo="diary")

    assert status == 200
    assert b"Fresh authorization required" in raw
    assert all(request["cmd"] != "decide" for request in services.executor.requests[before:])


def test_executor_selects_the_path_set_for_the_requested_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, object]] = []

    def fake_console(paths, verifier, authorizer, *, auditor, publisher, lock_state, repo="scripts"):
        seen.append((repo, paths))

        class EmptyConsole:
            def snapshot(self):
                return None

        return EmptyConsole()

    monkeypatch.setattr(executor, "_console", fake_console)
    scripts = _console_paths(tmp_path)
    diary = object()

    reply = executor.handle_request(
        {"cmd": "snapshot", "repo": "diary"},
        {"scripts": scripts, "diary": diary},  # type: ignore[dict-item]
        FakeVerifier(), auditor=object(),
    )

    assert reply["ok"] is True
    assert seen == [("diary", diary)]


@pytest.mark.parametrize("request_value", [
    {"cmd": "snapshot"},
    {"cmd": "snapshot", "repo": "elsewhere"},
    {"cmd": "snapshot", "repo": "diary", "extra": 1},
])
def test_executor_requires_exactly_one_known_repository(
    tmp_path: Path, request_value: dict[str, Any],
) -> None:
    with pytest.raises(executor.ExecutorError):
        executor.handle_request(
            request_value, _paths_by_repo(tmp_path), FakeVerifier(), auditor=object(),
        )


def test_diary_publication_does_not_sync_a_python_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diary approval has no venv; only the scripts repository does."""
    calls: list[tuple[str, bool, str | None]] = []

    def fake_approve(reviewed, paths, *, expected_base=None, sync_environment=True):
        calls.append((reviewed, sync_environment, expected_base))
        return True

    monkeypatch.setattr(executor.queue, "approve_queue", fake_approve)
    executor._publication("diary")("a" * 40, object(), expected_base="b" * 40)  # type: ignore[arg-type]
    executor._publication("scripts")("a" * 40, object(), expected_base="b" * 40)  # type: ignore[arg-type]

    # The reviewed base still reaches the publisher for both repositories; only
    # the venv sync differs.
    assert calls == [("a" * 40, False, "b" * 40), ("a" * 40, True, "b" * 40)]


def test_decide_accepts_a_browser_that_omits_origin(services: Services) -> None:
    """Safari omits Origin on same-origin form posts; the token still authorizes."""
    cookies = authorized(services)
    token = page_token(services, cookies)
    body = urllib.parse.urlencode({
        "repo": "scripts", "action": "merge",
        "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "csrf": token,
    })
    status, raw = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies,
        headers={
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )

    assert status == 200
    assert b"Merged" in raw


def test_decide_refuses_cross_site_fetch_metadata(services: Services) -> None:
    """A cross-site form post is refused even without an Origin header."""
    cookies = authorized(services)
    token = page_token(services, cookies)
    body = urllib.parse.urlencode({
        "repo": "scripts", "action": "merge",
        "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "csrf": token,
    })
    status, _ = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies,
        headers={
            "Sec-Fetch-Site": "cross-site",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )

    assert status == 403


def test_decide_accepts_an_opaque_null_origin(services: Services) -> None:
    """Firefox sends Origin: null for same-origin form posts; accept it."""
    cookies = authorized(services)
    token = page_token(services, cookies)
    body = urllib.parse.urlencode({
        "repo": "scripts", "action": "merge",
        "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "csrf": token,
    })
    status, raw = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies,
        headers={
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )

    assert status == 200
    assert b"Merged" in raw


def test_decision_rejects_a_confirm_field_that_disagrees(services: Services) -> None:
    """The submit button must name the same action that is being decided."""
    cookies = authorized(services)
    token = page_token(services, cookies)
    body = urllib.parse.urlencode({
        "repo": "scripts", "action": "merge",
        "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "csrf": token,
        "confirm": "deny",
    })
    status, _ = http_request(
        services.ui_server, "POST", "/lock/merge/decide",
        cookies=cookies,
        headers={**browser_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )

    assert status == 403


def _merge_begin(services: Services, origin: str) -> int:
    body = json.dumps({
        "action": "merge", "queue_oid": QUEUE_OID, "base_oid": BASE_OID, "repo": "scripts",
    })
    connection = http.client.HTTPConnection(
        "127.0.0.1", services.fresh_server.server_port, timeout=6,
    )
    connection.request(
        "POST", "/lock/merge/begin",
        headers={
            "X-Diary-Merge-Begin": "yes",
            "Content-Type": "application/json",
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": f"{SESSION_COOKIE}=owner",
        },
        body=body,
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_merge_begin_accepts_an_opaque_origin(services: Services) -> None:
    """Firefox sends Origin: null; the ceremony must still start."""
    services.auth_state.add("owner")

    assert _merge_begin(services, "null") == 303


def test_merge_begin_refuses_a_foreign_origin(services: Services) -> None:
    services.auth_state.add("owner")

    assert _merge_begin(services, "https://evil.example") == 403


def test_begin_capacity_failure_leaves_the_owner_signed_in(services: Services) -> None:
    """D5: a full transaction table must not sign the owner out for nothing."""
    services.auth_state.add("owner")
    store = services.fresh_server.transactions
    while True:
        try:
            store.create(
                "owner", 300, purpose="merge", action="merge",
                queue_oid=QUEUE_OID, base_oid=BASE_OID, repo="scripts",
            )
        except fresh.CapacityError:
            break
    before = services.auth_state.logout_calls

    status = _merge_begin(services, "https://example.test")

    assert status == 503
    assert services.auth_state.logout_calls == before, "session was destroyed before capacity was checked"


def test_stale_pair_is_refused_before_the_grant_is_consumed(services: Services) -> None:
    """D3: a queue change costs a re-review, not a new password+passkey."""
    cookies = authorized(services)
    token = page_token(services, cookies)
    services.executor.snapshot = {**services.executor.snapshot, "queue_oid": OTHER_OID}

    status, page = decide(services, cookies, token)

    assert status == 200
    assert b"changed after you reviewed" in page
    transaction_id, secret = cookies[fresh.TRANSACTION_COOKIE].split(".", 1)
    # Still usable: the grant was not spent on the refused decision.
    details = services.fresh_server.transactions.details(
        transaction_id, secret, "owner", "fresh",
    )
    assert details["purpose"] == "merge"
