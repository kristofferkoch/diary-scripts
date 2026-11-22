#!/usr/bin/python3
"""Passkey-only browser sessions and single-use owner authorization."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
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
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

DEFAULT_CONFIG = "/etc/diary/lock-auth.toml"
DEFAULT_DATABASE = "/var/lib/diary-lock-auth/transactions.sqlite3"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
CEREMONY_COOKIE = "diary_lock_passkey_ceremony"
COOKIE_PATH = "/lock"
MERGE_COOKIE_PATH = COOKIE_PATH
UNLOCK_COOKIE_PATH = COOKIE_PATH
REPOS = ("scripts", "diary")
PURPOSES = frozenset({"unlock", "merge"})
MERGE_ACTIONS = frozenset({"merge", "deny"})
OID_RE = re.compile(r"[0-9a-f]{40}\Z")
MAX_VERIFY_BYTES = 4096
MAX_REQUEST_BYTES = 64 * 1024
MAX_CONCURRENT_REQUESTS = 16
MAX_LIVE_TRANSACTIONS = 32
MAX_LIVE_CEREMONIES = 32
BEGIN_RATE_LIMIT = 8
BEGIN_RATE_WINDOW_SECONDS = 60
DEFAULT_ENROLLMENT_CODE_FILE = "/run/credentials/diary-fresh-unlock.service/enrollment_code"


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
    session_cookie: str
    transaction_ttl: int = 300
    session_ttl: int = 600


@dataclass(frozen=True)
class SessionProof:
    session_hash: bytes


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    allowed = {
        "owner_username", "public_origin", "auth_backend", "session_cookie",
        "transaction_ttl_seconds", "session_ttl_seconds",
    }
    required = {"owner_username", "public_origin", "session_cookie"}
    if set(raw) - allowed:
        raise AuthorizationError("unknown lock authorization configuration")
    if not required <= set(raw):
        raise AuthorizationError("incomplete lock authorization configuration")
    values = {key: raw[key] for key in required}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise AuthorizationError("lock authorization settings must be non-empty strings")
    ttl = raw.get("transaction_ttl_seconds", 300)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 300:
        raise AuthorizationError("transaction TTL must be between 30 and 300 seconds")
    session_ttl = raw.get("session_ttl_seconds", 600)
    if (
        isinstance(session_ttl, bool) or not isinstance(session_ttl, int)
        or not ttl < session_ttl <= 3600
    ):
        raise AuthorizationError("session TTL must exceed the transaction TTL and be at most 3600 seconds")
    origin = urllib.parse.urlsplit(values["public_origin"])
    if origin.scheme != "https" or not origin.hostname or origin.path or origin.query or origin.fragment:
        raise AuthorizationError("public origin must be an HTTPS origin without a path")
    cookie = values["session_cookie"]
    if not cookie.isascii() or not cookie.replace("_", "a").replace("-", "a").isalnum():
        raise AuthorizationError("invalid session cookie name")
    return Config(
        owner_username=values["owner_username"],
        public_origin=values["public_origin"].rstrip("/"),
        session_cookie=cookie,
        transaction_ttl=ttl,
        session_ttl=session_ttl,
    )


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


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

    def discard(self, transaction_id: str, secret: str, owner: str) -> None:
        """Drop a reservation the owner will never be asked to present."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM transactions WHERE id = ? AND secret_hash = ? AND owner = ?",
                (transaction_id, _digest(secret), owner),
            )

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


class PasskeyStore:
    def __init__(
        self, path: str | Path, config: Config,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self.config = config
        self.clock = clock
        self.rp_id = urllib.parse.urlsplit(config.public_origin).hostname or ""
        self.user_id = hashlib.sha256(config.owner_username.encode("utf-8")).digest()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS passkey_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS passkey_credentials (
                    id TEXT PRIMARY KEY,
                    public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL
                );
                CREATE TABLE IF NOT EXISTS passkey_sessions (
                    token_hash BLOB PRIMARY KEY,
                    owner TEXT NOT NULL,
                    authenticated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    transaction_id TEXT
                );
                CREATE TABLE IF NOT EXISTS passkey_ceremonies (
                    id TEXT PRIMARY KEY,
                    secret_hash BLOB NOT NULL,
                    challenge BLOB NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('login', 'enroll')),
                    transaction_id TEXT,
                    enrollment_hash BLOB,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS passkey_used_enrollments (
                    digest BLOB PRIMARY KEY,
                    used_at REAL NOT NULL
                );
                """
            )
            schema = connection.execute(
                "SELECT value FROM passkey_metadata WHERE key = 'schema'"
            ).fetchone()
            if schema is None:
                connection.execute("DELETE FROM transactions")
                connection.execute(
                    "INSERT INTO passkey_metadata (key, value) VALUES ('schema', '1')"
                )
            elif schema[0] != "1":
                raise AuthorizationError("unsupported passkey state schema")

    def _new_ceremony(
        self, kind: str, transaction_id: str | None = None,
        enrollment_hash: bytes | None = None,
    ) -> tuple[str, str, bytes]:
        ceremony_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32)
        challenge = secrets.token_bytes(32)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM passkey_ceremonies WHERE expires_at < ?", (now,))
            live = connection.execute("SELECT count(*) FROM passkey_ceremonies").fetchone()[0]
            if live >= MAX_LIVE_CEREMONIES:
                connection.execute("ROLLBACK")
                raise CapacityError("passkey ceremony capacity exceeded")
            connection.execute(
                "INSERT INTO passkey_ceremonies "
                "(id, secret_hash, challenge, kind, transaction_id, enrollment_hash, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ceremony_id, _digest(secret), challenge, kind, transaction_id,
                    enrollment_hash, now + self.config.transaction_ttl,
                ),
            )
        return ceremony_id, secret, challenge

    def authentication_options(
        self, transaction_id: str | None = None,
    ) -> tuple[str, str, str]:
        ceremony_id, secret, challenge = self._new_ceremony("login", transaction_id)
        options = generate_authentication_options(
            rp_id=self.rp_id,
            challenge=challenge,
            timeout=self.config.transaction_ttl * 1000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return ceremony_id, secret, options_to_json(options)

    def registration_options(
        self, enrollment_code: str, configured_hash: bytes | None,
    ) -> tuple[str, str, str]:
        if not enrollment_code or configured_hash is None:
            raise AuthorizationError("passkey enrollment is not enabled")
        enrollment_hash = _digest(enrollment_code)
        if not hmac.compare_digest(enrollment_hash, configured_hash):
            raise AuthorizationError("invalid enrollment code")
        with self._connect() as connection:
            used = connection.execute(
                "SELECT 1 FROM passkey_used_enrollments WHERE digest = ?", (enrollment_hash,)
            ).fetchone()
            if used:
                raise AuthorizationError("enrollment code has already been used")
            existing = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row[0]))
                for row in connection.execute("SELECT id FROM passkey_credentials")
            ]
        ceremony_id, secret, challenge = self._new_ceremony(
            "enroll", enrollment_hash=enrollment_hash,
        )
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="Diary owner authorization",
            user_name=self.config.owner_username,
            user_display_name="Diary owner",
            user_id=self.user_id,
            challenge=challenge,
            timeout=self.config.transaction_ttl * 1000,
            exclude_credentials=existing,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return ceremony_id, secret, options_to_json(options)

    def _pop_ceremony(self, cookie_header: str | None, kind: str) -> dict[str, Any]:
        value = _cookies(cookie_header).get(CEREMONY_COOKIE, "")
        parts = value.split(".")
        if len(parts) != 2 or not all(parts):
            raise AuthorizationError("passkey ceremony cookie is required")
        ceremony_id, secret = parts
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT secret_hash, challenge, kind, transaction_id, enrollment_hash, expires_at "
                "FROM passkey_ceremonies WHERE id = ?", (ceremony_id,)
            ).fetchone()
            valid = (
                row is not None and row[2] == kind and row[5] >= now
                and hmac.compare_digest(row[0], _digest(secret))
            )
            if not valid:
                connection.execute("ROLLBACK")
                raise AuthorizationError("invalid, expired, or already used passkey ceremony")
            connection.execute("DELETE FROM passkey_ceremonies WHERE id = ?", (ceremony_id,))
        return {
            "challenge": row[1], "transaction_id": row[3], "enrollment_hash": row[4],
        }

    def _create_session(
        self, connection: sqlite3.Connection, transaction_id: str | None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        connection.execute("DELETE FROM passkey_sessions WHERE expires_at < ?", (now,))
        connection.execute(
            "INSERT INTO passkey_sessions "
            "(token_hash, owner, authenticated_at, expires_at, transaction_id) VALUES (?, ?, ?, ?, ?)",
            (
                _digest(token), self.config.owner_username, now,
                now + self.config.session_ttl, transaction_id,
            ),
        )
        return token

    def register(self, cookie_header: str | None, credential: dict[str, Any]) -> str:
        ceremony = self._pop_ceremony(cookie_header, "enroll")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=ceremony["challenge"],
                expected_rp_id=self.rp_id,
                expected_origin=self.config.public_origin,
                require_user_verification=True,
            )
        except (InvalidRegistrationResponse, ValueError, TypeError, KeyError) as exc:
            raise AuthorizationError("passkey registration was not accepted") from exc
        credential_id = bytes_to_base64url(verified.credential_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = connection.execute(
                "SELECT 1 FROM passkey_used_enrollments WHERE digest = ?",
                (ceremony["enrollment_hash"],),
            ).fetchone()
            if used:
                connection.execute("ROLLBACK")
                raise AuthorizationError("enrollment code has already been used")
            try:
                connection.execute(
                    "INSERT INTO passkey_credentials (id, public_key, sign_count, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        credential_id, verified.credential_public_key,
                        verified.sign_count, self.clock(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise AuthorizationError("this passkey is already enrolled") from exc
            connection.execute(
                "INSERT INTO passkey_used_enrollments (digest, used_at) VALUES (?, ?)",
                (ceremony["enrollment_hash"], self.clock()),
            )
            return self._create_session(connection, None)

    def authenticate(self, cookie_header: str | None, credential: dict[str, Any]) -> str:
        ceremony = self._pop_ceremony(cookie_header, "login")
        credential_id = credential.get("id")
        if not isinstance(credential_id, str) or not credential_id:
            raise AuthorizationError("passkey assertion has no credential ID")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public_key, sign_count FROM passkey_credentials WHERE id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise AuthorizationError("unknown passkey")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=ceremony["challenge"],
                expected_rp_id=self.rp_id,
                expected_origin=self.config.public_origin,
                credential_public_key=row[0],
                credential_current_sign_count=row[1],
                require_user_verification=True,
            )
        except (InvalidAuthenticationResponse, ValueError, TypeError, KeyError) as exc:
            raise AuthorizationError("passkey assertion was not accepted") from exc
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE passkey_credentials SET sign_count = ?, last_used_at = ? "
                "WHERE id = ? AND sign_count = ?",
                (verified.new_sign_count, self.clock(), credential_id, row[1]),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise AuthorizationError("passkey changed concurrently; retry the ceremony")
            return self._create_session(connection, ceremony["transaction_id"])

    def logout(self, cookie_header: str | None) -> None:
        session = _cookies(cookie_header).get(self.config.session_cookie, "")
        if not session:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM passkey_sessions WHERE token_hash = ?", (_digest(session),))

    def verify_owner(self, cookie_header: str | None) -> None:
        session = _cookies(cookie_header).get(self.config.session_cookie, "")
        if not session:
            raise AuthorizationError("an owner passkey session is required")
        now = self.clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner, expires_at FROM passkey_sessions WHERE token_hash = ?",
                (_digest(session),),
            ).fetchone()
        if (
            row is None or row[1] < now
            or not hmac.compare_digest(row[0], self.config.owner_username)
        ):
            raise AuthorizationError("an owner passkey session is required")

    def session_proof(
        self, cookie_header: str | None, created_at: float, transaction_id: str,
    ) -> SessionProof:
        session = _cookies(cookie_header).get(self.config.session_cookie, "")
        if not session:
            raise AuthorizationError("an owner passkey session is required")
        now = self.clock()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner, authenticated_at, expires_at, transaction_id "
                "FROM passkey_sessions WHERE token_hash = ?", (_digest(session),),
            ).fetchone()
        if (
            row is None or row[1] <= created_at or row[2] < now
            or row[3] != transaction_id
            or not hmac.compare_digest(row[0], self.config.owner_username)
        ):
            raise AuthorizationError("a new passkey ceremony for this exact transaction is required")
        return SessionProof(session_hash=_digest(session))


BROWSER_ENDPOINTS = frozenset({
    ("GET", "/lock/merge/return"),
    ("POST", "/lock/merge/begin"),
    ("GET", "/lock/unlock/return"),
    ("POST", "/lock/unlock/begin"),
})


def render_browser_error(message: str, back_path: str, title: str = "Authorization not accepted") -> str:
    """Small HTML page for a browser-facing failure; JSON is for fetch callers."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}code{{overflow-wrap:anywhere}}.notice{{padding:0.8rem;border-left:4px solid #bf8700;background:#fff8c5;border-radius:4px}}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="notice">{html.escape(message)}</p>
<p>Nothing was merged, denied, unlocked, or extended.</p>
<p><a href="{html.escape(back_path, quote=True)}">Return to the review</a></p>
</body>
</html>
"""


def _confirmation_page(
    action: str, queue_oid: str, base_oid: str, repo: str, ttl_seconds: int,
) -> str:
    """Show the decision the ceremony was bound to, on a page the UI cannot forge.

    The console is an untrusted convenience layer; this page is served by the
    root-owned authorization service and names the action and both OIDs that were
    stored when the ceremony began. Without it the owner would only ever see what
    the console chose to display.
    """
    verb = "Merge and publish" if action == "merge" else "Deny (record only, no publication)"
    minutes = max(1, int(ttl_seconds) // 60)
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
<p>Your fresh user-verified passkey ceremony is recorded. This page is served by
the root-owned authorization service, which bound the ceremony to exactly:</p>
<dl>
<dt>Action</dt><dd><strong>{html.escape(verb)}</strong></dd>
<dt>Queue</dt><dd><code>{html.escape(repo)}</code></dd>
<dt>Reviewed queue OID</dt><dd><code>{html.escape(queue_oid)}</code></dd>
<dt>Approved base OID</dt><dd><code>{html.escape(base_oid)}</code></dd>
</dl>
<p>If that is not the decision you reviewed, close this page: nothing is merged
or denied until the console confirms, and the grant expires on its own after
{minutes} minutes.</p>
<p><a href="/lock/merge/?{urllib.parse.urlencode({'repo': repo})}">Continue to the merge review</a></p>
</body>
</html>
"""


PASSKEY_JS = r"""'use strict';
const button = document.getElementById('passkey-action');
const message = document.getElementById('passkey-message');
const enrollment = document.body.dataset.enrollment === 'yes';
const returnPath = document.body.dataset.returnPath || '/lock/';

function decode(value) {
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
  const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
  return Uint8Array.from(atob(padded), character => character.charCodeAt(0));
}
function encode(value) {
  if (!value) return null;
  const bytes = new Uint8Array(value);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function credentialJSON(credential) {
  const response = credential.response;
  const value = {
    id: credential.id,
    rawId: encode(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {clientDataJSON: encode(response.clientDataJSON)},
  };
  if (enrollment) {
    value.response.attestationObject = encode(response.attestationObject);
  } else {
    value.response.authenticatorData = encode(response.authenticatorData);
    value.response.signature = encode(response.signature);
    value.response.userHandle = encode(response.userHandle);
  }
  return value;
}
async function post(path, header, body) {
  const headers = {'X-Diary-Passkey-Action': header};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method: 'POST', headers, credentials: 'same-origin', cache: 'no-store',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Passkey request failed (HTTP ${response.status}).`);
  return value;
}
button.addEventListener('click', async () => {
  button.disabled = true;
  message.textContent = 'Follow your browser or authenticator prompt.';
  try {
    const prefix = enrollment ? '/lock/enroll' : '/lock/auth';
    const code = enrollment ? document.getElementById('enrollment-code').value : undefined;
    const optionsResponse = await post(`${prefix}/options`, 'options', enrollment ? {code} : undefined);
    const options = optionsResponse.publicKey;
    options.challenge = decode(options.challenge);
    if (enrollment) {
      options.user.id = decode(options.user.id);
      for (const credential of options.excludeCredentials || []) credential.id = decode(credential.id);
    } else {
      for (const credential of options.allowCredentials || []) credential.id = decode(credential.id);
    }
    const credential = enrollment
      ? await navigator.credentials.create({publicKey: options})
      : await navigator.credentials.get({publicKey: options});
    if (!credential) throw new Error('No passkey was returned.');
    await post(`${prefix}/complete`, 'complete', credentialJSON(credential));
    window.location.assign(enrollment ? '/lock/' : returnPath);
  } catch (error) {
    message.textContent = error.message;
    button.disabled = false;
  }
});
"""


def _passkey_page(return_path: str, details: dict[str, Any] | None, enrollment: bool = False) -> str:
    summary = "<p>Use a registered passkey to open the owner dashboard.</p>"
    title = "Owner passkey"
    action = "Use passkey"
    enrollment_field = ""
    if details and details.get("purpose") == "merge":
        verb = "Merge and publish" if details["action"] == "merge" else "Deny (record only)"
        summary = f"""<p>This passkey tap authorizes only:</p>
<dl><dt>Action</dt><dd><strong>{html.escape(verb)}</strong></dd>
<dt>Queue</dt><dd><code>{html.escape(details['repo'])}</code></dd>
<dt>Reviewed queue OID</dt><dd><code>{html.escape(details['queue_oid'])}</code></dd>
<dt>Approved base OID</dt><dd><code>{html.escape(details['base_oid'])}</code></dd></dl>"""
        title = "Authorize this decision"
        action = "Tap passkey to authorize"
    elif details and details.get("purpose") == "unlock":
        summary = "<p>This passkey tap authorizes one unlock request. Review and acknowledgement remain separate.</p>"
        title = "Authorize unlock"
        action = "Tap passkey to authorize"
    if enrollment:
        title = "Enroll a passkey"
        action = "Create passkey"
        summary = "<p>Enter a one-time enrollment code, then create a passkey with user verification.</p>"
        enrollment_field = """<p><label for="enrollment-code">One-time enrollment code</label><br>
<input id="enrollment-code" type="password" autocomplete="off" required></p>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1rem}}code{{overflow-wrap:anywhere}}dt{{font-weight:600;margin-top:.7rem}}button{{padding:.7rem 1rem}}#passkey-message{{min-height:2em}}</style>
</head><body data-enrollment="{'yes' if enrollment else 'no'}" data-return-path="{html.escape(return_path, quote=True)}">
<h1>{html.escape(title)}</h1>{summary}{enrollment_field}
<p><button id="passkey-action" type="button">{html.escape(action)}</button></p>
<p id="passkey-message" role="status"></p>
<script src="/lock/auth/auth.js" defer></script></body></html>"""


def _return_path(value: str | None) -> str:
    if not value:
        return "/lock/"
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme or parsed.netloc or not parsed.path.startswith("/lock/")
        or parsed.path.startswith("//") or "\\" in value
    ):
        raise AuthorizationError("invalid local return path")
    return value


def _cookies(header: str | None) -> dict[str, str]:
    parsed = SimpleCookie()
    try:
        parsed.load(header or "")
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

    def __init__(
        self, address: tuple[str, int], config: Config, database: str | Path,
        enrollment_code: str = "",
    ) -> None:
        self.config = config
        self.transactions = TransactionStore(database)
        self.passkeys = PasskeyStore(database, config)
        self.enrollment_hash = _digest(enrollment_code) if enrollment_code else None
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
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _json(self, status: int, value: dict[str, Any]) -> None:
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

    def _passkey_body(self, action: str) -> dict[str, Any]:
        if self.headers.get("Origin") != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        if self.headers.get("X-Diary-Passkey-Action") != action:
            raise AuthorizationError("explicit passkey action required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AuthorizationError("valid Content-Length required") from exc
        if not 0 <= length <= MAX_REQUEST_BYTES:
            raise AuthorizationError("request body is oversized")
        if length == 0:
            return {}
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise AuthorizationError("JSON request body required")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorizationError("invalid JSON request") from exc
        if not isinstance(value, dict):
            raise AuthorizationError("JSON request must be an object")
        return value

    def _pending_details(self) -> dict[str, Any] | None:
        try:
            transaction_id, secret = _transaction_cookie(self.headers.get("Cookie"))
            return self.server.transactions.details(
                transaction_id, secret, self.server.config.owner_username, "pending",
            )
        except AuthorizationError:
            return None

    def _login(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return_path = _return_path((query.get("rd") or [None])[0])
        self._html(200, _passkey_page(return_path, self._pending_details()))

    def _passkey_options(self, enrollment: bool) -> None:
        body = self._passkey_body("options")
        self.server.begin_rate.check()
        transaction_id = None
        if enrollment:
            if set(body) != {"code"} or not isinstance(body["code"], str):
                raise AuthorizationError("one enrollment code is required")
            ceremony_id, secret, options = self.server.passkeys.registration_options(
                body["code"], self.server.enrollment_hash,
            )
        else:
            if body:
                raise AuthorizationError("passkey options take no request fields")
            details = self._pending_details()
            if details:
                transaction_id, _ = _transaction_cookie(self.headers.get("Cookie"))
            ceremony_id, secret, options = self.server.passkeys.authentication_options(transaction_id)
        self.send_response(200)
        self._common_headers()
        self.send_header(
            "Set-Cookie",
            f"{CEREMONY_COOKIE}={ceremony_id}.{secret}; Max-Age={self.server.config.transaction_ttl}; "
            f"Path={COOKIE_PATH}; Secure; HttpOnly; SameSite=Strict",
        )
        encoded = json.dumps({"publicKey": json.loads(options)}, separators=(",", ":")).encode("ascii")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _passkey_complete(self, enrollment: bool) -> None:
        credential = self._passkey_body("complete")
        if not credential:
            raise AuthorizationError("passkey credential is required")
        if enrollment:
            token = self.server.passkeys.register(self.headers.get("Cookie"), credential)
        else:
            token = self.server.passkeys.authenticate(self.headers.get("Cookie"), credential)
        self.send_response(200)
        self._common_headers()
        self.send_header(
            "Set-Cookie",
            f"{self.server.config.session_cookie}={token}; Max-Age={self.server.config.session_ttl}; "
            "Path=/lock; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header(
            "Set-Cookie", f"{CEREMONY_COOKIE}=; Max-Age=0; Path={COOKIE_PATH}; Secure; HttpOnly; SameSite=Strict",
        )
        body = b'{"status":"ok"}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward_auth(self) -> None:
        try:
            self.server.passkeys.verify_owner(self.headers.get("Cookie"))
        except AuthorizationError:
            try:
                return_path = _return_path(self.headers.get("X-Forwarded-URI"))
            except AuthorizationError:
                return_path = "/lock/"
            location = f"{self.server.config.public_origin}/lock/auth/?{urllib.parse.urlencode({'rd': return_path})}"
            self.send_response(303)
            self._common_headers()
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self._common_headers()
        self.send_header("Remote-User", self.server.config.owner_username)
        self.send_header("Remote-Groups", "lock-owner")
        self.send_header("Remote-Name", "Owner")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _javascript(self) -> None:
        encoded = PASSKEY_JS.encode("utf-8")
        self.send_response(200)
        self._common_headers()
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
        # Reserve the transaction first; logging out is only safe once it exists.
        transaction_id, secret = self.server.transactions.create(
            self.server.config.owner_username, self.server.config.transaction_ttl,
        )
        try:
            self.server.passkeys.logout(self.headers.get("Cookie"))
        except Exception:
            self.server.transactions.discard(
                transaction_id, secret, self.server.config.owner_username,
            )
            raise
        return_path = "/lock/unlock/return"
        location = f"{self.server.config.public_origin}/lock/auth/?{urllib.parse.urlencode({'rd': return_path})}"
        self.send_response(303)
        self._common_headers()
        self.send_header("Location", location)
        self.send_header(
            "Set-Cookie",
            f"{self.server.config.session_cookie}=; Max-Age=0; Path=/lock; Secure; HttpOnly; SameSite=Strict",
        )
        self.send_header(
            "Set-Cookie",
            f"{TRANSACTION_COOKIE}={transaction_id}.{secret}; Max-Age={self.server.config.transaction_ttl}; "
            f"Path={UNLOCK_COOKIE_PATH}; Secure; HttpOnly; SameSite=Strict",
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
        proof = self.server.passkeys.session_proof(
            self.headers.get("Cookie"), created_at, transaction_id,
        )
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
        proof = self.server.passkeys.session_proof(
            self.headers.get("Cookie"), created_at, transaction_id,
        )
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
        required = {"action", "queue_oid", "base_oid", "repo"}
        if not required <= set(body) or not set(body) <= required | {"csrf", "confirm"}:
            raise AuthorizationError("invalid merge authorization request")
        repo = body["repo"]
        if repo not in REPOS:
            raise AuthorizationError("merge authorization repository must be scripts or diary")
        action = body["action"]
        if action not in MERGE_ACTIONS:
            raise AuthorizationError("merge authorization action must be merge or deny")
        if "confirm" in body and body["confirm"] != action:
            raise AuthorizationError("merge authorization button does not match its action")
        queue_oid, base_oid = body["queue_oid"], body["base_oid"]
        if not isinstance(queue_oid, str) or not isinstance(base_oid, str):
            raise AuthorizationError("merge authorization requires full queue and base OIDs")
        # Only an already-authenticated owner may start a ceremony. Without this
        # an unauthenticated caller could fill the bounded live-transaction table
        # and block the owner's only merge path.
        self.server.passkeys.verify_owner(self.headers.get("Cookie"))
        self.server.begin_rate.check()
        # Reserve the transaction before destroying the session: a full table or
        # a storage error must not leave the owner signed out with nothing.
        transaction_id, secret = self.server.transactions.create(
            self.server.config.owner_username, self.server.config.transaction_ttl,
            purpose="merge", action=action, queue_oid=queue_oid, base_oid=base_oid, repo=repo,
        )
        try:
            self.server.passkeys.logout(self.headers.get("Cookie"))
        except Exception:
            # The session survives a failed logout, so the reservation must not.
            self.server.transactions.discard(
                transaction_id, secret, self.server.config.owner_username,
            )
            raise
        return_path = "/lock/merge/return"
        location = f"{self.server.config.public_origin}/lock/auth/?{urllib.parse.urlencode({'rd': return_path})}"
        self.send_response(303)
        self._common_headers()
        self.send_header("Location", location)
        self.send_header(
            "Set-Cookie",
            f"{self.server.config.session_cookie}=; Max-Age=0; Path=/lock; Secure; HttpOnly; SameSite=Strict",
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
        proof = self.server.passkeys.session_proof(
            self.headers.get("Cookie"), details["created_at"], transaction_id,
        )
        self.server.transactions.authenticate(
            transaction_id, secret, self.server.config.owner_username, proof.session_hash,
        )
        self._html(
            200,
            _confirmation_page(
                details["action"], details["queue_oid"], details["base_oid"],
                details["repo"], self.server.config.transaction_ttl,
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
        proof = self.server.passkeys.session_proof(
            self.headers.get("Cookie"), details["created_at"], transaction_id,
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
        path = urllib.parse.urlsplit(self.path).path
        try:
            if method == "GET" and path == "/lock/health":
                self._json(200, {"status": "ok"})
            elif method == "GET" and path == "/lock/auth/":
                self._login()
            elif method == "GET" and path == "/lock/auth/auth.js":
                self._javascript()
            elif method == "GET" and path == "/lock/auth/forward-auth":
                self._forward_auth()
            elif method == "GET" and path == "/lock/enroll/":
                self._html(200, _passkey_page("/lock/", None, enrollment=True))
            elif method == "POST" and path == "/lock/auth/options":
                self._passkey_options(enrollment=False)
            elif method == "POST" and path == "/lock/auth/complete":
                self._passkey_complete(enrollment=False)
            elif method == "POST" and path == "/lock/enroll/options":
                self._passkey_options(enrollment=True)
            elif method == "POST" and path == "/lock/enroll/complete":
                self._passkey_complete(enrollment=True)
            elif method == "GET" and path == "/lock/unlock/return":
                self._return()
            elif method == "POST" and path == "/lock/unlock/begin":
                self._begin()
            elif method == "POST" and path == "/lock/unlock/consume":
                self._consume()
            elif method == "POST" and path == "/lock/merge/begin":
                self._merge_begin()
            elif method == "GET" and path == "/lock/merge/return":
                self._merge_return()
            elif method == "POST" and path == "/lock/merge/consume":
                self._merge_consume()
            else:
                self._json(404, {"error": "not found"})
        except RateLimitError:
            self._json(429, {"error": "passkey ceremony creation rate exceeded"})
        except CapacityError:
            self._json(503, {"error": "passkey ceremony capacity exceeded"})
        except AuthorizationError:
            if (method, path) in BROWSER_ENDPOINTS:
                back = "/lock/merge/" if path.startswith("/lock/merge") else "/lock/"
                self._html(403, render_browser_error(
                    "This authorization attempt was not accepted: it expired, was "
                    "already used, or the passkey ceremony did not complete.",
                    back,
                ))
            else:
                self._json(403, {"error": "passkey authorization required"})

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
    parser.add_argument("--enrollment-code-file", default=DEFAULT_ENROLLMENT_CODE_FILE)
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        enrollment_code = Path(args.enrollment_code_file).read_text().strip()
    except FileNotFoundError:
        enrollment_code = ""
    if enrollment_code and not 16 <= len(enrollment_code) <= 256:
        raise AuthorizationError("enrollment code must contain 16 to 256 characters")
    server = FreshUnlockServer(
        (args.listen, args.port), config, args.database, enrollment_code=enrollment_code,
    )
    if args.verify_socket:
        start_verify_server(args.verify_socket, server.transactions, args.verify_user)
    server.serve_forever()


if __name__ == "__main__":
    main()
