#!/usr/bin/python3
"""Independent owner console host for merge/deny review (CTL-07 host, LATER-01).

This build intentionally registers only the merge console: a read-only review
page and one explicit decision POST. It has no unlock, acknowledgement,
attendance, lock, or arbitrary API route, and it holds no repository or audit
authority of its own. Every repository read and decision goes through the
fixed-argv executor, which requires a consumed single-use owner grant bound to
the action plus the exact queue and approved-base OIDs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from scripts import agent_merge_console as console
from scripts.diary_lock_ui import AutheliaClient, Config, OwnerUIError, _cookies, load_config

DEFAULT_CONFIG = "/etc/diary/lock-auth.toml"
DEFAULT_EXECUTOR_SOCKET = "/run/diary-merge-console/socket"
DEFAULT_FRESH_BACKEND = "http" + "://127.0.0.1:9092"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
MERGE_COOKIE_PATH = "/lock/merge"
DECISION_PATH = "/lock/merge/decide"
PAGE_PATH = "/lock/merge/"
CSRF_TTL = 300
MAX_CSRF_TOKENS = 32
MAX_REQUEST_BYTES = 64 * 1024
MAX_EXECUTOR_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
OID_RE = re.compile(r"[0-9a-f]{40}\Z")


class ConsoleUIError(ValueError):
    status = 403
    code = "invalid"


class AuthorizationError(ConsoleUIError):
    code = "forbidden"


class DecisionRejected(ConsoleUIError):
    status = 409


class DecisionUnavailable(ConsoleUIError):
    status = 503


class DecisionFailed(ConsoleUIError):
    status = 502


@dataclass(frozen=True)
class ExecutorReply:
    ok: bool
    error: str
    snapshot: dict[str, Any] | None
    outcome: str


class ExecutorClient:
    """Fixed-protocol client for the kek-run executor; never a shell or broker."""

    def __init__(self, path: str = DEFAULT_EXECUTOR_SOCKET, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout

    def request(self, value: dict[str, Any]) -> ExecutorReply:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_EXECUTOR_BYTES:
                    raise DecisionUnavailable("executor response is oversized")
        except OSError as exc:
            raise DecisionUnavailable("the merge executor is unavailable") from exc
        finally:
            sock.close()
        try:
            reply = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecisionUnavailable("the merge executor response is invalid") from exc
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise DecisionUnavailable("the merge executor response is invalid")
        error = reply.get("error") if isinstance(reply.get("error"), str) else ""
        snapshot = reply.get("snapshot") if isinstance(reply.get("snapshot"), dict) else None
        outcome = reply.get("outcome") if isinstance(reply.get("outcome"), str) else ""
        return ExecutorReply(reply["ok"], error, snapshot, outcome)


class FreshGrantClient:
    """Consume the browser's fresh single-use merge grant at decision time."""

    def __init__(self, public_origin: str, backend: str, session_cookie: str) -> None:
        self.public_origin = public_origin
        self.backend = backend
        self.session_cookie = session_cookie

    def consume(self, cookie_header: str | None) -> None:
        cookies = _cookies(cookie_header)
        selected = [
            f"{name}={cookies[name]}" for name in (self.session_cookie, TRANSACTION_COOKIE) if name in cookies
        ]
        backend = urllib.parse.urlsplit(self.backend)
        connection = http.client.HTTPConnection(backend.hostname or "", backend.port or 80, timeout=5)
        try:
            connection.request(
                "POST", "/lock/merge/consume",
                headers={
                    "Origin": self.public_origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Diary-Merge-Confirm": "yes",
                    "Content-Length": "0",
                    "Cookie": "; ".join(selected),
                },
                body=b"",
            )
            response = connection.getresponse()
            response.read(1024)
            status = response.status
        except OSError as exc:
            raise DecisionUnavailable("the fresh authorization service is unavailable") from exc
        finally:
            connection.close()
        if status != 204:
            raise AuthorizationError(
                "a fresh password + WebAuthn authorization for this exact decision is required"
            )


class CsrfStore:
    """Bounded, expiring CSRF tokens bound to the owner's Authelia session."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens: dict[str, tuple[str, float]] = {}

    def issue(self, session_digest: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self.tokens = {key: value for key, value in self.tokens.items() if value[1] >= now}
            if len(self.tokens) >= MAX_CSRF_TOKENS:
                self.tokens.clear()
            self.tokens[token] = (session_digest, now + CSRF_TTL)
        return token

    def consume(self, token: str, session_digest: str) -> bool:
        now = self.clock()
        with self.lock:
            self.tokens = {key: value for key, value in self.tokens.items() if value[1] >= now}
            value = self.tokens.pop(token, None)
        return value is not None and value[0] == session_digest


def session_digest(cookie_header: str | None, session_cookie: str) -> str:
    value = _cookies(cookie_header).get(session_cookie, "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_from_json(value: dict[str, Any]) -> console.QueueSnapshot | None:
    if value.get("pending") is False:
        return None
    try:
        commits = tuple(
            console.QueueCommit(str(item["oid"]), str(item["subject"])) for item in value["commits"]
        )
        advisory = value["advisory"]
        return console.QueueSnapshot(
            str(value["queue_oid"]), str(value["base_oid"]), commits,
            str(value["diff"]), console.AdvisoryView(str(advisory["status"]), str(advisory["text"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionUnavailable("the executor snapshot is invalid") from exc


MESSAGES = {
    "merged": ("Merged", "The exact reviewed queue and approved-base OIDs were merged and published."),
    "denied": ("Denied", "The decision was recorded. Nothing was merged, pushed, reset, or synchronized."),
    "queue_changed": ("Queue changed", "The queue changed after you reviewed it. Nothing was merged."),
    "base_changed": ("Approved base changed", "Approved master changed after you reviewed it. Nothing was merged."),
    "unauthorized": ("Authorization required", "A fresh owner authorization was required and was not accepted."),
    "unavailable": ("Unavailable", "The trusted service is unavailable; nothing was merged."),
    "merge_failed": ("Unknown outcome", "Publication failed or its result could not be confirmed. Check the repository state before retrying."),
    "invalid": ("Rejected", "The decision request was invalid; nothing was merged."),
    "forbidden": ("Refused", "The request was refused; nothing was merged or pushed."),
    "empty": ("Nothing queued", "No queued diary-scripts changes require review."),
}


def render_result(code: str) -> str:
    title, detail = MESSAGES.get(code, MESSAGES["invalid"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'">
<title>{html.escape(title)}</title>
<style>body{{font-family:sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}</style>
</head>
<body><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>
<p><a href="{PAGE_PATH}">Back to the merge review</a></p></body></html>
"""


AUTHORIZE_ACTION = "/lock/merge/begin"


def render_authorize(action: str, queue_oid: str, base_oid: str) -> str:
    """Offer the fresh-authorization ceremony as a plain same-origin form.

    Nothing is decided on this page. It exists so an owner without a current
    grant gets a button instead of a dead end: the root-owned service shows the
    bound action and OIDs before the console can consume it.
    """
    verb = "merge and publish" if action == "merge" else "deny (record only)"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'">
<title>Fresh authorization required</title>
<style>body{{font-family:sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}code{{overflow-wrap:anywhere}}</style>
</head>
<body>
<h1>Fresh authorization required</h1>
<p>This decision needs a password + user-verified WebAuthn ceremony bound to
exactly this action and OID pair:</p>
<dl>
<dt>Action</dt><dd><strong>{html.escape(verb)}</strong></dd>
<dt>Reviewed queue OID</dt><dd><code>{html.escape(queue_oid)}</code></dd>
<dt>Approved base OID</dt><dd><code>{html.escape(base_oid)}</code></dd>
</dl>
<p>After signing in, the authorization service shows the same decision on its
own page. Nothing is merged until you confirm here again.</p>
<form method="post" action="{AUTHORIZE_ACTION}">
<input type="hidden" name="action" value="{html.escape(action, quote=True)}">
<input type="hidden" name="queue_oid" value="{html.escape(queue_oid, quote=True)}">
<input type="hidden" name="base_oid" value="{html.escape(base_oid, quote=True)}">
<button type="submit">Sign in with password + passkey</button>
</form>
<p><a href="{PAGE_PATH}">Cancel</a></p>
</body>
</html>
"""


def render_page(snapshot: console.QueueSnapshot | None, csrf_token: str) -> str:
    if snapshot is None:
        return render_result("empty")
    return console.render_console(snapshot, DECISION_PATH, csrf_token)


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        config: Config,
        executor: ExecutorClient,
        fresh: FreshGrantClient,
        csrf: CsrfStore | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.fresh = fresh
        self.authelia = AutheliaClient(config)
        self.csrf = csrf or CsrfStore()
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, ConsoleHandler)

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


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("ascii")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _require_form_action(self) -> dict[str, str]:
        if self.headers.get("Origin") != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            raise AuthorizationError("form request body required")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise AuthorizationError("valid Content-Length required") from exc
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise AuthorizationError("request body is missing or oversized")
        parsed = urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8", "strict"), keep_blank_values=True, strict_parsing=True,
        )
        return {key: values[0] for key, values in parsed.items() if len(values) == 1}

    def _page(self) -> None:
        cookie = self.headers.get("Cookie")
        self.server.authelia.verify_owner(cookie, "GET", self.path)
        reply = self.server.executor.request({"cmd": "snapshot"})
        if not reply.ok or reply.snapshot is None:
            raise DecisionUnavailable("the merge review is unavailable")
        snapshot = _snapshot_from_json(reply.snapshot)
        token = self.server.csrf.issue(session_digest(cookie, self.server.config.session_cookie))
        self._html(200, render_page(snapshot, token))

    def _decide(self) -> None:
        cookie = self.headers.get("Cookie")
        self.server.authelia.verify_owner(cookie, "POST", self.path)
        fields = self._require_form_action()
        if set(fields) != {"action", "queue_oid", "base_oid", "csrf"}:
            raise AuthorizationError("invalid decision request")
        action, queue_oid, base_oid = fields["action"], fields["queue_oid"], fields["base_oid"]
        if action not in {"merge", "deny"}:
            raise AuthorizationError("decision must be merge or deny")
        if not OID_RE.fullmatch(queue_oid) or not OID_RE.fullmatch(base_oid):
            raise AuthorizationError("decision requires full reviewed OIDs")
        digest = session_digest(cookie, self.server.config.session_cookie)
        if not self.server.csrf.consume(fields["csrf"], digest):
            raise AuthorizationError("a current review form is required")
        try:
            transaction = _transaction_id(cookie)
            self.server.fresh.consume(cookie)
        except AuthorizationError:
            self._html(200, render_authorize(action, queue_oid, base_oid))
            return
        reply = self.server.executor.request({
            "cmd": "decide", "action": action, "queue_oid": queue_oid,
            "base_oid": base_oid, "transaction": transaction,
        })
        if reply.ok:
            self._html(200, render_result("merged" if reply.outcome == "merged" else "denied"))
            return
        raise _decision_error(reply.error)

    def _handle(self, method: str) -> None:
        try:
            if method == "GET" and self.path == "/lock/health":
                self._json(200, {"status": "ok"})
            elif method == "GET" and self.path == "/lock/merge":
                self.send_response(302)
                self._common_headers()
                self.send_header("Location", PAGE_PATH)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif method == "GET" and self.path == PAGE_PATH:
                self._page()
            elif method == "POST" and self.path == DECISION_PATH:
                self._decide()
            else:
                self._json(404, {"error": "not found"})
        except ConsoleUIError as exc:
            self._html(exc.status, render_result(exc.code))
        except Exception:
            self._html(503, render_result("unavailable"))

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def _decision_error(code: str) -> ConsoleUIError:
    mapping: dict[str, tuple[type[ConsoleUIError], int]] = {
        "queue_changed": (DecisionRejected, 409),
        "base_changed": (DecisionRejected, 409),
        "unauthorized": (AuthorizationError, 403),
        "unavailable": (DecisionUnavailable, 503),
        "merge_failed": (DecisionFailed, 502),
    }
    kind, status = mapping.get(code, (DecisionRejected, 409))
    resolved = code if code in MESSAGES else "invalid"
    error = kind()
    error.status = status
    error.code = resolved
    return error


def _transaction_id(cookie_header: str | None) -> str:
    value = _cookies(cookie_header or "").get(TRANSACTION_COOKIE, "")
    parts = value.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise AuthorizationError("a fresh merge authorization for this decision is required")
    return parts[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--executor-socket", default=DEFAULT_EXECUTOR_SOCKET)
    parser.add_argument("--fresh-backend", default=DEFAULT_FRESH_BACKEND)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9093)
    args = parser.parse_args()
    config = load_config(args.config, args.fresh_backend)
    executor = ExecutorClient(args.executor_socket)
    fresh = FreshGrantClient(config.public_origin, args.fresh_backend, config.session_cookie)
    server = ConsoleServer((args.listen, args.port), config, executor, fresh)
    server.serve_forever()


if __name__ == "__main__":
    main()
