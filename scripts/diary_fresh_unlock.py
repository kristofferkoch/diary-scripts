#!/usr/bin/python3
"""Browser-bound, forced-login, single-use authorization for the lock UI."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import secrets
import sqlite3
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

DEFAULT_CONFIG = "/etc/diary/lock-auth.toml"
DEFAULT_DATABASE = "/var/lib/diary-lock-auth/transactions.sqlite3"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CONCURRENT_REQUESTS = 16
MAX_LIVE_TRANSACTIONS = 32
BEGIN_RATE_LIMIT = 8
BEGIN_RATE_WINDOW_SECONDS = 60


class AuthorizationError(ValueError):
    """Fresh authorization cannot be established or consumed."""


class RateLimitError(AuthorizationError):
    """The global transaction creation rate is exhausted."""


class CapacityError(AuthorizationError):
    """The bounded service or transaction capacity is exhausted."""


@dataclass(frozen=True)
class Config:
    owner_username: str
    public_origin: str
    authelia_backend: str
    session_cookie: str
    transaction_ttl: int = 300


@dataclass(frozen=True)
class SessionProof:
    session_hash: bytes


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    allowed = {
        "owner_username", "public_origin", "authelia_backend",
        "session_cookie", "transaction_ttl_seconds",
    }
    if set(raw) - allowed:
        raise AuthorizationError("unknown lock authorization configuration")
    required = allowed - {"transaction_ttl_seconds"}
    if not required <= set(raw):
        raise AuthorizationError("incomplete lock authorization configuration")
    values = {key: raw[key] for key in required}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise AuthorizationError("lock authorization settings must be non-empty strings")
    ttl = raw.get("transaction_ttl_seconds", 300)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 300:
        raise AuthorizationError("transaction TTL must be between 30 and 300 seconds")
    origin = urllib.parse.urlsplit(values["public_origin"])
    if origin.scheme != "https" or not origin.hostname or origin.path or origin.query or origin.fragment:
        raise AuthorizationError("public origin must be an HTTPS origin without a path")
    backend = urllib.parse.urlsplit(values["authelia_backend"])
    if (
        backend.scheme != "http" or backend.hostname not in {"127.0.0.1", "localhost"}
        or backend.path or backend.query or backend.fragment or backend.username or backend.password
    ):
        raise AuthorizationError("Authelia backend must be a loopback HTTP origin")
    cookie = values["session_cookie"]
    if not cookie.isascii() or not cookie.replace("_", "a").replace("-", "a").isalnum():
        raise AuthorizationError("invalid Authelia session cookie name")
    return Config(
        owner_username=values["owner_username"],
        public_origin=values["public_origin"].rstrip("/"),
        authelia_backend=values["authelia_backend"].rstrip("/"),
        session_cookie=cookie,
        transaction_ttl=ttl,
    )


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


class CreationRateLimiter:
    def __init__(
        self, limit: int = BEGIN_RATE_LIMIT, window: float = BEGIN_RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.events: list[float] = []
        self.lock = threading.Lock()

    def check(self) -> None:
        now = self.clock()
        with self.lock:
            self.events = [event for event in self.events if event > now - self.window]
            if len(self.events) >= self.limit:
                raise RateLimitError("global transaction creation rate exceeded")
            self.events.append(now)


class TransactionStore:
    def __init__(
        self, path: str | Path, clock: Callable[[], float] = time.time,
        max_live: int = MAX_LIVE_TRANSACTIONS,
    ):
        self.path = str(path)
        self.clock = clock
        self.max_live = max_live
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    secret_hash BLOB NOT NULL,
                    owner TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'fresh', 'consumed')),
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    authenticated_at INTEGER,
                    consumed_at INTEGER,
                    session_hash BLOB
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
            if "session_hash" not in columns:
                connection.execute("ALTER TABLE transactions ADD COLUMN session_hash BLOB")
                connection.execute("DELETE FROM transactions")

    def create(self, owner: str, ttl: int) -> tuple[str, str]:
        transaction_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM transactions WHERE expires_at < ?", (now,))
            live = connection.execute(
                "SELECT count(*) FROM transactions "
                "WHERE state IN ('pending', 'fresh') AND expires_at >= ?",
                (now,),
            ).fetchone()[0]
            if live >= self.max_live:
                connection.execute("ROLLBACK")
                raise CapacityError("live transaction capacity exceeded")
            connection.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL)",
                (transaction_id, _digest(secret), owner, now, now + ttl),
            )
        return transaction_id, secret

    def binding(
        self, transaction_id: str, secret: str, owner: str, expected_state: str,
    ) -> tuple[int, bytes | None]:
        now = int(self.clock())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret_hash, owner, state, expires_at, created_at, session_hash "
                "FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
        if (
            row is None
            or not hmac.compare_digest(row[0], _digest(secret))
            or not hmac.compare_digest(row[1], owner)
            or row[2] != expected_state
            or row[3] < now
        ):
            raise AuthorizationError("invalid, expired, or already used transaction")
        return row[4], row[5]

    def authenticate(self, transaction_id: str, secret: str, owner: str, session_hash: bytes) -> None:
        self._transition(transaction_id, secret, owner, "pending", "fresh", "authenticated_at", session_hash)

    def consume(self, transaction_id: str, secret: str, owner: str, session_hash: bytes) -> None:
        self._transition(transaction_id, secret, owner, "fresh", "consumed", "consumed_at", session_hash)

    def _transition(
        self, transaction_id: str, secret: str, owner: str,
        previous: str, following: str, timestamp_column: str, session_hash: bytes,
    ) -> None:
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT secret_hash, owner, state, expires_at, session_hash FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            valid = (
                row is not None
                and hmac.compare_digest(row[0], _digest(secret))
                and hmac.compare_digest(row[1], owner)
                and row[2] == previous
                and row[3] >= now
                and (previous == "pending" or (row[4] is not None and hmac.compare_digest(row[4], session_hash)))
            )
            if not valid:
                connection.execute("ROLLBACK")
                raise AuthorizationError("invalid, expired, already used, or session-mismatched transaction")
            cursor = connection.execute(
                f"UPDATE transactions SET state = ?, {timestamp_column} = ?, session_hash = ? "
                "WHERE id = ? AND state = ?",
                (following, now, session_hash, transaction_id, previous),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise AuthorizationError("transaction changed concurrently")


class AutheliaClient:
    def __init__(self, config: Config):
        self.config = config
        self.backend = urllib.parse.urlsplit(config.authelia_backend)
        self.public = urllib.parse.urlsplit(config.public_origin)

    def _request(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.backend.hostname or "", self.backend.port or 80, timeout=5)
        try:
            connection.request(method, path, headers=headers, body=body)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise AuthorizationError("Authelia response is too large")
            values = {key.lower(): value for key, value in response.getheaders()}
            return response.status, values, response_body
        except (OSError, http.client.HTTPException) as exc:
            raise AuthorizationError("Authelia is unavailable") from exc
        finally:
            connection.close()

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorizationError("invalid Authelia JSON response") from exc
        if not isinstance(value, dict):
            raise AuthorizationError("invalid Authelia JSON response")
        return value

    def _session_value(self, cookie_header: str | None) -> str:
        if not cookie_header:
            return ""
        return _cookies(cookie_header).get(self.config.session_cookie, "")

    def _forward_auth(self, session: str, method: str, uri: str) -> tuple[int, dict[str, str]]:
        headers = {
            "X-Forwarded-Method": method,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": self.public.netloc,
            "X-Forwarded-URI": uri,
        }
        if session:
            headers["Cookie"] = f"{self.config.session_cookie}={session}"
        query = urllib.parse.urlencode({"authelia_url": f"{self.config.public_origin}/authelia/"})
        status, response_headers, _ = self._request(
            "GET", f"/api/authz/forward-auth?{query}", headers,
        )
        return status, response_headers

    def logout(self, cookie_header: str | None) -> None:
        old_session = self._session_value(cookie_header)
        headers = {
            "Host": self.public.netloc,
            "X-Forwarded-Proto": "https",
            "Content-Type": "application/json",
        }
        if old_session:
            headers["Cookie"] = f"{self.config.session_cookie}={old_session}"
        status, _, body = self._request("POST", "/authelia/api/logout", headers, b"{}")
        response = self._json(body)
        data = response.get("data")
        if (
            status != 200 or response.get("status") != "OK" or not isinstance(data, dict)
            or not isinstance(data.get("safeTargetURL"), bool)
        ):
            raise AuthorizationError("Authelia session invalidation failed")
        if old_session:
            old_status, _ = self._forward_auth(old_session, "GET", "/lock/auth-check")
            if old_status == 200:
                raise AuthorizationError("Authelia session remains valid after logout")

    def session_proof(self, cookie_header: str | None, created_at: int, method: str, uri: str) -> SessionProof:
        session = self._session_value(cookie_header)
        if not session:
            raise AuthorizationError("an authenticated session is required")
        status, response_headers = self._forward_auth(session, method, uri)
        groups = {item.strip() for item in response_headers.get("remote-groups", "").split(",")}
        if (
            status != 200
            or response_headers.get("remote-user") != self.config.owner_username
            or "lock-owner" not in groups
        ):
            raise AuthorizationError("an owner two-factor session is required")
        headers = {
            "Host": self.public.netloc,
            "X-Forwarded-Proto": "https",
            "Cookie": f"{self.config.session_cookie}={session}",
        }
        state_status, _, body = self._request("GET", "/authelia/api/state", headers)
        response = self._json(body)
        state = response.get("data")
        if state_status != 200 or response.get("status") != "OK" or not isinstance(state, dict):
            raise AuthorizationError("trusted session proof is unavailable")
        first = state.get("first_factor_authn_timestamp")
        second = state.get("second_factor_authn_timestamp")
        if (
            state.get("username") != self.config.owner_username
            or state.get("authentication_level") != 2
            or state.get("factor_knowledge") is not True
            or state.get("username_and_password") is not True
            or state.get("webauthn") is not True
            or state.get("webauthn_user_verified") is not True
            or isinstance(first, bool) or not isinstance(first, int)
            or isinstance(second, bool) or not isinstance(second, int)
            or first <= created_at or second <= created_at
        ):
            raise AuthorizationError("password and user-verified WebAuthn must postdate the transaction")
        return SessionProof(session_hash=_digest(session))


def _cookies(header: str) -> dict[str, str]:
    parsed = SimpleCookie()
    try:
        parsed.load(header)
    except CookieError as exc:
        raise AuthorizationError("invalid cookies") from exc
    return {key: morsel.value for key, morsel in parsed.items()}


def _transaction_cookie(header: str | None) -> tuple[str, str]:
    if not header:
        raise AuthorizationError("transaction cookie is required")
    value = _cookies(header).get(TRANSACTION_COOKIE, "")
    parts = value.split(".")
    if len(parts) != 2 or not all(parts):
        raise AuthorizationError("invalid transaction cookie")
    return parts[0], parts[1]


class FreshUnlockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: Config, database: str | Path):
        self.config = config
        self.authelia = AutheliaClient(config)
        self.transactions = TransactionStore(database)
        self.begin_rate = CreationRateLimiter()
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, FreshUnlockHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                    b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class FreshUnlockHandler(BaseHTTPRequestHandler):
    server: FreshUnlockServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _json(self, status: int, value: dict[str, str]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("ascii")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_same_origin(self, confirmation_header: str) -> None:
        if self.headers.get("Origin") != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        if self.headers.get(confirmation_header) != "yes":
            raise AuthorizationError("explicit browser action required")
        if self.headers.get("Content-Length", "0") != "0":
            raise AuthorizationError("request body is not accepted")

    def _clear_transaction_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{TRANSACTION_COOKIE}=; Max-Age=0; Path=/lock/unlock; Secure; HttpOnly; SameSite=Strict",
        )

    def _begin(self) -> None:
        self._require_same_origin("X-Diary-Unlock-Begin")
        self.server.begin_rate.check()
        self.server.authelia.logout(self.headers.get("Cookie"))
        transaction_id, secret = self.server.transactions.create(
            self.server.config.owner_username, self.server.config.transaction_ttl,
        )
        return_url = f"{self.server.config.public_origin}/lock/unlock/return"
        location = f"{self.server.config.public_origin}/authelia/?{urllib.parse.urlencode({'rd': return_url})}"
        self.send_response(303)
        self._common_headers()
        self.send_header("Location", location)
        self.send_header(
            "Set-Cookie",
            f"{self.server.config.session_cookie}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header(
            "Set-Cookie",
            f"{TRANSACTION_COOKIE}={transaction_id}.{secret}; Max-Age={self.server.config.transaction_ttl}; "
            "Path=/lock/unlock; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _return(self) -> None:
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        created_at, _ = self.server.transactions.binding(
            transaction_id, secret, self.server.config.owner_username, "pending",
        )
        proof = self.server.authelia.session_proof(self.headers.get("Cookie"), created_at, "GET", self.path)
        self.server.transactions.authenticate(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self._json(200, {"status": "fresh"})

    def _consume(self) -> None:
        self._require_same_origin("X-Diary-Unlock-Confirm")
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        created_at, session_hash = self.server.transactions.binding(
            transaction_id, secret, self.server.config.owner_username, "fresh",
        )
        if session_hash is None:
            raise AuthorizationError("transaction has no authenticated session binding")
        proof = self.server.authelia.session_proof(self.headers.get("Cookie"), created_at, "POST", self.path)
        self.server.transactions.consume(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self.send_response(204)
        self._common_headers()
        self._clear_transaction_cookie()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method: str) -> None:
        try:
            if method == "GET" and self.path == "/lock/health":
                self._json(200, {"status": "ok"})
            elif method == "GET" and self.path == "/lock/unlock/return":
                self._return()
            elif method == "POST" and self.path == "/lock/unlock/begin":
                self._begin()
            elif method == "POST" and self.path == "/lock/unlock/consume":
                self._consume()
            else:
                self._json(404, {"error": "not found"})
        except RateLimitError:
            self._json(429, {"error": "transaction creation rate exceeded"})
        except CapacityError:
            self._json(503, {"error": "transaction capacity exceeded"})
        except AuthorizationError:
            self._json(403, {"error": "fresh authorization required"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9092)
    args = parser.parse_args()
    config = load_config(args.config)
    server = FreshUnlockServer((args.listen, args.port), config, args.database)
    server.serve_forever()


if __name__ == "__main__":
    main()
