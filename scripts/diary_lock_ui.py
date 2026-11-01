#!/usr/bin/python3
"""Independent Authelia-protected review, unlock, and attendance UI."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol, cast

from scripts import diary_production_baseline as production_baseline

DEFAULT_CONFIG = "/etc/diary/lock-auth.toml"
DEFAULT_CONTROLLER_SOCKET = "/run/diary-lock/socket"
DEFAULT_AUDIT_SOCKET = "/run/diary-audit/socket"
DEFAULT_EXPECTED_FLOWS = "/etc/diary/lock-expected-flows.json"
DEFAULT_FRESH_BACKEND = "http://127.0.0.1:9092"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
MAX_ACKNOWLEDGEMENTS = 32
ACKNOWLEDGEMENT_TTL = 300
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32}\Z")
ATTENTION_DECISIONS = frozenset({"deny", "would_deny", "error"})
ROUTINE_DECISIONS = frozenset({"allow", "would_allow", "observed"})
GROUP_FIELDS = (
    "fingerprint", "event", "component", "operation", "decision", "lock_state", "reason", "unit",
    "destination_ip", "destination_port", "protocol", "dns_name", "caller_uid",
    "count", "records", "first_seen", "last_seen", "repeated",
)

WORKFLOW_JS = """'use strict';
async function request(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.error) detail = body.error;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}
function post(path, action, body) {
  const headers = {'Origin': window.location.origin, [action]: 'yes'};
  const options = {method: 'POST', headers};
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  return request(path, options);
}
function mount({element, getReport, getState, refresh}) {
  const acknowledged = new Set();
  const message = document.createElement('p');
  message.setAttribute('role', 'status');
  const lock = document.createElement('button');
  lock.type = 'button';
  lock.textContent = 'Lock now';
  const extend = document.createElement('button');
  extend.type = 'button';
  extend.textContent = 'Extend attendance by 15 minutes';
  const begin = document.createElement('button');
  begin.type = 'button';
  begin.textContent = 'Begin fresh authorization';
  const unlock = document.createElement('button');
  unlock.type = 'button';
  unlock.textContent = 'Confirm unlock';
  const ackAll = document.createElement('button');
  ackAll.type = 'button';
  ackAll.textContent = 'Acknowledge all listed groups';
  const acknowledgements = document.createElement('div');
  element.replaceChildren(message, lock, extend, begin, unlock, ackAll, acknowledgements);
  function render() {
    const state = getState();
    const report = getReport();
    extend.disabled = state.state !== 'ATTENDED';
    begin.disabled = state.state !== 'LOCKED';
    unlock.disabled = state.state !== 'LOCKED' || report.incomplete;
    ackAll.disabled = report.incomplete || !report.required_groups.length;
    const buttons = report.required_groups.map(group => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.fingerprint = group.fingerprint;
      button.textContent = acknowledged.has(group.fingerprint) ? `Acknowledged ${group.fingerprint}` : `Acknowledge ${group.fingerprint}`;
      button.disabled = report.incomplete || acknowledged.has(group.fingerprint);
      button.addEventListener('click', () => acknowledge([group.fingerprint]));
      return button;
    });
    acknowledgements.replaceChildren(...buttons);
  }
  async function finish(action, text) {
    try {
      await action();
      message.textContent = text;
      await refresh();
      render();
    } catch (error) { message.textContent = error.message; }
  }
  async function acknowledge(groups) {
    const report = getReport();
    await finish(async () => {
      const result = await post('/lock/unlock/acknowledge', 'X-Diary-Acknowledge-Review', {review_id: report.review_id, groups});
      for (const fingerprint of result.groups) acknowledged.add(fingerprint);
    }, 'Acknowledgement recorded for this fresh transaction.');
  }
  lock.addEventListener('click', () => finish(() => post('/lock/api/lock', 'X-Diary-Lock-Now'), 'Locked now.'));
  extend.addEventListener('click', () => finish(() => post('/lock/api/extend', 'X-Diary-Extend-Attendance'), 'Attendance extended by 15 minutes.'));
  ackAll.addEventListener('click', () => acknowledge(getReport().required_groups.map(group => group.fingerprint)));
  unlock.addEventListener('click', () => finish(() => post('/lock/unlock/confirm', 'X-Diary-Unlock-Confirm'), 'Unlocked into attended mode.'));
  begin.addEventListener('click', async () => {
    try {
      const response = await fetch('/lock/unlock/begin', {method: 'POST', headers: {'Origin': window.location.origin, 'X-Diary-Unlock-Begin': 'yes'}});
      if (!response.ok) throw new Error(`Fresh authorization could not begin (HTTP ${response.status}).`);
      window.location.assign(response.url);
    } catch (error) { message.textContent = error.message; }
  });
  render();
  return {render};
}
window.DiaryLockWorkflow = {mount};
"""


class OwnerUIError(ValueError):
    status = 403


class AuthorizationError(OwnerUIError):
    pass


class ReviewRequired(OwnerUIError):
    status = 409


class ServiceUnavailable(OwnerUIError):
    status = 503


class UnlockFailed(OwnerUIError):
    status = 502


@dataclass(frozen=True)
class Config:
    owner_username: str
    public_origin: str
    authelia_backend: str
    fresh_backend: str
    session_cookie: str


def _loopback_http_origin(value: str, label: str) -> str:
    origin = urllib.parse.urlsplit(value)
    if (
        origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost"}
        or origin.path or origin.query or origin.fragment or origin.username or origin.password
    ):
        raise OwnerUIError(f"{label} must be a loopback HTTP origin")
    return value.rstrip("/")


def load_config(
    path: str | Path = DEFAULT_CONFIG, fresh_backend: str = DEFAULT_FRESH_BACKEND,
) -> Config:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    allowed = {
        "owner_username", "public_origin", "authelia_backend",
        "session_cookie", "transaction_ttl_seconds",
    }
    required = allowed - {"transaction_ttl_seconds"}
    if set(raw) - allowed or not required <= set(raw):
        raise OwnerUIError("unknown or incomplete lock UI configuration")
    values = {key: raw[key] for key in required}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise OwnerUIError("lock UI settings must be non-empty strings")
    origin = urllib.parse.urlsplit(values["public_origin"])
    if origin.scheme != "https" or not origin.hostname or origin.path or origin.query or origin.fragment:
        raise OwnerUIError("public origin must be an HTTPS origin without a path")
    cookie = values["session_cookie"]
    if not cookie.isascii() or not cookie.replace("_", "a").replace("-", "a").isalnum():
        raise OwnerUIError("invalid Authelia session cookie name")
    return Config(
        owner_username=values["owner_username"],
        public_origin=values["public_origin"].rstrip("/"),
        authelia_backend=_loopback_http_origin(values["authelia_backend"], "Authelia backend"),
        fresh_backend=_loopback_http_origin(fresh_backend, "fresh authorization backend"),
        session_cookie=cookie,
    )


def _cookies(header: str | None) -> dict[str, str]:
    parsed = SimpleCookie()
    try:
        parsed.load(header or "")
    except CookieError as exc:
        raise AuthorizationError("invalid cookies") from exc
    return {key: morsel.value for key, morsel in parsed.items()}


def _request(
    origin: str, method: str, path: str, headers: dict[str, str], body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    backend = urllib.parse.urlsplit(origin)
    connection = http.client.HTTPConnection(backend.hostname or "", backend.port or 80, timeout=5)
    try:
        connection.request(method, path, headers=headers, body=body)
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise ServiceUnavailable("trusted service response is too large")
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, response_body
    except (OSError, http.client.HTTPException) as exc:
        raise ServiceUnavailable("trusted service is unavailable") from exc
    finally:
        connection.close()


class AutheliaClient:
    def __init__(self, config: Config):
        self.config = config
        self.public = urllib.parse.urlsplit(config.public_origin)

    def verify_owner(self, cookie_header: str | None, method: str, uri: str) -> None:
        session = _cookies(cookie_header).get(self.config.session_cookie, "")
        if not session:
            raise AuthorizationError("an owner Authelia session is required")
        headers = {
            "X-Forwarded-Method": method,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": self.public.netloc,
            "X-Forwarded-URI": uri,
            "Cookie": f"{self.config.session_cookie}={session}",
        }
        query = urllib.parse.urlencode({"authelia_url": f"{self.config.public_origin}/authelia/"})
        status, response_headers, _ = _request(
            self.config.authelia_backend, "GET", f"/api/authz/forward-auth?{query}", headers,
        )
        groups = {item.strip() for item in response_headers.get("remote-groups", "").split(",")}
        if (
            status != 200
            or response_headers.get("remote-user") != self.config.owner_username
            or "lock-owner" not in groups
        ):
            raise AuthorizationError("an owner two-factor Authelia session is required")


class FreshAuthorizationClient:
    def __init__(self, config: Config):
        self.config = config

    def consume(self, cookie_header: str | None) -> None:
        cookies = _cookies(cookie_header)
        selected = []
        for name in (self.config.session_cookie, TRANSACTION_COOKIE):
            if name in cookies:
                selected.append(f"{name}={cookies[name]}")
        headers = {
            "Origin": self.config.public_origin,
            "Sec-Fetch-Site": "same-origin",
            "X-Diary-Unlock-Confirm": "yes",
            "Content-Length": "0",
            "Cookie": "; ".join(selected),
        }
        status, _, _ = _request(self.config.fresh_backend, "POST", "/lock/unlock/consume", headers, b"")
        if status != 204:
            raise AuthorizationError("fresh single-use authorization is invalid, expired, or already used")


def _unix_request(path: str, value: dict[str, Any]) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10)
        sock.connect(path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 1024 * 1024:
                raise ServiceUnavailable("trusted socket response is too large")
        reply = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceUnavailable("trusted socket is unavailable") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise ServiceUnavailable("trusted socket request failed")
    return reply


class Controller(Protocol):
    def call(self, command: str) -> dict[str, Any]: ...


class ControllerClient:
    def __init__(
        self,
        path: str = DEFAULT_CONTROLLER_SOCKET,
        reader: Callable[[str, dict[str, Any]], dict[str, Any]] = _unix_request,
    ):
        self.path = path
        self.reader = reader

    def call(self, command: str) -> dict[str, Any]:
        if command not in {"status", "lock", "unlock", "extend"}:
            raise OwnerUIError("unsupported controller command")
        return self.reader(self.path, {"cmd": command})


class ReportProvider(Protocol):
    def review(self) -> dict[str, Any]: ...


def _group(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ServiceUnavailable("audit report contains an invalid group")
    value = cast(dict[str, Any], value)
    if not FINGERPRINT_RE.fullmatch(str(value.get("fingerprint", ""))):
        raise ServiceUnavailable("audit report contains an invalid group")
    result = {field: value[field] for field in GROUP_FIELDS if field in value}
    for field in ("count", "records"):
        item = result.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ServiceUnavailable("audit report contains invalid counters")
    if result.get("decision") not in ATTENTION_DECISIONS | ROUTINE_DECISIONS:
        raise ServiceUnavailable("audit report contains an unknown decision")
    event = result.get("event")
    operation = result.get("operation")
    if not isinstance(event, str) or not isinstance(operation, str):
        raise ServiceUnavailable("audit report group has no operation")
    result["category"] = event.split(".", 1)[0] if event.split(".", 1)[0] in {"network", "dns", "broker"} else "other"
    return result


def _report_groups(report: object, expected: str) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(report, dict):
        raise ServiceUnavailable("audit report response is invalid")
    report = cast(dict[str, Any], report)
    if report.get("ok") is not True or report.get("report") != expected:
        raise ServiceUnavailable("audit report response is invalid")
    groups = report.get("groups")
    truncated = report.get("truncated")
    if not isinstance(groups, list) or not isinstance(truncated, bool):
        raise ServiceUnavailable("audit report response is incomplete")
    return [_group(group) for group in groups], truncated


class AuditReportProvider:
    def __init__(
        self, controller: Controller, audit_socket: str = DEFAULT_AUDIT_SOCKET,
        expected: dict[str, Any] | None = None,
        reader: Callable[[str, dict[str, Any]], dict[str, Any]] = _unix_request,
    ):
        self.controller = controller
        self.audit_socket = audit_socket
        self.expected = production_baseline.load_expected(expected)
        self.reader = reader

    def review(self) -> dict[str, Any]:
        status = self.controller.call("status")
        since_value = status.get("review_since")
        if isinstance(since_value, bool) or not isinstance(since_value, (int, float)) or not math.isfinite(since_value):
            raise ServiceUnavailable("controller has no valid review window")
        since = dt.datetime.fromtimestamp(since_value, dt.UTC).isoformat()
        review, review_truncated = _report_groups(
            self.reader(self.audit_socket, {"cmd": "review", "since": since, "until": "now"}),
            "review",
        )
        findings = production_baseline.classify_groups(review, self.expected)
        required_by_fingerprint = {}
        for kind in production_baseline.FINDING_KINDS[1:]:
            for group in findings[kind]:
                required_by_fingerprint[group["fingerprint"]] = group
        required_ids = sorted(required_by_fingerprint)
        review_id = hashlib.sha256(json.dumps(
            {"since": since, "groups": required_ids}, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        required = []
        for fingerprint in required_ids:
            group = required_by_fingerprint[fingerprint]
            group["attention"] = (
                "suspicious" if set(group["classifications"]) & {"repeated_denials", "suspicious_broker_activity"}
                else "novel"
            )
            required.append(group)
        return {
            "review_id": review_id,
            "since": since,
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "incomplete": review_truncated,
            "required_groups": required,
        }


@dataclass
class Acknowledgement:
    review_id: str
    groups: set[str]
    expires_at: float


class AcknowledgementStore:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.records: dict[str, Acknowledgement] = {}
        self.lock = threading.Lock()

    def _purge(self, now: float) -> None:
        self.records = {key: value for key, value in self.records.items() if value.expires_at >= now}

    def add(self, transaction: str, review_id: str, groups: set[str]) -> None:
        now = self.clock()
        with self.lock:
            self._purge(now)
            current = self.records.get(transaction)
            if current is None and len(self.records) >= MAX_ACKNOWLEDGEMENTS:
                raise ServiceUnavailable("acknowledgement capacity is exhausted")
            if current is None or current.review_id != review_id:
                current = Acknowledgement(review_id, set(), now + ACKNOWLEDGEMENT_TTL)
                self.records[transaction] = current
            current.groups.update(groups)
            current.expires_at = now + ACKNOWLEDGEMENT_TTL

    def acknowledged(self, transaction: str | None, review_id: str) -> set[str]:
        now = self.clock()
        with self.lock:
            self._purge(now)
            if transaction is None:
                return set()
            current = self.records.get(transaction)
            return set(current.groups) if current is not None and current.review_id == review_id else set()

    def complete(self, transaction: str, review_id: str, required: set[str]) -> bool:
        return required <= self.acknowledged(transaction, review_id)


class OwnerUIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], config: Config, controller: Controller,
        reports: ReportProvider, fresh: FreshAuthorizationClient | None = None,
        acknowledgement_clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.controller = controller
        self.reports = reports
        self.authelia = AutheliaClient(config)
        self.fresh = fresh or FreshAuthorizationClient(config)
        self.acknowledgements = AcknowledgementStore(acknowledgement_clock)
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, OwnerUIHandler)

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


class OwnerUIHandler(BaseHTTPRequestHandler):
    server: OwnerUIServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _response(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._response(status, "application/json", json.dumps(value, separators=(",", ":")).encode("ascii"))

    def _require_action(self, header: str, allow_body: bool = False) -> None:
        if self.headers.get("Origin") != self.server.config.public_origin:
            raise AuthorizationError("same-origin browser request required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise AuthorizationError("cross-site request rejected")
        if self.headers.get(header) != "yes":
            raise AuthorizationError("explicit browser action required")
        if not allow_body and self.headers.get("Content-Length", "0") != "0":
            raise AuthorizationError("request body is not accepted")

    def _json_body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise AuthorizationError("JSON request body required")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise AuthorizationError("valid Content-Length required") from exc
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise AuthorizationError("request body is missing or oversized")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorizationError("invalid JSON request") from exc
        if not isinstance(value, dict):
            raise AuthorizationError("JSON request must be an object")
        return value

    def _transaction_id(self) -> str:
        value = _cookies(self.headers.get("Cookie")).get(TRANSACTION_COOKIE, "")
        parts = value.split(".")
        if len(parts) != 2 or not parts[1] or not TRANSACTION_ID_RE.fullmatch(parts[0]):
            raise AuthorizationError("a fresh-unlock transaction is required")
        return parts[0]

    def _optional_transaction_id(self) -> str | None:
        try:
            return self._transaction_id()
        except AuthorizationError:
            return None

    def _report(self) -> dict[str, Any]:
        report = self.server.reports.review()
        if not isinstance(report, dict) or not FINGERPRINT_RE.fullmatch(str(report.get("review_id", ""))):
            raise ServiceUnavailable("review provider returned an invalid report")
        acknowledged = self.server.acknowledgements.acknowledged(
            self._optional_transaction_id(), report["review_id"],
        )
        for group in report.get("required_groups", []):
            group["acknowledged"] = group.get("fingerprint") in acknowledged
        return report

    def _acknowledge(self) -> None:
        self._require_action("X-Diary-Acknowledge-Review", allow_body=True)
        body = self._json_body()
        if set(body) != {"review_id", "groups"} or not isinstance(body["review_id"], str) or not isinstance(body["groups"], list):
            raise AuthorizationError("invalid acknowledgement request")
        groups = body["groups"]
        if not groups or len(groups) > 10_000 or any(not isinstance(item, str) or not FINGERPRINT_RE.fullmatch(item) for item in groups):
            raise AuthorizationError("invalid acknowledgement groups")
        if len(set(groups)) != len(groups):
            raise AuthorizationError("duplicate acknowledgement groups")
        groups = cast(list[str], groups)
        transaction = self._transaction_id()
        status = self.server.controller.call("status")
        if status.get("state") != "LOCKED":
            raise ReviewRequired("acknowledgement requires locked state")
        report = self._report()
        if report.get("incomplete") is True:
            raise ReviewRequired("a complete report is required")
        if body["review_id"] != report["review_id"]:
            raise ReviewRequired("the report changed; review it again")
        required = {group["fingerprint"] for group in report["required_groups"]}
        if not set(groups) <= required:
            raise ReviewRequired("only current novel or suspicious groups can be acknowledged")
        self.server.acknowledgements.add(transaction, report["review_id"], set(groups))
        acknowledged = self.server.acknowledgements.acknowledged(transaction, report["review_id"])
        self._json(200, {"status": "acknowledged", "groups": sorted(acknowledged)})

    def _confirm(self) -> None:
        self._require_action("X-Diary-Unlock-Confirm")
        transaction = self._transaction_id()
        status = self.server.controller.call("status")
        if status.get("state") != "LOCKED":
            raise ReviewRequired("fresh unlock requires locked state")
        report = self._report()
        if report.get("incomplete") is True:
            raise ReviewRequired("a complete report is required before unlock")
        required = {group["fingerprint"] for group in report["required_groups"]}
        if not self.server.acknowledgements.complete(transaction, report["review_id"], required):
            raise ReviewRequired("acknowledge every current novel or suspicious group before unlock")
        self.server.fresh.consume(self.headers.get("Cookie"))
        try:
            result = self.server.controller.call("unlock")
        except ServiceUnavailable as exc:
            raise UnlockFailed("unlock failed; restrictions remain in place and authorization cannot be replayed") from exc
        self._json(200, result)

    def _handle(self, method: str) -> None:
        try:
            if method == "GET" and self.path == "/lock/health":
                self._json(200, {"status": "ok"})
                return
            self.server.authelia.verify_owner(self.headers.get("Cookie"), method, self.path)
            if method == "GET" and self.path == "/lock/workflow.js":
                self._response(200, "text/javascript; charset=utf-8", WORKFLOW_JS.encode("utf-8"))
            elif method == "POST" and self.path == "/lock/api/lock":
                self._require_action("X-Diary-Lock-Now")
                self._json(200, self.server.controller.call("lock"))
            elif method == "POST" and self.path == "/lock/api/extend":
                self._require_action("X-Diary-Extend-Attendance")
                self._json(200, self.server.controller.call("extend"))
            elif method == "POST" and self.path == "/lock/unlock/acknowledge":
                self._acknowledge()
            elif method == "POST" and self.path == "/lock/unlock/confirm":
                self._confirm()
            else:
                self._json(404, {"error": "not found"})
        except OwnerUIError as exc:
            self._json(exc.status, {"error": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--controller-socket", default=DEFAULT_CONTROLLER_SOCKET)
    parser.add_argument("--audit-socket", default=DEFAULT_AUDIT_SOCKET)
    parser.add_argument("--expected-flows", default=DEFAULT_EXPECTED_FLOWS)
    parser.add_argument("--fresh-backend", default=DEFAULT_FRESH_BACKEND)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9093)
    args = parser.parse_args()
    config = load_config(args.config, args.fresh_backend)
    controller = ControllerClient(args.controller_socket)
    expected = production_baseline.load_json(args.expected_flows)
    reports = AuditReportProvider(controller, args.audit_socket, expected)
    server = OwnerUIServer((args.listen, args.port), config, controller, reports)
    server.serve_forever()


if __name__ == "__main__":
    main()
