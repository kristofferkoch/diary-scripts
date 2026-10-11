#!/usr/bin/env python3
"""Send the dsh web login URL (with its per-boot token) to Signal note-to-self.

The dsh web UI has mandatory token auth: a random token per process start
(can't be disabled or pinned), printed to the journal as a login URL with
``?token=``. Phone bookmarks therefore die on every restart. This tool polls
the service's journal for the URL printed by the current boot, rewrites the
loopback authority to the public Caddy/tailnet base (``hosts.dsh_web`` in
config, env override ``DSH_WEB_URL``), and sends it to the user's own
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
from urllib.parse import urlsplit, urlunsplit

from mail_reader.config import dsh_web_url

UNIT = "dsh-web"
TOKEN_RE = re.compile(r"https?://\S*?token=\S+")

DEFAULT_SOCKET_ENV = "SIGNAL_CLI_SOCKET"


def default_socket() -> str:
    """signal-cli JSON-RPC socket, same resolution as signal_capture."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.environ.get(DEFAULT_SOCKET_ENV, f"{runtime}/signal-cli/socket")


def _invocation_id() -> str:
    """Current InvocationID of the running service — scopes the journal
    query to exactly this boot, so a token printed before this script got
    scheduled is still found (and stale tokens from older boots never are)."""
    out = subprocess.run(
        ["systemctl", "--user", "show", UNIT,
         "--property=InvocationID", "--value"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _journal_invocation(invocation_id: str) -> str:
    out = subprocess.run(
        ["journalctl", "--user", "-u", UNIT,
         f"_SYSTEMD_INVOCATION_ID={invocation_id}", "-o", "cat", "--no-pager"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def find_token_url(timeout: float = 60.0, poll: float = 1.0) -> str | None:
    """Latest token login URL printed by the *current* dsh-web boot, polling
    up to `timeout` seconds (the URL lands a moment after the service forks).
    Returns None when the service isn't running or no URL shows up in time."""
    invocation_id = _invocation_id()
    if not invocation_id:
        return None
    deadline = time.monotonic() + timeout
    while True:
        matches = TOKEN_RE.findall(_journal_invocation(invocation_id))
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
    return urlunsplit((b.scheme, b.netloc, p.path or "/", p.query, ""))


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
    p.add_argument("--timeout", type=float, default=60.0,
                   help="seconds to wait for the token URL in the journal")
    p.add_argument("--socket", default=None,
                   help="signal-cli JSON-RPC socket (default $SIGNAL_CLI_SOCKET "
                        "or $XDG_RUNTIME_DIR/signal-cli/socket)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the message instead of sending it")
    args = p.parse_args()

    url = find_token_url(timeout=args.timeout)
    if url is None:
        print(f"!! no token URL from a running '{UNIT}' within {args.timeout:.0f}s",
              file=sys.stderr)
        sys.exit(1)

    link = public_url(url, dsh_web_url())
    message = f"dsh web (re)started — login from phone:\n{link}"
    if args.dry_run:
        print(message)
        return

    signal_note(message, socket_path=args.socket)
    print(f"sent to note-to-self: {link}")


if __name__ == "__main__":
    main()
