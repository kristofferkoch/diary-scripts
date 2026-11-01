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


class AuthorizationError(ValueError):
    """Fresh authorization cannot be established or consumed."""


@dataclass(frozen=True)
class Config:
    owner_username: str
    public_origin: str
    authelia_backend: str
    session_cookie: str
    transaction_ttl: int = 300


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


class TransactionStore:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = str(path)
        self.clock = clock
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
                    consumed_at INTEGER
                )
                """
            )

    def create(self, owner: str, ttl: int) -> tuple[str, str]:
        transaction_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM transactions WHERE expires_at < ?", (now - 86400,))
            connection.execute(
                "INSERT INTO transactions VALUES (?, ?, ?, 'pending', ?, ?, NULL, NULL)",
                (transaction_id, _digest(secret), owner, now, now + ttl),
            )
        return transaction_id, secret

    def authenticate(self, transaction_id: str, secret: str, owner: str) -> None:
        self._transition(transaction_id, secret, owner, "pending", "fresh", "authenticated_at")

    def consume(self, transaction_id: str, secret: str, owner: str) -> None:
        self._transition(transaction_id, secret, owner, "fresh", "consumed", "consumed_at")

    def _transition(
        self, transaction_id: str, secret: str, owner: str,
        previous: str, following: str, timestamp_column: str,
    ) -> None:
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT secret_hash, owner, state, expires_at FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            valid = (
                row is not None
                and hmac.compare_digest(row[0], _digest(secret))
                and hmac.compare_digest(row[1], owner)
                and row[2] == previous
                and row[3] >= now
            )
            if not valid:
                connection.execute("ROLLBACK")
                raise AuthorizationError("invalid, expired, or already used transaction")
            cursor = connection.execute(
                f"UPDATE transactions SET state = ?, {timestamp_column} = ? "
                "WHERE id = ? AND state = ?",
                (following, now, transaction_id, previous),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise AuthorizationError("transaction changed concurrently")


class AutheliaClient:
    def __init__(self, config: Config):
        self.config = config
        self.backend = urllib.parse.urlsplit(config.authelia_backend)
        self.public = urllib.parse.urlsplit(config.public_origin)

    def _request(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
        connection = http.client.HTTPConnection(self.backend.hostname or "", self.backend.port or 80, timeout=5)
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AuthorizationError("Authelia response is too large")
            values = {key.lower(): value for key, value in response.getheaders()}
            return response.status, values
        except (OSError, http.client.HTTPException) as exc:
            raise AuthorizationError("Authelia is unavailable") from exc
        finally:
            connection.close()

    def _session(self, cookie_header: str | None) -> str:
        if not cookie_header:
            return ""
        cookies = _cookies(cookie_header)
        value = cookies.get(self.config.session_cookie)
        return f"{self.config.session_cookie}={value}" if value else ""

    def logout(self, cookie_header: str | None) -> None:
        headers = {
            "Host": self.public.netloc,
            "X-Forwarded-Proto": "https",
            "Content-Length": "0",
        }
        session = self._session(cookie_header)
        if session:
            headers["Cookie"] = session
        status, _ = self._request("POST", "/authelia/api/logout", headers)
        if status != 200:
            raise AuthorizationError("Authelia session invalidation failed")

    def require_owner(self, cookie_header: str | None, method: str, uri: str) -> None:
        headers = {
            "X-Forwarded-Method": method,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": self.public.netloc,
            "X-Forwarded-URI": uri,
        }
        session = self._session(cookie_header)
        if session:
            headers["Cookie"] = session
        query = urllib.parse.urlencode({"authelia_url": f"{self.config.public_origin}/authelia/"})
        status, response_headers = self._request(
            "GET", f"/api/authz/forward-auth?{query}", headers,
        )
        groups = {item.strip() for item in response_headers.get("remote-groups", "").split(",")}
        if (
            status != 200
            or response_headers.get("remote-user") != self.config.owner_username
            or "lock-owner" not in groups
        ):
            raise AuthorizationError("a fresh owner two-factor session is required")


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
        super().__init__(address, FreshUnlockHandler)


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

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        self.server.authelia.require_owner(self.headers.get("Cookie"), "GET", self.path)
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        self.server.transactions.authenticate(transaction_id, secret, self.server.config.owner_username)
        self._json(200, {"status": "fresh"})

    def _consume(self) -> None:
        self._require_same_origin("X-Diary-Unlock-Confirm")
        self.server.authelia.require_owner(self.headers.get("Cookie"), "POST", self.path)
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        self.server.transactions.consume(transaction_id, secret, self.server.config.owner_username)
        self.send_response(204)
        self._common_headers()
        self._clear_transaction_cookie()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            if self.path == "/lock/health":
                self._json(200, {"status": "ok"})
            elif self.path == "/lock/unlock/return":
                self._return()
            else:
                self._json(404, {"error": "not found"})
        except AuthorizationError:
            self._json(403, {"error": "fresh authorization required"})

    def do_POST(self) -> None:
        try:
            if self.path == "/lock/unlock/begin":
                self._begin()
            elif self.path == "/lock/unlock/consume":
                self._consume()
            else:
                self._json(404, {"error": "not found"})
        except AuthorizationError:
            self._json(403, {"error": "fresh authorization required"})


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
