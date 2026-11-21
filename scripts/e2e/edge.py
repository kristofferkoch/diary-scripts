"""Layer 3: real Caddy and passkey authentication in front of the merge console.

Repository publication still uses the layer-2 fake grant service so those tests
stay focused on the console and executor. The separate live passkey suite drives
real single-use grants through the same authorization service.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from scripts import diary_fresh_unlock as fresh
from scripts import diary_lock_console_ui as ui
from scripts.e2e import browser, harness

CADDY = "/usr/bin/caddy"
LOOPBACK = "127.0.0.1"
HOST = "console.example"
OWNER = "owner"
ENROLLMENT_CODE = "edge-enrollment-code"
SESSION_COOKIE = "diary_lock_session"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def origin_for(port: int, scheme: str) -> str:
    return scheme + "://" + HOST + ":" + str(port)


def loopback(port: int) -> str:
    return "http://" + LOOPBACK + ":" + str(port)


class _SNIConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, sni: str, **kwargs) -> None:
        self._sni_context = kwargs.get("context") or ssl.create_default_context()
        super().__init__(host, port, **kwargs)
        self._sni = sni

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._sni_context.wrap_socket(self.sock, server_hostname=self._sni)


def _wait_http(
    host: str,
    port: int,
    path: str,
    scheme: str,
    timeout: float = 30.0,
    host_header: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    context = ssl._create_unverified_context()
    while time.monotonic() < deadline:
        connection = (
            http.client.HTTPSConnection
            if scheme == "https"
            else http.client.HTTPConnection
        )
        try:
            if scheme == "https":
                conn = _SNIConnection(
                    host,
                    port,
                    host_header.split(":", 1)[0] if host_header else host,
                    timeout=2,
                    context=context,
                )
            else:
                conn = connection(host, port, timeout=2)
            headers = {"Host": host_header} if host_header else {}
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status < 500:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{scheme}://{host}:{port}{path} did not become ready")


@dataclass
class EdgeStack:
    root: Path
    caddy_process: subprocess.Popen
    caddy_port: int
    auth: fresh.FreshUnlockServer
    auth_thread: threading.Thread
    console: ui.ConsoleServer
    executor_server: browser.ExecutorServer
    fresh: browser.FakeFresh
    grants: browser.Grants
    harness_stack: harness.Stack
    scratch: harness.Scratch

    @property
    def origin(self) -> str:
        return origin_for(self.caddy_port, "https")

    def request(
        self,
        method: str,
        path: str,
        *,
        cookie: str = "",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        context = ssl._create_unverified_context()
        conn = _SNIConnection(
            "127.0.0.1", self.caddy_port, HOST, timeout=timeout, context=context
        )
        merged = {"Host": f"{HOST}:{self.caddy_port}", **(headers or {})}
        if cookie:
            merged["Cookie"] = cookie
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        conn.close()
        return result

    def preauthorize(
        self, repo: str, action: str, queue_oid: str, base_oid: str
    ) -> str:
        return self.grants.mint(repo, action, queue_oid, base_oid).transaction

    def close(self) -> None:
        self.caddy_process.terminate()
        try:
            self.caddy_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.caddy_process.kill()
        self.console.shutdown()
        self.console.server_close()
        self.executor_server.shutdown()
        self.executor_server.server_close()
        self.fresh.close()
        self.auth.shutdown()
        self.auth.server_close()
        self.auth_thread.join(timeout=5)
        self.harness_stack.close()


def _certificate(root: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(root / "tls-key.pem"),
            "-out",
            str(root / "tls-cert.pem"),
            "-subj",
            f"/CN={HOST}",
            "-addext",
            f"subjectAltName=DNS:{HOST}",
        ],
        capture_output=True,
        check=True,
    )


def _caddyfile(
    root: Path,
    auth_port: int,
    fresh_port: int,
    console_port: int,
    caddy_port: int,
) -> str:
    forward = (
        f"forward_auth 127.0.0.1:{auth_port} {{\n"
        "\t\t\turi /lock/auth/forward-auth\n"
        "\t\t\tcopy_headers Remote-User Remote-Groups Remote-Name\n"
        "\t\t}"
    )
    return f"""{{
	auto_https off
	admin off
}}
{origin_for(caddy_port, "https")} {{
	bind 127.0.0.1
	tls {root / "tls-cert.pem"} {root / "tls-key.pem"}
	handle /lock/auth/* {{
		reverse_proxy 127.0.0.1:{auth_port}
	}}
	handle /lock/enroll/* {{
		reverse_proxy 127.0.0.1:{auth_port}
	}}
	handle /lock/merge/begin {{
		reverse_proxy 127.0.0.1:{fresh_port}
	}}
	handle /lock/* {{
		{forward}
		reverse_proxy 127.0.0.1:{console_port}
	}}
}}
"""


def run_driver(stack: EdgeStack, transaction: str = "", repo: str = "scripts") -> dict:
    """Drive the real browser through passkey enrollment and a decision."""
    core = os.environ.get("E2E_PLAYWRIGHT_CORE")
    chrome = os.environ.get("E2E_CHROMIUM")
    if not (core and chrome):
        raise RuntimeError(
            "E2E_PLAYWRIGHT_CORE and E2E_CHROMIUM must point at the browser tooling"
        )
    driver = Path(__file__).with_name("edge_driver.mjs")
    xdg = stack.scratch.directory("xdg")
    env = {
        **os.environ,
        "E2E_PLAYWRIGHT_CORE": core,
        "E2E_CHROMIUM": chrome,
        "E2E_EDGE_HOST": HOST,
        "E2E_ENROLLMENT_CODE": ENROLLMENT_CODE,
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_DATA_HOME": str(xdg / "data"),
    }
    proc = subprocess.run(
        ["node", str(driver), stack.origin, HOST, transaction or "-", repo],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"browser driver failed: {proc.stderr.strip()[-800:]}")
    return json.loads(proc.stdout)


def start(base: Path) -> EdgeStack:
    stack = harness.build_stack(base)
    root = stack.scratch.path("edge")
    root.mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    caddy = None
    auth = None
    try:
        _certificate(root)
        caddy_port = free_port()
        origin = origin_for(caddy_port, "https")
        config = fresh.Config(
            owner_username=OWNER,
            public_origin=origin,
            session_cookie=SESSION_COOKIE,
            transaction_ttl=300,
            session_ttl=600,
        )
        auth = fresh.FreshUnlockServer(
            ("127.0.0.1", 0),
            config,
            root / "state" / "auth.sqlite3",
            enrollment_code=ENROLLMENT_CODE,
        )
        auth_thread = threading.Thread(target=auth.serve_forever, daemon=True)
        auth_thread.start()
        _wait_http("127.0.0.1", auth.server_port, "/lock/health", "http")

        def valid_session(token: str) -> bool:
            try:
                auth.passkeys.verify_owner(f"{SESSION_COOKIE}={token}")
                return True
            except fresh.AuthorizationError:
                return False

        grants = browser.Grants()
        fake_fresh = browser.FakeFresh(grants, valid_session, origin)
        socket_path = str(stack.scratch.path("executor.sock"))
        executor_server = browser.ExecutorServer(socket_path, stack, grants)
        threading.Thread(target=executor_server.serve_forever, daemon=True).start()

        ui_config = ui.Config(
            owner_username=OWNER,
            public_origin=origin,
            auth_backend=loopback(auth.server_port),
            fresh_backend=fake_fresh.url,
            session_cookie=SESSION_COOKIE,
        )
        console = ui.ConsoleServer(
            ("127.0.0.1", 0),
            ui_config,
            ui.ExecutorClient(socket_path),
            ui.FreshGrantClient(origin, fake_fresh.url, SESSION_COOKIE),
        )
        threading.Thread(target=console.serve_forever, daemon=True).start()

        (root / "Caddyfile").write_text(
            _caddyfile(
                root,
                auth.server_port,
                fake_fresh.server.server_port,
                console.server_port,
                caddy_port,
            )
        )
        caddy = subprocess.Popen(
            [
                CADDY,
                "run",
                "--config",
                str(root / "Caddyfile"),
                "--adapter",
                "caddyfile",
            ],
            stdout=(root / "caddy.log").open("wb"),
            stderr=subprocess.STDOUT,
        )
        _wait_http(
            "127.0.0.1",
            caddy_port,
            "/lock/auth/",
            "https",
            host_header=f"{HOST}:{caddy_port}",
        )
    except Exception:
        if caddy is not None:
            caddy.terminate()
        if auth is not None:
            auth.shutdown()
            auth.server_close()
        stack.close()
        raise
    return EdgeStack(
        root=root,
        caddy_process=caddy,
        caddy_port=caddy_port,
        auth=auth,
        auth_thread=auth_thread,
        console=console,
        executor_server=executor_server,
        fresh=fake_fresh,
        grants=grants,
        harness_stack=stack,
        scratch=stack.scratch,
    )
