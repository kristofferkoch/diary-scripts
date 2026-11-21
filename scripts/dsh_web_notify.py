#!/usr/bin/env python3
"""Send a web UI's login details to Signal note-to-self.

Works for the three web UIs behind Caddy:

* ``dsh-web`` — random token per process start (can't be disabled or pinned),
  printed as a login URL with ``?token=``. Phone bookmarks die on every
  restart, which is why this exists at all.
* ``kimi-web`` — persistent bearer token, printed as ``#token=`` (a URL
  *fragment*, which the browser never sends to the server). The link is
  stable, but sending it on restart is still the quickest way to confirm the
  service came back.
* ``opencode-web`` — HTTP Basic auth with a *static* password. Nothing is
  printed to the journal (a startup warning appears only when the password is
  missing), so this mode reads the password out of the unit's env file
  (``--password-from``) and sends the credentials next to the plain link.
  There is no token to rotate while the server is down, which is why the
  bookmark survives restarts.

The tool polls the unit's journal for the URL printed by the current boot,
rewrites the loopback authority to the public Caddy/tailnet base
(``hosts.dsh_web`` / ``hosts.kimi_web`` / ``hosts.opencode_web`` in the
root-owned ``/etc/diary/hosts.toml``), and sends it to the user's own
note-to-self chat via the signal-cli daemon's JSON-RPC socket.

The socket call is a plain JSON-RPC *client* request — it runs no receive
loop of its own, so it does not violate the "never a second signal-cli
receive" rule (network/docs/signal-cli-mirror.md). The daemon pushes receive
notifications to all connected clients, so responses are matched by id and
unrelated notifications are skipped.

Runs from ``dsh-web-token.service`` (``WantedBy=dsh-web.service``), i.e.
fires automatically on every start/restart of the web UI. Manual run:

    uv run --project diary-scripts dsh-web-notify
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit

from mail_reader.config import dsh_web_url, kimi_web_url, opencode_web_url

DEFAULT_UNIT = "dsh-web"
TOKEN_RE = re.compile(r"https?://\S*?token=\S+")

# Units that authenticate with HTTP Basic + a static password instead of a
# token URL in the journal. The username is fixed by the server, only the
# password is configurable.
BASIC_AUTH_UNITS = frozenset({"opencode-web"})
BASIC_AUTH_USER = "opencode"
DEFAULT_PASSWORD_KEY = "OPENCODE_SERVER_PASSWORD"


class UnitDefaults(TypedDict):
    base: Callable[[], str]
    label: str


# Per-unit defaults: public base + human label for the Signal message.
UNIT_DEFAULTS: dict[str, UnitDefaults] = {
    "dsh-web": {"base": dsh_web_url, "label": "dsh web"},
    "kimi-web": {"base": kimi_web_url, "label": "kimi web"},
    "opencode-web": {"base": opencode_web_url, "label": "opencode web"},
}

DEFAULT_SOCKET_ENV = "SIGNAL_CLI_SOCKET"


def default_socket() -> str:
    """signal-cli JSON-RPC socket, same resolution as signal_capture."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.environ.get(DEFAULT_SOCKET_ENV, f"{runtime}/signal-cli/socket")


def _scope(args: list[str], scope: str) -> list[str]:
    """Insert --user for user-scope units (system units take no flag)."""
    return args[:1] + (["--user"] if scope == "user" else []) + args[1:]


def _invocation_id(unit: str, scope: str) -> str:
    """Current InvocationID of the running service — scopes the journal
    query to exactly this boot, so a token printed before this script got
    scheduled is still found (and stale tokens from older boots never are)."""
    out = subprocess.run(
        _scope(["systemctl", "show", unit,
                "--property=InvocationID", "--value"], scope),
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _journal_invocation(invocation_id: str, unit: str, scope: str) -> str:
    out = subprocess.run(
        _scope(["journalctl", "-u", unit,
                f"_SYSTEMD_INVOCATION_ID={invocation_id}",
                "-o", "cat", "--no-pager"], scope),
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def find_token_url(unit: str = DEFAULT_UNIT, scope: str = "user",
                   timeout: float = 60.0, poll: float = 1.0) -> str | None:
    """Latest token login URL printed by the *current* dsh-web boot, polling
    up to `timeout` seconds (the URL lands a moment after the service forks).
    Returns None when the service isn't running or no URL shows up in time."""
    invocation_id = _invocation_id(unit, scope)
    if not invocation_id:
        return None
    deadline = time.monotonic() + timeout
    while True:
        matches = TOKEN_RE.findall(_journal_invocation(invocation_id, unit, scope))
        if matches:
            return matches[-1]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def public_url(login_url: str, base: str) -> str:
    """Swap the loopback scheme+authority of the journal URL for the public
    base, keeping path and query (the token) intact."""
    b = urlsplit(base)
    p = urlsplit(login_url)
    return urlunsplit((b.scheme, b.netloc, p.path or "/", p.query, p.fragment))


def env_value(path: str, key: str) -> str:
    """Last ``key=value`` in a shell-style env file (comments and quotes are
    tolerated). Returns ``""`` when the key is absent — the caller decides
    whether that is fatal."""
    value = ""
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, rest = line.partition("=")
            if name.strip() == key:
                value = rest.strip().strip('"').strip("'")
    return value


def wait_for_http(url: str, timeout: float = 60.0, poll: float = 1.0) -> bool:
    """Poll until ``url`` answers with *any* HTTP status.

    A 401 counts as ready: for a Basic-auth UI it means the server is
    listening and already enforcing the password, which is exactly what the
    message is about to report.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            urlrequest.urlopen(url, timeout=5).close()
            return True
        except urlerror.HTTPError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)


def basic_auth_message(label: str, base: str, user: str, password: str) -> str:
    """Signal body for a static-password UI: link plus the credentials."""
    return (
        f"{label} (re)started — login from phone:\n{base}/"
        f"\nuser: {user}  password: {password}"
    )


def signal_note(message: str, socket_path: str | None = None,
                recv_timeout: float = 15.0) -> dict:
    """Send `message` to note-to-self via the daemon's JSON-RPC socket.
    Returns the result dict; raises RuntimeError on a JSON-RPC error."""
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "send",
        "params": {"noteToSelf": True, "message": message},
    }
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(recv_timeout)
        sock.connect(socket_path or default_socket())
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("signal-cli socket closed without a reply")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("id") != 1:
                    continue  # pushed receive notification, not our reply
                if "error" in msg:
                    raise RuntimeError(f"signal-cli JSON-RPC error: {msg['error']}")
                return msg.get("result") or {}
    finally:
        sock.close()


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--unit", default=DEFAULT_UNIT,
                   help="systemd unit whose journal carries the URL "
                        "(dsh-web, kimi-web, opencode-web)")
    p.add_argument("--password-from", default=None,
                   help="env file holding the Basic-auth password; required "
                        "for units in BASIC_AUTH_UNITS (opencode-web)")
    p.add_argument("--password-key", default=DEFAULT_PASSWORD_KEY,
                   help=f"variable to read from --password-from "
                        f"(default: {DEFAULT_PASSWORD_KEY})")
    p.add_argument("--probe", default=None,
                   help="HTTP URL polled until it answers (any status) before "
                        "sending; a 401 counts as ready")
    p.add_argument("--scope", choices=("user", "system"), default="user",
                   help="current agent user units, or LATER-03 system units")
    p.add_argument("--base-url", default=None,
                   help="public base URL (default: hosts.dsh_web / "
                        "hosts.kimi_web for the chosen unit)")
    p.add_argument("--label", default=None,
                   help="name used in the Signal message")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="seconds to wait for the token URL in the journal")
    p.add_argument("--socket", default=None,
                   help="signal-cli JSON-RPC socket (default $SIGNAL_CLI_SOCKET "
                        "or $XDG_RUNTIME_DIR/signal-cli/socket)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the message instead of sending it")
    args = p.parse_args()

    defaults = UNIT_DEFAULTS.get(args.unit, {"base": dsh_web_url, "label": args.unit})
    base = args.base_url or defaults["base"]()
    label = args.label or defaults["label"]

    if args.probe and not wait_for_http(args.probe, timeout=args.timeout):
        print(f"!! '{args.unit}' is not answering {args.probe} within "
              f"{args.timeout:.0f}s", file=sys.stderr)
        sys.exit(1)

    if args.unit in BASIC_AUTH_UNITS:
        if not args.password_from:
            print(f"!! --password-from is required for '{args.unit}'",
                  file=sys.stderr)
            sys.exit(2)
        password = env_value(args.password_from, args.password_key)
        if not password:
            print(f"!! no {args.password_key} in {args.password_from}",
                  file=sys.stderr)
            sys.exit(2)
        message = basic_auth_message(label, base, BASIC_AUTH_USER, password)
        if args.dry_run:
            print(message)
            return
        signal_note(message, socket_path=args.socket)
        print(f"sent to note-to-self: {base}/ ({BASIC_AUTH_USER} + password)")
        return

    url = find_token_url(unit=args.unit, scope=args.scope, timeout=args.timeout)
    if url is None:
        print(f"!! no token URL from a running '{args.unit}' within "
              f"{args.timeout:.0f}s", file=sys.stderr)
        sys.exit(1)

    link = public_url(url, base)
    message = f"{label} (re)started — login from phone:\n{link}"
    if args.dry_run:
        print(message)
        return

    signal_note(message, socket_path=args.socket)
    print(f"sent to note-to-self: {link}")


if __name__ == "__main__":
    main()
