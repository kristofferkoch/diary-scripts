from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import sqlite3
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from scripts import diary_fresh_unlock as fresh


class FakeSessionState:
    def __init__(self, server: fresh.FreshUnlockServer) -> None:
        self.server = server
        self.sessions: dict[str, str] = {}
        self.logout_calls = 0
        self.logout_entered = threading.Event()
        self.logout_release = threading.Event()
        self.logout_release.set()
        original_logout = server.passkeys.logout

        def logout(cookie_header: str | None) -> None:
            self.logout_calls += 1
            self.logout_entered.set()
            self.logout_release.wait(timeout=5)
            name = fresh._cookies(cookie_header).get(server.config.session_cookie, "")
            original_logout(cookie_header)
            self.sessions.pop(name, None)

        server.passkeys.logout = logout

    def add(
        self, name: str, username: str = "owner", level: int = 2,
        authenticated_at: float | None = None, transaction_id: str | None = None,
        **values: Any,
    ) -> None:
        self.sessions[name] = username
        if level != 2:
            return
        now = time.time()
        if "second" in values:
            authenticated_at = values["second"]
        with sqlite3.connect(self.server.transactions.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO passkey_sessions "
                "(token_hash, owner, authenticated_at, expires_at, transaction_id) VALUES (?, ?, ?, ?, ?)",
                (
                    fresh._digest(name), username,
                    authenticated_at if authenticated_at is not None else now - 10,
                    now + 600, transaction_id,
                ),
            )

    def add_fresh(self, name: str, transaction_id: str | None = None, **values: Any) -> None:
        if transaction_id is None:
            with sqlite3.connect(self.server.transactions.path) as connection:
                row = connection.execute(
                    "SELECT id FROM transactions WHERE state = 'pending' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            transaction_id = row[0] if row else None
        self.add(
            name, transaction_id=transaction_id,
            authenticated_at=values.pop("first", time.time() + 1), **values,
        )


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[fresh.FreshUnlockServer, FakeSessionState]]:
    config = fresh.Config(
        owner_username="owner",
        public_origin="https://example.test",
        session_cookie="diary_lock_session",
        transaction_ttl=300,
        session_ttl=600,
    )
    server = fresh.FreshUnlockServer(
        ("127.0.0.1", 0), config, tmp_path / "transactions.sqlite3",
        enrollment_code="test-enrollment-code",
    )
    state = FakeSessionState(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, state
    server.shutdown()
    server.server_close()
    thread.join()


def request(
    server: fresh.FreshUnlockServer, method: str, path: str,
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
) -> tuple[int, dict[str, str], list[tuple[str, str]], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
    all_headers = dict(headers or {})
    if cookies:
        all_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    connection.request(method, path, headers=all_headers, body=body)
    response = connection.getresponse()
    body = response.read()
    result = response.status, dict(response.getheaders()), response.getheaders(), body
    connection.close()
    return result


def browser_headers(action: str) -> dict[str, str]:
    return {
        "Origin": "https://example.test",
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
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    state.add("old")
    transaction, headers = begin(server, {"diary_lock_session": "old"})
    assert state.logout_calls == 1
    assert "old" not in state.sessions
    location = dict(headers)["Location"]
    assert location.startswith("https://example.test/lock/auth/?rd=")
    assert "%2Flock%2Funlock%2Freturn" in location

    stale = {"diary_lock_session": "old", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", stale)[0] == 403

    state.add_fresh("new")
    state.add_fresh("alternate")
    current = {"diary_lock_session": "new", fresh.TRANSACTION_COOKIE: transaction}
    status, _, _, body = request(server, "GET", "/lock/unlock/return", current)
    assert status == 200
    assert json.loads(body) == {"status": "fresh"}
    substituted = {"diary_lock_session": "alternate", fresh.TRANSACTION_COOKIE: transaction}
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
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    state.add("preexisting")
    transaction, _ = begin(server)
    cookies = {"diary_lock_session": "preexisting", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403

    state.add("a")
    transaction, _ = begin(server, {"diary_lock_session": "a"})
    assert "a" not in state.sessions and "preexisting" in state.sessions
    cookies = {"diary_lock_session": "preexisting", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403


def test_stale_or_transaction_unbound_session_fails(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    transaction_id = transaction.split(".", 1)[0]
    state.add("stale-passkey", transaction_id=transaction_id, authenticated_at=1)
    cookies = {"diary_lock_session": "stale-passkey", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403
    state.add("unbound")
    cookies = {"diary_lock_session": "unbound", fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403


def test_session_from_one_transaction_cannot_authorize_another(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    first, _ = begin(server)
    first_id = first.split(".", 1)[0]
    state.add_fresh("first-session", transaction_id=first_id)
    second, _ = begin(server)
    cookies = {"diary_lock_session": "first-session", fresh.TRANSACTION_COOKIE: second}
    assert request(server, "GET", "/lock/unlock/return", cookies)[0] == 403


def test_cookie_only_agent_sessions_and_spoofed_headers_fail(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    transaction, _ = begin(server)
    state.add_fresh("new")
    for cookies in (
        {"diary_lock_session": "new"},
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
        {"diary_lock_session": "new", fresh.TRANSACTION_COOKIE: transaction},
        {"Remote-User": "owner", "Remote-Groups": "lock-owner"},
    )[0] == 200


def test_global_creation_rate_limit_and_live_capacity(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
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
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
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


def test_config_rejects_long_lived_transactions_and_sessions(tmp_path: Path) -> None:
    base = {
        "owner_username": "owner",
        "public_origin": "https://example.test",
        "auth_backend": "http://localhost:9092",
        "session_cookie": "diary_lock_session",
    }
    path = tmp_path / "config.toml"
    path.write_text("\n".join(f'{key} = "{value}"' for key, value in base.items()))
    assert fresh.load_config(path).transaction_ttl == 300
    path.write_text(path.read_text() + "\ntransaction_ttl_seconds = 301\n")
    with pytest.raises(fresh.AuthorizationError):
        fresh.load_config(path)
    path.write_text(path.read_text().replace("transaction_ttl_seconds = 301", "session_ttl_seconds = 3601"))
    with pytest.raises(fresh.AuthorizationError):
        fresh.load_config(path)


def test_stale_verify_socket_is_discarded(tmp_path: Path) -> None:
    """A socket left behind by a crashed instance must not wedge a restart."""
    import socket

    path = tmp_path / "verify.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()

    fresh.discard_stale_socket(str(path))

    assert not path.exists()


def test_live_verify_socket_is_not_discarded(tmp_path: Path) -> None:
    """A socket someone is still listening on must stop the service, not be stolen."""
    import socket

    path = tmp_path / "verify.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    try:
        with pytest.raises(RuntimeError):
            fresh.discard_stale_socket(str(path))
    finally:
        listener.close()


def test_non_socket_verify_path_is_refused(tmp_path: Path) -> None:
    """Never delete something that is not a socket."""
    path = tmp_path / "verify.sock"
    path.write_text("not a socket")

    with pytest.raises(RuntimeError):
        fresh.discard_stale_socket(str(path))
    assert path.read_text() == "not a socket"


def _consumed_merge_grant(store: fresh.TransactionStore, repo: str) -> str:
    transaction_id, secret = store.create(
        "owner", 300, purpose="merge", action="merge",
        queue_oid="a" * 40, base_oid="b" * 40, repo=repo,
    )
    store.authenticate(transaction_id, secret, "owner", fresh._digest("session"))
    store.consume(transaction_id, secret, "owner", fresh._digest("session"))
    return transaction_id


def test_merge_grant_is_bound_to_its_repository(tmp_path: Path) -> None:
    """A consumed grant for one queue can never authorize the other."""
    store = fresh.TransactionStore(tmp_path / "transactions.sqlite3", max_live=4)
    for repo, other in (("scripts", "diary"), ("diary", "scripts")):
        transaction_id = _consumed_merge_grant(store, repo)
        assert store.verify_consumed(
            transaction_id, repo, "merge", "a" * 40, "b" * 40,
        ) is not None
        assert store.verify_consumed(
            transaction_id, other, "merge", "a" * 40, "b" * 40,
        ) is None


def test_merge_grant_requires_a_known_repository(tmp_path: Path) -> None:
    store = fresh.TransactionStore(tmp_path / "transactions.sqlite3", max_live=4)
    with pytest.raises(fresh.AuthorizationError):
        store.create(
            "owner", 300, purpose="merge", action="merge",
            queue_oid="a" * 40, base_oid="b" * 40,
        )
    with pytest.raises(fresh.AuthorizationError):
        store.create(
            "owner", 300, purpose="merge", action="merge",
            queue_oid="a" * 40, base_oid="b" * 40, repo="elsewhere",
        )


def test_existing_rows_gain_the_scripts_repository_on_migration(tmp_path: Path) -> None:
    """Rows created before the diary queue are scripts grants, not unknown ones."""
    path = tmp_path / "transactions.sqlite3"
    store = fresh.TransactionStore(path, max_live=4)
    transaction_id = _consumed_merge_grant(store, "scripts")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE transactions SET repo = 'scripts'")
    reopened = fresh.TransactionStore(path, max_live=4)
    assert reopened.verify_consumed(
        transaction_id, "scripts", "merge", "a" * 40, "b" * 40,
    ) is not None


# ------------------------------------------- browser-facing failures (D4)


def test_merge_return_failure_renders_html_for_the_browser(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    """A missing transaction is a page for the owner, not a JSON API error."""
    server, _ = service

    status, headers, _, body = request(server, "GET", "/lock/merge/return")

    assert status == 403
    assert headers["Content-Type"].startswith("text/html")
    assert b'href="/lock/merge/"' in body
    assert b"Nothing was merged" in body
    assert b"fresh authorization required" not in body


def test_merge_confirmation_page_states_the_expiry(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, state = service
    state.add("old")
    payload = json.dumps({
        "action": "merge", "queue_oid": "a" * 40, "base_oid": "b" * 40, "repo": "scripts",
    })
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
    connection.request(
        "POST", "/lock/merge/begin", body=payload,
        headers={
            "X-Diary-Merge-Begin": "yes",
            "Content-Type": "application/json",
            "Origin": "https://example.test",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": "diary_lock_session=old",
        },
    )
    response = connection.getresponse()
    response.read()
    assert response.status == 303
    transaction = next(
        value for name, value in response.getheaders()
        if name.lower() == "set-cookie" and value.startswith(f"{fresh.TRANSACTION_COOKIE}=")
    )
    value = transaction.split("=", 1)[1].split(";", 1)[0]
    connection.close()

    status, _, _, login_page = request(
        server, "GET", "/lock/auth/", {fresh.TRANSACTION_COOKIE: value},
    )
    assert status == 200
    assert b"Merge and publish" in login_page
    assert b"a" * 40 in login_page and b"b" * 40 in login_page

    state.add_fresh("new")
    status, _, _, page = request(
        server, "GET", "/lock/merge/return",
        {"diary_lock_session": "new", fresh.TRANSACTION_COOKIE: value},
    )

    assert status == 200
    assert b"expires on its own after" in page
    assert b"5 minutes" in page


@dataclass
class VirtualPasskey:
    key: Any
    credential_id: bytes
    sign_count: int = 0

    @classmethod
    def create(cls) -> VirtualPasskey:
        return cls(ec.generate_private_key(ec.SECP256R1()), secrets.token_bytes(32))

    def registration(self, options: dict[str, Any], user_verified: bool = True) -> dict[str, Any]:
        challenge = options["challenge"]
        client_data = json.dumps({
            "type": "webauthn.create", "challenge": challenge,
            "origin": "https://example.test", "crossOrigin": False,
        }, separators=(",", ":")).encode()
        numbers = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps({
            1: 2, 3: -7, -1: 1,
            -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big"),
        })
        flags = 0x41 | (0x04 if user_verified else 0)
        auth_data = (
            hashlib.sha256(b"example.test").digest() + bytes([flags]) + struct.pack(">I", 0)
            + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose_key
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": fresh.bytes_to_base64url(self.credential_id),
            "rawId": fresh.bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": fresh.bytes_to_base64url(client_data),
                "attestationObject": fresh.bytes_to_base64url(attestation),
            },
        }

    def authentication(self, options: dict[str, Any]) -> dict[str, Any]:
        client_data = json.dumps({
            "type": "webauthn.get", "challenge": options["challenge"],
            "origin": "https://example.test", "crossOrigin": False,
        }, separators=(",", ":")).encode()
        self.sign_count += 1
        auth_data = (
            hashlib.sha256(b"example.test").digest() + b"\x05" + struct.pack(">I", self.sign_count)
        )
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": fresh.bytes_to_base64url(self.credential_id),
            "rawId": fresh.bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": fresh.bytes_to_base64url(client_data),
                "authenticatorData": fresh.bytes_to_base64url(auth_data),
                "signature": fresh.bytes_to_base64url(signature),
                "userHandle": None,
            },
        }


def passkey_request(
    server: fresh.FreshUnlockServer, path: str, action: str,
    cookies: dict[str, str] | None = None, body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], list[tuple[str, str]], bytes]:
    headers = {
        "Origin": "https://example.test", "Sec-Fetch-Site": "same-origin",
        "X-Diary-Passkey-Action": action,
    }
    encoded = None
    if body is not None:
        encoded = json.dumps(body)
        headers["Content-Type"] = "application/json"
    return request(server, "POST", path, cookies, headers, encoded)


def cookie_value(headers: list[tuple[str, str]], name: str) -> str:
    value = next(
        item for key, item in headers
        if key.lower() == "set-cookie" and item.startswith(f"{name}=")
    )
    assert "Secure" in value and "HttpOnly" in value and "SameSite=Strict" in value
    assert "Path=/lock" in value
    return value.split("=", 1)[1].split(";", 1)[0]


def test_real_webauthn_enrollment_login_and_single_use_unlock(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, _ = service
    passkey = VirtualPasskey.create()
    status, _, headers, body = passkey_request(
        server, "/lock/enroll/options", "options", body={"code": "test-enrollment-code"},
    )
    assert status == 200
    ceremony = cookie_value(headers, fresh.CEREMONY_COOKIE)
    options = json.loads(body)["publicKey"]
    status, _, headers, _ = passkey_request(
        server, "/lock/enroll/complete", "complete",
        {fresh.CEREMONY_COOKIE: ceremony}, passkey.registration(options),
    )
    assert status == 200
    enrolled_session = cookie_value(headers, server.config.session_cookie)
    assert request(
        server, "GET", "/lock/auth/forward-auth", {"diary_lock_session": enrolled_session},
    )[0] == 200
    assert passkey_request(
        server, "/lock/enroll/options", "options", body={"code": "test-enrollment-code"},
    )[0] == 403

    transaction, _ = begin(server, {"diary_lock_session": enrolled_session})
    status, _, _, page = request(
        server, "GET", "/lock/auth/", {fresh.TRANSACTION_COOKIE: transaction},
    )
    assert status == 200 and b"Authorize unlock" in page
    status, _, headers, body = passkey_request(
        server, "/lock/auth/options", "options", {fresh.TRANSACTION_COOKIE: transaction},
    )
    assert status == 200
    login_ceremony = cookie_value(headers, fresh.CEREMONY_COOKIE)
    assertion = passkey.authentication(json.loads(body)["publicKey"])
    login_cookies = {
        fresh.CEREMONY_COOKIE: login_ceremony, fresh.TRANSACTION_COOKIE: transaction,
    }
    status, _, headers, _ = passkey_request(
        server, "/lock/auth/complete", "complete", login_cookies, assertion,
    )
    assert status == 200
    session = cookie_value(headers, server.config.session_cookie)
    assert passkey_request(
        server, "/lock/auth/complete", "complete", login_cookies, assertion,
    )[0] == 403
    current = {"diary_lock_session": session, fresh.TRANSACTION_COOKIE: transaction}
    assert request(server, "GET", "/lock/unlock/return", current)[0] == 200
    assert request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )[0] == 204
    assert request(
        server, "POST", "/lock/unlock/consume", current,
        browser_headers("X-Diary-Unlock-Confirm"),
    )[0] == 403


def test_webauthn_requires_user_verification_and_valid_enrollment_code(
    service: tuple[fresh.FreshUnlockServer, FakeSessionState],
) -> None:
    server, _ = service
    passkey = VirtualPasskey.create()
    assert passkey_request(
        server, "/lock/enroll/options", "options", body={"code": "wrong"},
    )[0] == 403
    status, _, headers, body = passkey_request(
        server, "/lock/enroll/options", "options", body={"code": "test-enrollment-code"},
    )
    ceremony = cookie_value(headers, fresh.CEREMONY_COOKIE)
    options = json.loads(body)["publicKey"]
    assert status == 200
    assert passkey_request(
        server, "/lock/enroll/complete", "complete",
        {fresh.CEREMONY_COOKIE: ceremony}, passkey.registration(options, user_verified=False),
    )[0] == 403
