from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import diary_fresh_unlock as fresh


class FakeAutheliaState:
    def __init__(self) -> None:
        self.sessions: dict[str, tuple[str, str]] = {}
        self.logout_calls = 0
        self.logout_status = 200


class FakeAutheliaHandler(BaseHTTPRequestHandler):
    state: FakeAutheliaState

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _session(self) -> str:
        header = self.headers.get("Cookie", "")
        for item in header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "diary_lock_authelia_session":
                return value
        return ""

    def _reply(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/authelia/api/logout":
            self._reply(404)
            return
        self.state.logout_calls += 1
        self.state.sessions.pop(self._session(), None)
        self._reply(self.state.logout_status)

    def do_GET(self) -> None:
        if not self.path.startswith("/api/authz/forward-auth?"):
            self._reply(404)
            return
        session = self.state.sessions.get(self._session())
        if session is None:
            self._reply(401)
            return
        username, groups = session
        self._reply(200, {"Remote-User": username, "Remote-Groups": groups})


@pytest.fixture
def authelia() -> Iterator[tuple[FakeAutheliaState, ThreadingHTTPServer]]:
    state = FakeAutheliaState()
    handler = type("BoundFakeAutheliaHandler", (FakeAutheliaHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, server
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def service(
    tmp_path: Path, authelia: tuple[FakeAutheliaState, ThreadingHTTPServer],
) -> Iterator[tuple[fresh.FreshUnlockServer, FakeAutheliaState]]:
    state, auth_server = authelia
    config = fresh.Config(
        owner_username="owner",
        public_origin="https://owner.example.test",
        authelia_backend=f"http://127.0.0.1:{auth_server.server_port}",
        session_cookie="diary_lock_authelia_session",
        transaction_ttl=300,
    )
    server = fresh.FreshUnlockServer(("127.0.0.1", 0), config, tmp_path / "transactions.sqlite3")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, state
    server.shutdown()
    server.server_close()
    thread.join()


def request(
    server: fresh.FreshUnlockServer, method: str, path: str,
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], list[tuple[str, str]], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    all_headers = dict(headers or {})
    if cookies:
        all_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    connection.request(method, path, headers=all_headers)
    response = connection.getresponse()
    body = response.read()
    result = response.status, dict(response.getheaders()), response.getheaders(), body
    connection.close()
    return result


def browser_headers(action: str) -> dict[str, str]:
    return {
        "Origin": "https://owner.example.test",
        "Sec-Fetch-Site": "same-origin",
        action: "yes",
    }


def begin(
    server: fresh.FreshUnlockServer, cookies: dict[str, str] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    status, _, headers, _ = request(
        server, "POST", "/lock/unlock/begin", cookies,
        browser_headers("X-Diary-Unlock-Begin"),
    )
    assert status == 303
    transaction = next(
        value for name, value in headers
        if name.lower() == "set-cookie" and value.startswith(f"{fresh.TRANSACTION_COOKIE}=")
    )
    value = transaction.split("=", 1)[1].split(";", 1)[0]
    assert "Max-Age=300" in transaction
    assert "Secure" in transaction and "HttpOnly" in transaction and "SameSite=Strict" in transaction
    return value, headers


def test_full_flow_invalidates_old_session_and_rejects_replay(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    state.sessions["old"] = ("owner", "lock-owner")
    transaction, headers = begin(server, {"diary_lock_authelia_session": "old"})
    assert state.logout_calls == 1
    assert "old" not in state.sessions
    location = dict(headers)["Location"]
    assert location.startswith("https://owner.example.test/authelia/?rd=")
    assert "%2Flock%2Funlock%2Freturn" in location

    stale = {"diary_lock_authelia_session": "old", fresh.TRANSACTION_COOKIE: transaction}
    status, _, _, _ = request(server, "GET", "/lock/unlock/return", stale)
    assert status == 403

    state.sessions["new"] = ("owner", "lock-owner")
    current = {"diary_lock_authelia_session": "new", fresh.TRANSACTION_COOKIE: transaction}
    status, _, _, body = request(server, "GET", "/lock/unlock/return", current)
    assert status == 200
    assert json.loads(body) == {"status": "fresh"}
    status, _, _, _ = request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )
    assert status == 204
    status, _, _, _ = request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )
    assert status == 403


def test_cookie_only_agent_sessions_and_spoofed_headers_fail(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    state.sessions["new"] = ("owner", "lock-owner")
    for cookies in (
        {"diary_lock_authelia_session": "new"},
        {fresh.TRANSACTION_COOKIE: transaction},
        {"dsh_session": "stolen", "kimi_session": "stolen"},
        {"dsh_session": "stolen", fresh.TRANSACTION_COOKIE: transaction},
    ):
        status, _, _, _ = request(
            server, "POST", "/lock/unlock/consume", cookies,
            browser_headers("X-Diary-Unlock-Confirm"),
        )
        assert status == 403
    status, _, _, _ = request(
        server, "GET", "/lock/unlock/return",
        {"diary_lock_authelia_session": "new", fresh.TRANSACTION_COOKIE: transaction},
        {"Remote-User": "owner", "Remote-Groups": "lock-owner"},
    )
    assert status == 200


def test_pending_wrong_owner_and_lost_second_factor_cannot_consume(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    state.sessions["new"] = ("owner", "lock-owner")
    cookies = {"diary_lock_authelia_session": "new", fresh.TRANSACTION_COOKIE: transaction}
    status, _, _, _ = request(
        server, "POST", "/lock/unlock/consume", cookies,
        browser_headers("X-Diary-Unlock-Confirm"),
    )
    assert status == 403

    state.sessions["new"] = ("other", "lock-owner")
    status, _, _, _ = request(server, "GET", "/lock/unlock/return", cookies)
    assert status == 403
    state.sessions["new"] = ("owner", "lock-owner")
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 200
    del state.sessions["new"]
    status, _, _, _ = request(
        server, "POST", "/lock/unlock/consume", cookies,
        browser_headers("X-Diary-Unlock-Confirm"),
    )
    assert status == 403


def test_logout_failure_creates_no_transaction(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    state.logout_status = 500
    status, _, _, _ = request(
        server, "POST", "/lock/unlock/begin", None,
        browser_headers("X-Diary-Unlock-Begin"),
    )
    assert status == 403
    with sqlite3.connect(server.transactions.path) as connection:
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_begin_requires_explicit_same_origin_action(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    for headers in ({}, {"Origin": "https://attacker.example.test"}, browser_headers("X-Diary-Unlock-Begin") | {"Content-Length": "1"}):
        status, _, _, _ = request(server, "POST", "/lock/unlock/begin", headers=headers)
        assert status == 403
    assert state.logout_calls == 0


def test_store_expiry_restart_and_secret_hashing(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "transactions.sqlite3"
    store = fresh.TransactionStore(path, clock=lambda: now[0])
    transaction_id, secret = store.create("owner", 30)
    assert secret.encode("ascii") not in path.read_bytes()
    with pytest.raises(fresh.AuthorizationError):
        store.authenticate(transaction_id, "wrong", "owner")
    store.authenticate(transaction_id, secret, "owner")
    with pytest.raises(fresh.AuthorizationError):
        store.authenticate(transaction_id, secret, "owner")
    reopened = fresh.TransactionStore(path, clock=lambda: now[0])
    reopened.consume(transaction_id, secret, "owner")
    with pytest.raises(fresh.AuthorizationError):
        reopened.consume(transaction_id, secret, "owner")

    second_id, second_secret = store.create("owner", 30)
    now[0] += 31
    with pytest.raises(fresh.AuthorizationError):
        reopened.authenticate(second_id, second_secret, "owner")


def test_config_rejects_non_loopback_and_long_lived(tmp_path: Path) -> None:
    base = {
        "owner_username": "owner",
        "public_origin": "https://owner.example.test",
        "authelia_backend": "http://127.0.0.1:9091",
        "session_cookie": "diary_lock_authelia_session",
    }
    path = tmp_path / "config.toml"
    path.write_text("\n".join(f'{key} = "{value}"' for key, value in base.items()))
    assert fresh.load_config(path).transaction_ttl == 300
    path.write_text(path.read_text() + "\ntransaction_ttl_seconds = 301\n")
    with pytest.raises(fresh.AuthorizationError):
        fresh.load_config(path)
    path.write_text(path.read_text().replace("http://127.0.0.1:9091", "https://auth.example.test"))
    with pytest.raises(fresh.AuthorizationError):
        fresh.load_config(path)
