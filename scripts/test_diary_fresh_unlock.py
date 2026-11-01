from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import diary_fresh_unlock as fresh


@dataclass
class FakeSession:
    username: str = "owner"
    groups: str = "lock-owner"
    level: int = 2
    first: int = 1
    second: int = 1
    password: bool = True
    webauthn: bool = True
    user_verified: bool = True


class FakeAutheliaState:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.logout_calls = 0
        self.logout_status = 200
        self.logout_body: dict[str, Any] | None = None
        self.destroy_on_logout = True
        self.logout_entered = threading.Event()
        self.logout_release = threading.Event()
        self.logout_release.set()

    def add(self, name: str, **values: Any) -> None:
        self.sessions[name] = FakeSession(**values)

    def add_fresh(self, name: str, **values: Any) -> None:
        now = int(time.time())
        self.add(name, **{"first": now + 1, "second": now + 2, **values})


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

    def _reply(self, status: int, value: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value).encode("ascii")
        self.send_response(status)
        for name, item in (headers or {}).items():
            self.send_header(name, item)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != "/authelia/api/logout":
            self._reply(404, {"status": "KO"})
            return
        self.state.logout_calls += 1
        self.state.logout_entered.set()
        self.state.logout_release.wait(timeout=5)
        if self.state.destroy_on_logout:
            self.state.sessions.pop(self._session(), None)
        body = self.state.logout_body or {"status": "OK", "data": {"safeTargetURL": False}}
        self._reply(self.state.logout_status, body)

    def do_GET(self) -> None:
        session_name = self._session()
        session = self.state.sessions.get(session_name)
        if self.path.startswith("/api/authz/forward-auth?"):
            if session is None or session.level != 2:
                self._reply(401, {"status": "KO"})
                return
            self._reply(
                200, {"status": "OK"},
                {"Remote-User": session.username, "Remote-Groups": session.groups},
            )
            return
        if self.path == "/authelia/api/state":
            if session is None:
                self._reply(403, {"status": "KO"})
                return
            self._reply(200, {"status": "OK", "data": {
                "username": session.username,
                "authentication_level": session.level,
                "factor_knowledge": session.password,
                "first_factor_authn_timestamp": session.first,
                "second_factor_authn_timestamp": session.second,
                "username_and_password": session.password,
                "webauthn": session.webauthn,
                "webauthn_user_verified": session.user_verified,
            }})
            return
        self._reply(404, {"status": "KO"})


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
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
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


def test_full_flow_binds_session_and_rejects_substitution_and_replay(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    state.add("old")
    transaction, headers = begin(server, {"diary_lock_authelia_session": "old"})
    assert state.logout_calls == 1
    assert "old" not in state.sessions
    location = dict(headers)["Location"]
    assert location.startswith("https://owner.example.test/authelia/?rd=")
    assert "%2Flock%2Funlock%2Freturn" in location

    stale = {"diary_lock_authelia_session": "old", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", stale)[0] == 403

    state.add_fresh("new")
    state.add_fresh("alternate")
    current = {"diary_lock_authelia_session": "new", fresh.TRANSACTION_COOKIE: transaction}
    status, _, _, body = request(server, "GET", "/lock/unlock/return", current)
    assert status == 200
    assert json.loads(body) == {"status": "fresh"}
    substituted = {"diary_lock_authelia_session": "alternate", fresh.TRANSACTION_COOKIE: transaction}
    assert request(
        server, "POST", "/lock/unlock/consume", substituted,
        browser_headers("X-Diary-Unlock-Confirm"),
    )[0] == 403
    assert request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )[0] == 204
    assert request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )[0] == 403


def test_omitted_and_alternate_preexisting_sessions_never_become_fresh(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    state.add("preexisting")
    transaction, _ = begin(server)
    cookies = {"diary_lock_authelia_session": "preexisting", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403

    state.add("a")
    transaction, _ = begin(server, {"diary_lock_authelia_session": "a"})
    assert "a" not in state.sessions and "preexisting" in state.sessions
    cookies = {"diary_lock_authelia_session": "preexisting", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403


def test_stale_second_factor_or_missing_user_verification_fails(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    state.add_fresh("stale-webauthn", second=1)
    cookies = {"diary_lock_authelia_session": "stale-webauthn", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403
    state.add_fresh("no-uv", user_verified=False)
    cookies = {"diary_lock_authelia_session": "no-uv", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403


def test_cookie_only_agent_sessions_and_spoofed_headers_fail(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    state.add_fresh("new")
    for cookies in (
        {"diary_lock_authelia_session": "new"},
        {fresh.TRANSACTION_COOKIE: transaction},
        {"dsh_session": "stolen", "kimi_session": "stolen"},
        {"dsh_session": "stolen", fresh.TRANSACTION_COOKIE: transaction},
    ):
        assert request(
            server, "POST", "/lock/unlock/consume", cookies,
            browser_headers("X-Diary-Unlock-Confirm"),
        )[0] == 403
    assert request(
        server, "GET", "/lock/unlock/return",
        {"diary_lock_authelia_session": "new", fresh.TRANSACTION_COOKIE: transaction},
        {"Remote-User": "owner", "Remote-Groups": "lock-owner"},
    )[0] == 200


@pytest.mark.parametrize(
    "body,destroy",
    [
        ({"status": "KO", "message": "Operation failed"}, True),
        ({"status": "OK", "data": {"safeTargetURL": False}}, False),
    ],
)
def test_http_200_logout_failure_creates_no_transaction(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
    body: dict[str, Any], destroy: bool,
) -> None:
    server, state = service
    state.add("old")
    state.logout_body = body
    state.destroy_on_logout = destroy
    assert request(
        server, "POST", "/lock/unlock/begin", {"diary_lock_authelia_session": "old"},
        browser_headers("X-Diary-Unlock-Begin"),
    )[0] == 403
    with sqlite3.connect(server.transactions.path) as connection:
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_global_creation_rate_limit_and_live_capacity(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, _ = service
    server.begin_rate.limit = 2
    begin(server)
    begin(server)
    assert request(
        server, "POST", "/lock/unlock/begin", None,
        browser_headers("X-Diary-Unlock-Begin"),
    )[0] == 429

    server.begin_rate.limit = 8
    server.transactions.max_live = 2
    assert request(
        server, "POST", "/lock/unlock/begin", None,
        browser_headers("X-Diary-Unlock-Begin"),
    )[0] == 503
    with sqlite3.connect(server.transactions.path) as connection:
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2


def test_bounded_request_concurrency(
    service: tuple[fresh.FreshUnlockServer, FakeAutheliaState],
) -> None:
    server, state = service
    server.request_slots = threading.BoundedSemaphore(1)
    state.logout_release.clear()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(request(
        server, "POST", "/lock/unlock/begin", None,
        browser_headers("X-Diary-Unlock-Begin"),
    )[0]))
    thread.start()
    assert state.logout_entered.wait(timeout=2)
    assert request(server, "GET", "/lock/health")[0] == 503
    state.logout_release.set()
    thread.join(timeout=6)
    assert result == [303]


def test_store_expiry_restart_pruning_and_secret_hashing(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "transactions.sqlite3"
    store = fresh.TransactionStore(path, clock=lambda: now[0], max_live=2)
    transaction_id, secret = store.create("owner", 30)
    session_hash = fresh._digest("session")
    assert secret.encode("ascii") not in path.read_bytes()
    with pytest.raises(fresh.AuthorizationError):
        store.authenticate(transaction_id, "wrong", "owner", session_hash)
    store.authenticate(transaction_id, secret, "owner", session_hash)
    with pytest.raises(fresh.AuthorizationError):
        store.authenticate(transaction_id, secret, "owner", session_hash)
    reopened = fresh.TransactionStore(path, clock=lambda: now[0], max_live=2)
    with pytest.raises(fresh.AuthorizationError):
        reopened.consume(transaction_id, secret, "owner", fresh._digest("other"))
    reopened.consume(transaction_id, secret, "owner", session_hash)
    with pytest.raises(fresh.AuthorizationError):
        reopened.consume(transaction_id, secret, "owner", session_hash)

    second_id, second_secret = store.create("owner", 30)
    now[0] += 31
    with pytest.raises(fresh.AuthorizationError):
        reopened.authenticate(second_id, second_secret, "owner", session_hash)
    third_id, _ = store.create("owner", 30)
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT id FROM transactions").fetchall()
    assert rows == [(third_id,)]


def test_legacy_transactions_are_invalidated_on_schema_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "transactions.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE transactions ("
            "id TEXT PRIMARY KEY, secret_hash BLOB NOT NULL, owner TEXT NOT NULL, "
            "state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, "
            "authenticated_at INTEGER, consumed_at INTEGER)"
        )
        connection.execute("INSERT INTO transactions VALUES ('legacy', ?, 'owner', 'fresh', 1, 999, NULL, NULL)", (fresh._digest("secret"),))
    store = fresh.TransactionStore(path, max_live=2)
    with pytest.raises(fresh.AuthorizationError):
        store.consume("legacy", "secret", "owner", fresh._digest("session"))


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
