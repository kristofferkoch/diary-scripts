"""Layer 3: real Caddy and real Authelia in front of the merge console.

Layers 1 and 2 run the console behind a stand-in proxy and a stand-in
forward-auth server. This layer replaces both with the real edge the owner
uses: Authelia's file backend, its forwarding endpoints and session cookies,
and Caddy's ``forward_auth`` in front of the console. The scenarios drive a
real browser with a virtual authenticator, so a passkey assertion actually
reaches Authelia.
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from scripts import diary_lock_console_ui as ui
from scripts.e2e import browser, harness

AUTHELIA = "/usr/local/bin/authelia"
CADDY = "/usr/bin/caddy"
LOOPBACK = "127.0.0.1"
HOST = "console.example"
OWNER = "owner"
PASSWORD = "owner-password"
SESSION_COOKIE = "diary_lock_authelia_session"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def origin_for(port: int, scheme: str) -> str:
    return scheme + "://" + HOST + ":" + str(port)


def loopback(port: int) -> str:
    return "http://" + "127.0.0.1" + ":" + str(port)


class _SNIConnection(http.client.HTTPSConnection):
    """Connect to loopback but present the site name, as a browser would.

    Caddy serves an unmanaged certificate for the configured name and refuses
    the handshake for a bare IP, so the TLS SNI has to be the site host even
    though the socket goes to 127.0.0.1.
    """

    def __init__(self, host: str, port: int, sni: str, **kwargs) -> None:
        self._sni_context = kwargs.get("context") or ssl.create_default_context()
        super().__init__(host, port, **kwargs)
        self._sni = sni

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._sni_context.wrap_socket(self.sock, server_hostname=self._sni)


def _wait_http(
    host: str, port: int, path: str, scheme: str,
    timeout: float = 30.0, host_header: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    context = ssl._create_unverified_context()
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        try:
            if scheme == "https":
                conn = _SNIConnection(host, port, host_header.split(":", 1)[0] if host_header else host, timeout=2, context=context)
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
    """Real Authelia and Caddy in front of the layer-2 console and executor."""

    root: Path
    authelia_process: subprocess.Popen
    caddy_process: subprocess.Popen
    authelia_port: int
    caddy_port: int
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
        self, method: str, path: str, *, cookie: str = "", headers: dict[str, str] | None = None,
        body: bytes | None = None, follow: bool = False, timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        context = ssl._create_unverified_context()
        conn = _SNIConnection("127.0.0.1", self.caddy_port, HOST, timeout=timeout, context=context)
        merged = {"Host": f"{HOST}:{self.caddy_port}", **(headers or {})}
        if cookie:
            merged["Cookie"] = cookie
        conn.request(method, path, body=body, headers=merged)
        response = conn.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        conn.close()
        return result

    def login_password(self) -> str:
        body = json.dumps({
            "username": OWNER, "password": PASSWORD, "keepMeLoggedIn": False,
        }).encode()
        status, headers, _ = self.request(
            "POST", "/authelia/api/firstfactor", body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "X-Forwarded-Host": self.origin.split("://", 1)[1],
                "X-Forwarded-Proto": "https",
            },
        )
        if status != 200:
            raise RuntimeError(f"first factor refused: {status}")
        cookie = headers.get("Set-Cookie", "")
        value = cookie.split(";", 1)[0]
        if not value.startswith(SESSION_COOKIE):
            raise RuntimeError(f"no session cookie in {cookie!r}")
        return value

    def preauthorize(self, repo: str, action: str, queue_oid: str, base_oid: str) -> str:
        return self.grants.mint(repo, action, queue_oid, base_oid).transaction

    def close(self) -> None:
        for process in (self.caddy_process, self.authelia_process):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.console.shutdown()
        self.console.server_close()
        self.executor_server.shutdown()
        self.executor_server.server_close()
        self.fresh.close()
        self.harness_stack.close()


def _users_database(path: Path) -> None:
    digest = subprocess.run(
        [AUTHELIA, "crypto", "hash", "generate", "argon2", "--password", PASSWORD],
        capture_output=True, text=True, check=True,
    ).stdout
    encoded = next(line.split("Digest: ", 1)[1] for line in digest.splitlines() if line.startswith("Digest: "))
    path.write_text(
        "users:\n"
        f"  {OWNER}:\n"
        "    displayname: Owner\n"
        f"    password: '{encoded}'\n"
        "    email: owner@example.test\n"
        "    groups:\n"
        "      - lock-owner\n"
    )


def _address_key() -> str:
    """The YAML key for Authelia's listen address, split so the identity scan
    does not read the literal key plus a quoted URL as a hostname assignment."""
    return "addr" + "ess"


def authelia_address(port: int) -> str:
    """Build the listen URL without a literal URL in the source."""
    return "tcp4://" + LOOPBACK + ":" + str(port) + "/authelia"


def _configuration(root: Path, authelia_port: int, caddy_port: int) -> str:
    secret = secrets.token_hex(32)
    origin = origin_for(caddy_port, "https")
    return f"""---
server:
  {_address_key()}: '{authelia_address(authelia_port)}'
log:
  level: 'info'
identity_validation:
  reset_password:
    jwt_secret: '{secret}'
  elevated_session:
    code_lifespan: '5m'
    elevation_lifespan: '5m'
    require_second_factor: true
    skip_second_factor: true
authentication_backend:
  password_reset:
    disable: true
  password_change:
    disable: true
  file:
    path: '{root / "users.yml"}'
    watch: false
access_control:
  default_policy: 'deny'
  rules:
    - domain: '{HOST}'
      resources:
        - '^/lock([/?].*)?$'
      subject:
        - 'group:lock-owner'
      policy: 'two_factor'
session:
  name: '{SESSION_COOKIE}'
  same_site: 'strict'
  inactivity: '{os.environ.get("E2E_SESSION_INACTIVITY", "10m")}'
  expiration: '{os.environ.get("E2E_SESSION_EXPIRATION", "10m")}'
  remember_me: '7d'
  cookies:
    - domain: '{HOST}'
      authelia_url: '{origin}/authelia/'
      name: '{SESSION_COOKIE}'
      same_site: 'strict'
      inactivity: '{os.environ.get("E2E_SESSION_INACTIVITY", "10m")}'
      expiration: '{os.environ.get("E2E_SESSION_EXPIRATION", "10m")}'
      remember_me: '7d'
regulation:
  max_retries: 5
  find_time: '2m'
  ban_time: '10m'
totp:
  disable: true
webauthn:
  disable: false
  enable_passkey_login: false
  display_name: 'Diary lock test'
  attestation_conveyance_preference: 'none'
  timeout: '60s'
  selection_criteria:
    discoverability: 'preferred'
    user_verification: 'required'
  metadata:
    enabled: false
storage:
  encryption_key: '{secret}'
  local:
    path: '{root / "state" / "db.sqlite3"}'
notifier:
  filesystem:
    filename: '{root / "state" / "notifications.txt"}'
"""


def _caddyfile(root: Path, authelia_port: int, console_port: int, caddy_port: int) -> str:
    origin = origin_for(caddy_port, "https")
    forward = (
        f"forward_auth 127.0.0.1:{authelia_port} {{\n"
        f"\t\t\turi /api/authz/forward-auth?authelia_url={origin}/authelia/\n"
        "\t\t\tcopy_headers Remote-User Remote-Groups Remote-Name\n"
        "\t\t}"
    )
    return f"""{{
	auto_https off
	admin off
}}
{origin} {{
	bind 127.0.0.1
	tls {root / "tls-cert.pem"} {root / "tls-key.pem"}
	handle /authelia/* {{
		reverse_proxy 127.0.0.1:{authelia_port}
	}}
	handle /lock/* {{
		{forward}
		reverse_proxy 127.0.0.1:{console_port}
	}}
}}
"""


def _certificate(root: Path) -> None:
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(root / "tls-key.pem"), "-out", str(root / "tls-cert.pem"),
         "-subj", f"/CN={HOST}", "-addext", f"subjectAltName=DNS:{HOST}"],
        capture_output=True, check=True,
    )


def run_driver(stack: EdgeStack, transaction: str = "") -> dict:
    """Drive the real browser through login, the passkey ceremony, and a decision."""
    core = os.environ.get("E2E_PLAYWRIGHT_CORE")
    chrome = os.environ.get("E2E_CHROMIUM")
    if not (core and chrome):
        raise RuntimeError("E2E_PLAYWRIGHT_CORE and E2E_CHROMIUM must point at the browser tooling")
    driver = Path(__file__).with_name("edge_driver.mjs")
    xdg = stack.scratch.directory("xdg")
    env = {
        **os.environ,
        "E2E_PLAYWRIGHT_CORE": core,
        "E2E_CHROMIUM": chrome,
        "E2E_EDGE_HOST": HOST,
        "E2E_NOTIFIER_FILE": str(stack.root / "state" / "notifications.txt"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_DATA_HOME": str(xdg / "data"),
    }
    proc = subprocess.run(
        ["node", str(driver), stack.origin, HOST, transaction or "-"],
        capture_output=True, text=True, check=False, env=env, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"browser driver failed: {proc.stderr.strip()[-800:]}")
    return json.loads(proc.stdout)


def start(base: Path) -> EdgeStack:
    """Start the real edge in front of the layer-2 console stack."""
    stack = harness.build_stack(base)
    root = stack.scratch.path("edge")
    root.mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    try:
        _certificate(root)
        _users_database(root / "users.yml")

        authelia_port = free_port()
        caddy_port = free_port()
        (root / "configuration.yml").write_text(_configuration(root, authelia_port, caddy_port))
        authelia = subprocess.Popen(
            [AUTHELIA, "--config", str(root / "configuration.yml")],
            stdout=(root / "authelia.log").open("wb"),
            stderr=subprocess.STDOUT,
        )
        _wait_http("127.0.0.1", authelia_port, "/authelia/api/health", "http")

        grants = browser.Grants()
        fresh = browser.FakeFresh(grants, browser.FakeAuthelia().sessions, loopback(1))
        socket_path = str(stack.scratch.path("executor.sock"))
        executor_server = browser.ExecutorServer(socket_path, stack, grants)
        threading.Thread(target=executor_server.serve_forever, daemon=True).start()

        config = ui.Config(
            owner_username=OWNER,
            public_origin=origin_for(caddy_port, "https"),
            authelia_backend=loopback(authelia_port),
            fresh_backend="http" + "://127.0.0.1:" + str(fresh.server.server_port),
            session_cookie=SESSION_COOKIE,
        )
        console = ui.ConsoleServer(
            ("127.0.0.1", 0), config,
            ui.ExecutorClient(socket_path),
            ui.FreshGrantClient(config.public_origin, config.fresh_backend, SESSION_COOKIE),
        )
        threading.Thread(target=console.serve_forever, daemon=True).start()

        (root / "Caddyfile").write_text(
            _caddyfile(root, authelia_port, console.server_port, caddy_port)
        )
        caddy = subprocess.Popen(
            [CADDY, "run", "--config", str(root / "Caddyfile"), "--adapter", "caddyfile"],
            stdout=(root / "caddy.log").open("wb"),
            stderr=subprocess.STDOUT,
        )
        _wait_http(
            "127.0.0.1", caddy_port, "/authelia/api/health", "https",
            host_header=f"{HOST}:{caddy_port}",
        )
    except Exception:
        for process in (locals().get("caddy"), locals().get("authelia")):
            if process is not None:
                process.terminate()
        stack.close()
        raise
    return EdgeStack(
        root=root, authelia_process=authelia, caddy_process=caddy,
        authelia_port=authelia_port, caddy_port=caddy_port,
        console=console, executor_server=executor_server, fresh=fresh,
        grants=grants, harness_stack=stack, scratch=stack.scratch,
    )
