#!/usr/bin/env python3
"""Send a web UI's login URL (with its token) to Signal note-to-self.

Works for both web UIs behind Caddy:

* ``dsh-web`` — random token per process start (can't be disabled or pinned),
  printed as a login URL with ``?token=``. Phone bookmarks die on every
  restart, which is why this exists at all.
* ``kimi-web`` — persistent bearer token, printed as ``#token=`` (a URL
  *fragment*, which the browser never sends to the server). The link is
  stable, but sending it on restart is still the quickest way to confirm the
  service came back.

The tool polls the unit's journal for the URL printed by the current boot,
rewrites the loopback authority to the public Caddy/tailnet base
(``hosts.dsh_web`` / ``hosts.kimi_web`` in the root-owned
``/etc/diary/hosts.toml``), and sends it to the user's own
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
from urllib.parse import urlsplit, urlunsplit

from mail_reader.config import dsh_web_url, kimi_web_url

DEFAULT_UNIT = "dsh-web"
TOKEN_RE = re.compile(r"https?://\S*?token=\S+")


class UnitDefaults(TypedDict):
    base: Callable[[], str]
    label: str


# Per-unit defaults: public base + human label for the Signal message.
UNIT_DEFAULTS: dict[str, UnitDefaults] = {
    "dsh-web": {"base": dsh_web_url, "label": "dsh web"},
    "kimi-web": {"base": kimi_web_url, "label": "kimi web"},
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
                        "(dsh-web, kimi-web)")
    p.add_argument("--scope", choices=("user", "system"), default="user",
                   help="user units run as kek today; system units after the "
                        "agent-uid split")
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
