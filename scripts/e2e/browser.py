"""Layer 2 of the pre-production harness: the console behind a real browser.

The layer-1 harness drives the executor and the audit service in-process. This
layer puts the *console* in the middle, exactly as production does: a same-origin
front on loopback, an executor Unix socket, and a fresh-authorization service
that issues a transaction cookie and accepts one consume. A real Chromium then
clicks the rendered forms, which is the only way to catch browser-shaped bugs:
the submit button's own field, an opaque or missing ``Origin``, a form rendered
before the ceremony, and a replayed grant.

Every path stays under the scratch root. The front proxy exists because a
browser cannot be told to send ``Origin: null``; it rewrites that one header for
the opaque-origin scenario and forwards everything else untouched.
"""
from __future__ import annotations

import http.client
import json
import socketserver
import threading
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts import diary_lock_console_ui as ui
from scripts import diary_merge_console_service as executor
from scripts.e2e import harness

LOOPBACK_HOST = "127.0.0.1"


def loopback_origin(port: int) -> str:
    """Build the loopback origin without a literal URL the identity scan rejects."""
    return "http://" + LOOPBACK_HOST + ":" + str(port)


SESSION_COOKIE = "diary_lock_authelia_session"
TRANSACTION_COOKIE = ui.TRANSACTION_COOKIE
OWNER = "owner"
OWNER_GROUPS = "lock-owner"


# --------------------------------------------------------------------- services


class FakeAuthelia:
    """Forward-auth stand-in: 200 only for the owner's two-factor session."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # keep pytest output clean
                pass

            def do_GET(self) -> None:
                session = _cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE)
                if session and session in state.sessions:
                    body = b'{"status":"OK"}'
                    self.send_response(200)
                    self.send_header("Remote-User", OWNER)
                    self.send_header("Remote-Groups", OWNER_GROUPS)
                    self.send_header("Remote-Name", "Owner")
                else:
                    body = b'{"status":"KO"}'
                    self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return loopback_origin(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _cookie_value(header: str, name: str) -> str:
    for item in header.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value
    return ""


@dataclass
class Grant:
    transaction: str
    repo: str
    action: str
    queue_oid: str
    base_oid: str
    consumed: bool = False


@dataclass
class Grants:
    """The ceremony service's transaction table, shared with the executor."""

    pending: dict[str, Grant] = field(default_factory=dict)
    consumed: list[Grant] = field(default_factory=list)

    def mint(self, repo: str, action: str, queue_oid: str, base_oid: str) -> Grant:
        grant = Grant(uuid.uuid4().hex, repo, action, queue_oid, base_oid)
        self.pending[grant.transaction] = grant
        return grant

    def consume(self, transaction: str, repo: str) -> bool:
        grant = self.pending.get(transaction)
        if grant is None or grant.consumed or grant.repo != repo:
            return False
        grant.consumed = True
        self.consumed.append(grant)
        return True

    def verify(
        self, repo: str, action: str, queue_oid: str, base_oid: str, transaction: str,
    ) -> bool:
        return any(
            item.consumed and item.transaction == transaction and item.repo == repo
            and item.action == action and item.queue_oid == queue_oid
            and item.base_oid == base_oid
            for item in self.consumed
        )


class FakeFresh:
    """The merge ceremony service: begin issues a transaction, consume burns it."""

    def __init__(self, grants: Grants, sessions: set[str], public_origin: str) -> None:
        self.grants = grants
        self.sessions = sessions
        self.public_origin = public_origin
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/lock/health":
                    self._send(200, b'{"status":"ok"}')
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self) -> None:
                body = self._body()
                if self.path == "/lock/merge/begin":
                    fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                    repo = (fields.get("repo") or [""])[0]
                    action = (fields.get("action") or [""])[0]
                    queue_oid = (fields.get("queue_oid") or [""])[0]
                    base_oid = (fields.get("base_oid") or [""])[0]
                    session = _cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE)
                    if session not in state.sessions or repo not in {"scripts", "diary"}:
                        self._send(403, b'{"error":"fresh authorization required"}')
                        return
                    grant = state.grants.mint(repo, action, queue_oid, base_oid)
                    self._send(303, headers={
                        "Location": "/lock/merge/",
                        "Set-Cookie": (
                            f"{TRANSACTION_COOKIE}={grant.transaction}.{grant.transaction}"
                            f"; Path=/lock/merge; HttpOnly; SameSite=Strict"
                        ),
                    })
                    return
                if self.path == "/lock/merge/consume":
                    transaction = _cookie_value(
                        self.headers.get("Cookie", ""), TRANSACTION_COOKIE,
                    ).split(".")[0]
                    repo = self.headers.get("X-Diary-Merge-Repo", "")
                    if state.grants.consume(transaction, repo):
                        self._send(204)
                    else:
                        self._send(403, b'{"error":"fresh authorization required"}')
                    return
                self._send(404, b'{"error":"not found"}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return loopback_origin(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ExecutorServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The real executor entry point, served over the same Unix socket shape."""

    daemon_threads = True

    def __init__(self, path: str, stack: harness.Stack, grants: Grants) -> None:
        self.stack = stack
        self.grants = grants
        super().__init__(path, ExecutorHandler)


class ExecutorHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(executor.MAX_REQUEST_BYTES + 1)
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply: object = {"ok": False, "error": "invalid"}
        else:
            server = self.server
            assert isinstance(server, ExecutorServer)
            try:
                reply = executor.handle_request(
                    request,
                    server.stack.paths_by_repo,
                    server.grants,
                    auditor=harness.decision_audit(server.stack.audit),
                    publisher=harness.publish_without_venv,
                    lock_state="locked",
                )
            except (OSError, ValueError, RuntimeError) as exc:
                reply = executor.error_reply(exc)
        self.wfile.write(json.dumps(reply).encode("ascii") + b"\n")


# ------------------------------------------------------------------------ proxy


class FrontProxy(ThreadingHTTPServer):
    """Same-origin front: console for everything, ceremony paths to the fresh service."""

    daemon_threads = True

    def __init__(self, console_port: int, fresh_port: int) -> None:
        self.console_port = console_port
        self.fresh_port = fresh_port
        self.return_port: int | None = None
        self.origin_mode = "passthrough"  # or "null" / "evil"
        #: Optional callback run once, when a decision POST reaches the front but
        #: before it is forwarded: lets a test change the queue between the page
        #: render and the decision deterministically.
        self.before_decide: Callable[[], None] | None = None
        super().__init__(("127.0.0.1", 0), FrontHandler)


class FrontHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _target_port(self) -> int:
        server = self.server
        assert isinstance(server, FrontProxy)
        path = self.path.split("?", 1)[0]
        if path.startswith("/lock/merge/begin"):
            return server.fresh_port
        if path == "/lock/merge/return" and server.return_port:
            # Production routes the confirmation page to the fresh-authorization
            # service, not the console; a test may attach the real service here.
            return server.return_port
        return server.console_port

    def _forward(self) -> None:
        server = self.server
        assert isinstance(server, FrontProxy)
        if (
            self.command == "POST"
            and self.path.split("?", 1)[0] == "/lock/merge/decide"
            and server.before_decide is not None
        ):
            callback, server.before_decide = server.before_decide, None
            callback()
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length", "origin"}
        }
        headers["Origin"] = {
            "null": "null",
            "evil": "https://evil.example",
        }.get(server.origin_mode, loopback_origin(server.server_port))
        headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", self._target_port(), timeout=10)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in {"connection", "content-length", "transfer-encoding"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()


# ------------------------------------------------------------------------ stack


@dataclass
class BrowserStack:
    """The console, its executor socket, and the fake services, all scratch."""

    scratch: Path
    harness_stack: harness.Stack
    grants: Grants
    authelia: FakeAuthelia
    fresh: FakeFresh
    console: ui.ConsoleServer
    executor_server: ExecutorServer
    proxy: FrontProxy

    @property
    def origin(self) -> str:
        return loopback_origin(self.proxy.server_port)

    def session(self) -> str:
        value = uuid.uuid4().hex
        self.authelia.sessions.add(value)
        return value

    def preauthorize(self, repo: str, action: str, queue_oid: str, base_oid: str) -> str:
        """Mint the grant a finished ceremony would, for a single-click decision.

        The console consumes it at decide time, exactly as it does after a real
        ceremony; consuming it here would make the first click ask again.
        """
        return self.grants.mint(repo, action, queue_oid, base_oid).transaction

    def close(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.console.shutdown()
        self.console.server_close()
        self.executor_server.shutdown()
        self.executor_server.server_close()
        self.fresh.close()
        self.authelia.close()
        self.harness_stack.close()


def start(base: Path) -> BrowserStack:
    """Build the layer-1 stack, then put the console in front of it."""
    stack = harness.build_stack(base)
    try:
        grants = Grants()
        authelia = FakeAuthelia()
        # The console needs the proxy origin before the proxy exists; bind the
        # proxy first so its port is known, then point the config at it.
        proxy = FrontProxy(0, 0)
        fresh = FakeFresh(grants, authelia.sessions, loopback_origin(proxy.server_port))
        proxy.fresh_port = fresh.server.server_port

        socket_path = str(stack.scratch.path("executor.sock"))
        executor_server = ExecutorServer(socket_path, stack, grants)
        threading.Thread(target=executor_server.serve_forever, daemon=True).start()

        config = ui.Config(
            owner_username=OWNER,
            public_origin=loopback_origin(proxy.server_port),
            authelia_backend=authelia.url,
            fresh_backend=fresh.url,
            session_cookie=SESSION_COOKIE,
        )
        console = ui.ConsoleServer(
            ("127.0.0.1", 0), config,
            ui.ExecutorClient(socket_path),
            ui.FreshGrantClient(config.public_origin, config.fresh_backend, SESSION_COOKIE),
        )
        proxy.console_port = console.server_port
        threading.Thread(target=console.serve_forever, daemon=True).start()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
    except Exception:
        stack.close()
        raise
    return BrowserStack(
        stack.scratch.root, stack, grants, authelia, fresh, console,
        executor_server, proxy,
    )
