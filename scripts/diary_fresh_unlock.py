#!/usr/bin/python3
"""Browser-bound, forced-login, single-use authorization for the lock UI."""
from __future__ import annotations

import argparse
import grp
import hashlib
import hmac
import html
import http.client
import json
import os
import pwd
import re
import secrets
import socket
import socketserver
import sqlite3
import stat
import struct
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
MERGE_COOKIE_PATH = "/lock/merge"
UNLOCK_COOKIE_PATH = "/lock/unlock"
REPOS = ("scripts", "diary")
PURPOSES = frozenset({"unlock", "merge"})
MERGE_ACTIONS = frozenset({"merge", "deny"})
OID_RE = re.compile(r"[0-9a-f]{40}\Z")
MAX_VERIFY_BYTES = 4096
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
                    session_hash BLOB,
                    purpose TEXT NOT NULL DEFAULT 'unlock',
                    action TEXT,
                    queue_oid TEXT,
                    base_oid TEXT,
                    repo TEXT NOT NULL DEFAULT 'scripts' 
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
            if "session_hash" not in columns:
                connection.execute("ALTER TABLE transactions ADD COLUMN session_hash BLOB")
                connection.execute("DELETE FROM transactions")
            if "repo" not in columns:
                # Existing rows predate the diary queue and were all scripts.
                connection.execute(
                    "ALTER TABLE transactions ADD COLUMN repo TEXT NOT NULL DEFAULT 'scripts'"
                )
            for column in ("purpose", "action", "queue_oid", "base_oid"):
                if column not in columns:
                    default = " NOT NULL DEFAULT 'unlock'" if column == "purpose" else ""
                    connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} TEXT{default}")

    def create(
        self, owner: str, ttl: int, *, purpose: str = "unlock",
        action: str | None = None, queue_oid: str | None = None, base_oid: str | None = None,
        repo: str | None = None,
    ) -> tuple[str, str]:
        if purpose not in PURPOSES:
            raise AuthorizationError("unknown authorization purpose")
        if purpose == "merge" and (
            action not in MERGE_ACTIONS
            or repo not in REPOS
            or not OID_RE.fullmatch(queue_oid or "")
            or not OID_RE.fullmatch(base_oid or "")
        ):
            raise AuthorizationError(
                "merge authorization requires an action, repository, and full queue/base OIDs"
            )
        if purpose == "unlock" and any(
            value is not None for value in (action, queue_oid, base_oid, repo)
        ):
            raise AuthorizationError("unlock authorization cannot carry merge bindings")
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
                "INSERT INTO transactions "
                "(id, secret_hash, owner, state, created_at, expires_at, purpose, action, queue_oid, base_oid, repo) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (transaction_id, _digest(secret), owner, now, now + ttl, purpose, action,
                 queue_oid, base_oid, repo or "scripts"),
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

    def details(
        self, transaction_id: str, secret: str, owner: str, expected_state: str,
    ) -> dict[str, Any]:
        now = int(self.clock())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret_hash, owner, state, expires_at, created_at, session_hash, "
                "purpose, action, queue_oid, base_oid, repo FROM transactions WHERE id = ?",
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
        return {
            "created_at": row[4], "session_hash": row[5], "purpose": row[6],
            "action": row[7], "queue_oid": row[8], "base_oid": row[9], "repo": row[10],
        }

    def verify_consumed(
        self, transaction_id: str, repo: str, action: str, queue_oid: str, base_oid: str,
    ) -> dict[str, Any] | None:
        """Answer only whether this exact merge grant was consumed, never grant."""
        if not isinstance(transaction_id, str) or action not in MERGE_ACTIONS:
            return None
        if repo not in REPOS:
            return None
        if not OID_RE.fullmatch(queue_oid) or not OID_RE.fullmatch(base_oid):
            return None
        now = int(self.clock())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state, expires_at, purpose, action, queue_oid, base_oid, consumed_at, repo "
                "FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
        if (
            row is None or row[0] != "consumed" or row[1] < now or row[2] != "merge"
            or row[3] != action
            or not hmac.compare_digest(row[4] or "", queue_oid)
            or not hmac.compare_digest(row[5] or "", base_oid)
            or row[6] is None
            or row[7] != repo
        ):
            return None
        return {"consumed_at": row[6]}

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

    def verify_owner(self, cookie_header: str | None, method: str, uri: str) -> None:
        """Require an owner two-factor session before creating a transaction."""
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
            if old_status not in {302, 401}:
                raise AuthorizationError("Authelia session invalidation could not be confirmed")

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


def _confirmation_page(action: str, queue_oid: str, base_oid: str, repo: str) -> str:
    """Show the decision the ceremony was bound to, on a page the UI cannot forge.

    The console is an untrusted convenience layer; this page is served by the
    root-owned authorization service and names the action and both OIDs that were
    stored when the ceremony began. Without it the owner would only ever see what
    the console chose to display.
    """
    verb = "Merge and publish" if action == "merge" else "Deny (record only, no publication)"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'">
<title>Confirm the authorized decision</title>
<style>body{{font-family:sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}code{{overflow-wrap:anywhere}}</style>
</head>
<body>
<h1>Confirm the authorized decision</h1>
<p>Your password and user-verified WebAuthn are recorded. This page is served by
the root-owned authorization service, which bound the ceremony to exactly:</p>
<dl>
<dt>Action</dt><dd><strong>{html.escape(verb)}</strong></dd>
<dt>Queue</dt><dd><code>{html.escape(repo)}</code></dd>
<dt>Reviewed queue OID</dt><dd><code>{html.escape(queue_oid)}</code></dd>
<dt>Approved base OID</dt><dd><code>{html.escape(base_oid)}</code></dd>
</dl>
<p>If that is not the decision you reviewed, close this page: nothing is merged
or denied until the console confirms, and the grant expires on its own.</p>
<p><a href="/lock/merge/">Continue to the merge review</a></p>
</body>
</html>
"""


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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        )
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
        # Browsers are inconsistent: some omit Origin and Firefox sends the
        # opaque value "null" on a same-origin form post. Only a concrete
        # mismatching origin is refused; Sec-Fetch-Site plus the session-bound
        # transaction do the authorization.
        origin = self.headers.get("Origin")
        if origin not in (None, "null") and origin != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        if self.headers.get(confirmation_header) != "yes":
            raise AuthorizationError("explicit browser action required")
        if self.headers.get("Content-Length", "0") != "0":
            raise AuthorizationError("request body is not accepted")

    def _clear_transaction_cookie(self, path: str = UNLOCK_COOKIE_PATH) -> None:
        self.send_header(
            "Set-Cookie",
            f"{TRANSACTION_COOKIE}=; Max-Age=0; Path={path}; Secure; HttpOnly; SameSite=Strict",
        )

    def _begin_action(self, header: str) -> dict[str, Any]:
        """Read one authorization request: the JSON API call or the console form.

        The console page offers a plain form so the owner needs no script; the
        JSON branch keeps the explicit browser-action header. Both require the
        exact public origin and a same-site request, and both are only accepted
        together with a live owner session by the caller.
        """
        # Browsers are inconsistent: some omit Origin and Firefox sends the
        # opaque value "null" on a same-origin form post. Only a concrete
        # mismatching origin is refused; Sec-Fetch-Site plus the session-bound
        # transaction do the authorization.
        origin = self.headers.get("Origin")
        if origin not in (None, "null") and origin != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise AuthorizationError("valid Content-Length required") from exc
        if not 0 < length <= MAX_VERIFY_BYTES:
            raise AuthorizationError("request body is missing or oversized")
        raw = self.rfile.read(length)
        if content_type == "application/json":
            if self.headers.get(header) != "yes":
                raise AuthorizationError("explicit browser action required")
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuthorizationError("invalid JSON request") from exc
            if not isinstance(value, dict):
                raise AuthorizationError("JSON request must be an object")
            return value
        if content_type == "application/x-www-form-urlencoded":
            try:
                parsed = urllib.parse.parse_qs(raw.decode("utf-8", "strict"), strict_parsing=True)
            except (UnicodeDecodeError, ValueError) as exc:
                raise AuthorizationError("invalid form request") from exc
            return {key: values[0] for key, values in parsed.items() if len(values) == 1}
        raise AuthorizationError("unsupported request body")

    def _html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
        details = self.server.transactions.details(
            transaction_id, secret, self.server.config.owner_username, "pending",
        )
        if details["purpose"] != "unlock":
            raise AuthorizationError("transaction is not an unlock authorization")
        created_at = details["created_at"]
        proof = self.server.authelia.session_proof(self.headers.get("Cookie"), created_at, "GET", self.path)
        self.server.transactions.authenticate(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self._json(200, {"status": "fresh"})

    def _consume(self) -> None:
        self._require_same_origin("X-Diary-Unlock-Confirm")
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        details = self.server.transactions.details(
            transaction_id, secret, self.server.config.owner_username, "fresh",
        )
        if details["purpose"] != "unlock":
            raise AuthorizationError("transaction is not an unlock authorization")
        created_at, session_hash = details["created_at"], details["session_hash"]
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

    def _merge_begin(self) -> None:
        body = self._begin_action("X-Diary-Merge-Begin")
        if set(body) != {"action", "queue_oid", "base_oid", "repo"}:
            raise AuthorizationError("invalid merge authorization request")
        repo = body["repo"]
        if repo not in REPOS:
            raise AuthorizationError("merge authorization repository must be scripts or diary")
        action = body["action"]
        if action not in MERGE_ACTIONS:
            raise AuthorizationError("merge authorization action must be merge or deny")
        queue_oid, base_oid = body["queue_oid"], body["base_oid"]
        if not isinstance(queue_oid, str) or not isinstance(base_oid, str):
            raise AuthorizationError("merge authorization requires full queue and base OIDs")
        # Only an already-authenticated owner may start a ceremony. Without this
        # an unauthenticated caller could fill the bounded live-transaction table
        # and block the owner's only merge path.
        self.server.authelia.verify_owner(self.headers.get("Cookie"), "POST", self.path)
        self.server.begin_rate.check()
        self.server.authelia.logout(self.headers.get("Cookie"))
        transaction_id, secret = self.server.transactions.create(
            self.server.config.owner_username, self.server.config.transaction_ttl,
            purpose="merge", action=action, queue_oid=queue_oid, base_oid=base_oid, repo=repo,
        )
        return_url = f"{self.server.config.public_origin}/lock/merge/return"
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
            f"Path={MERGE_COOKIE_PATH}; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _merge_return(self) -> None:
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        details = self.server.transactions.details(
            transaction_id, secret, self.server.config.owner_username, "pending",
        )
        if details["purpose"] != "merge":
            raise AuthorizationError("transaction is not a merge authorization")
        proof = self.server.authelia.session_proof(
            self.headers.get("Cookie"), details["created_at"], "GET", self.path,
        )
        self.server.transactions.authenticate(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self._html(
            200,
            _confirmation_page(
                details["action"], details["queue_oid"], details["base_oid"], details["repo"],
            ),
        )

    def _merge_consume(self) -> None:
        self._require_same_origin("X-Diary-Merge-Confirm")
        transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
        details = self.server.transactions.details(
            transaction_id, secret, self.server.config.owner_username, "fresh",
        )
        if details["purpose"] != "merge":
            raise AuthorizationError("transaction is not a merge authorization")
        if self.headers.get("X-Diary-Merge-Repo") != details["repo"]:
            raise AuthorizationError("merge authorization repository does not match")
        if details["session_hash"] is None:
            raise AuthorizationError("transaction has no authenticated session binding")
        proof = self.server.authelia.session_proof(
            self.headers.get("Cookie"), details["created_at"], "POST", self.path,
        )
        self.server.transactions.consume(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self.send_response(204)
        self._common_headers()
        self._clear_transaction_cookie(MERGE_COOKIE_PATH)
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
            elif method == "POST" and self.path == "/lock/merge/begin":
                self._merge_begin()
            elif method == "GET" and self.path == "/lock/merge/return":
                self._merge_return()
            elif method == "POST" and self.path == "/lock/merge/consume":
                self._merge_consume()
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



def peer_uid(connection: socket.socket) -> int:
    """Read the kernel-provided peer UID of a connected Unix socket."""
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


class MergeGrantVerifyHandler(socketserver.StreamRequestHandler):
    """Answer one metadata question: was this exact merge grant consumed?"""

    server: MergeGrantVerifyServer

    def _reply(self, value: dict[str, Any]) -> None:
        try:
            self.wfile.write(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
            self.wfile.flush()
        except OSError:
            pass

    def handle(self) -> None:
        try:
            uid = peer_uid(self.connection)
        except OSError:
            return
        if uid not in self.server.allowed_uids:
            self._reply({"ok": False, "error": "caller is not authorized"})
            return
        line = self.rfile.readline(MAX_VERIFY_BYTES + 1)
        if not line or not line.endswith(b"\n") or len(line) > MAX_VERIFY_BYTES:
            self._reply({"ok": False, "error": "invalid request"})
            return
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply({"ok": False, "error": "invalid request"})
            return
        if (
            not isinstance(value, dict)
            or set(value) != {"cmd", "repository", "transaction", "action", "queue_oid", "base_oid"}
            or value.get("cmd") != "verify"
            or not all(
                isinstance(value[key], str)
                for key in ("repository", "transaction", "action", "queue_oid", "base_oid")
            )
        ):
            self._reply({"ok": False, "error": "invalid request"})
            return
        result = self.server.transactions.verify_consumed(
            value["transaction"], value["repository"], value["action"],
            value["queue_oid"], value["base_oid"],
        )
        if result is None:
            self._reply({"ok": False, "error": "no consumed grant for this action and OID pair"})
            return
        self._reply({"ok": True, **result})


class MergeGrantVerifyServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Root-owned verifier socket; only the merge executor identity may connect."""

    daemon_threads = True

    def __init__(self, path: str, store: TransactionStore, allowed_uids: set[int]) -> None:
        self.transactions = store
        self.allowed_uids = frozenset(allowed_uids)
        super().__init__(path, MergeGrantVerifyHandler)


def discard_stale_socket(path: str) -> None:
    """Remove a socket left by a killed instance before binding again.

    A Unix socket path is not removed when its process dies, so a restart after
    a crash fails to bind with EADDRINUSE and the whole service crash-loops.
    Only a socket that nobody is listening on may be removed; a live listener or
    a non-socket at that path is an error.
    """
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"refusing to replace a non-socket at {target}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(target))
    except OSError:
        target.unlink(missing_ok=True)
    else:
        raise RuntimeError(f"another verifier is already listening on {target}")
    finally:
        probe.close()


def start_verify_server(
    path: str, store: TransactionStore, user: str,
) -> MergeGrantVerifyServer:
    directory = Path(path).parent
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o750)
    discard_stale_socket(path)
    account = pwd.getpwnam(user)
    server = MergeGrantVerifyServer(path, store, {account.pw_uid})
    os.chmod(path, 0o660)
    try:
        os.chown(path, 0, account.pw_gid)
    except PermissionError:
        # The service may run without CAP_CHOWN; the socket then inherits the
        # private group from a setgid runtime directory (root:kek 2770).
        if Path(path).stat().st_gid != account.pw_gid:
            raise
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--verify-socket", default="/run/diary-fresh-unlock/verify.sock")
    parser.add_argument("--verify-user", default="kek")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9092)
    args = parser.parse_args()
    config = load_config(args.config)
    server = FreshUnlockServer((args.listen, args.port), config, args.database)
    if args.verify_socket:
        start_verify_server(args.verify_socket, server.transactions, args.verify_user)
    server.serve_forever()


if __name__ == "__main__":
    main()
