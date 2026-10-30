#!/usr/bin/python3
"""signal-notify — a write-only, note-to-self-only proxy in front of signal-cli.

The signal-cli daemon exposes one JSON-RPC socket that can do everything:
subscribe to incoming messages, list accounts, and `send` to *any* recipient.
Anything that can reach that socket can therefore read the whole Signal history
and impersonate the user — the exfiltration leg of the lethal trifecta.

This proxy narrows it to the one thing agent-side services legitimately need:
a note to yourself. It listens on its own socket (default
``/run/signal-notify/socket``) and:

* accepts only ``method == "send"``,
* requires ``params.noteToSelf == true``,
* rejects any addressing field (``recipient``, ``recipients``, ``groupId``,
  …) instead of silently dropping it — a caller that tries to address someone
  else gets a hard error, not a surprise,
* caps message length and rate, so a runaway agent cannot spam the phone,
* never forwards anything upstream that it did not reconstruct itself.

The upstream call is a plain JSON-RPC client request — no receive loop — so the
single-receiver rule (network/docs/signal-cli-mirror.md) is respected.

Run it as the account owner (kek), with the socket directory group-readable by
``diary`` if other UIDs should be able to ask:

    signal-notify-proxy --listen /run/signal-notify/socket
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

MAX_MESSAGE_CHARS = 4000
MAX_MESSAGES_PER_MINUTE = 20
DEFAULT_LISTEN = "/run/signal-notify/socket"

# Fields that address a message somewhere other than note-to-self.
ADDRESSING_FIELDS = (
    "recipient", "recipients", "groupId", "group", "groupName", "username",
    "attachment", "attachments", "quoteTimestamp", "quoteAuthor",
)


class Limiter:
    """Token bucket: burst of `burst`, refilling `burst` per 60 s."""

    def __init__(self, burst: int = MAX_MESSAGES_PER_MINUTE, period: float = 60.0) -> None:
        self.burst = burst
        self.period = period
        self.tokens = float(burst)
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.burst / self.period)
            self.stamp = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


def sanitize(request: dict) -> tuple[dict | None, str]:
    """Return (forwardable_request, "") or (None, reason). Pure, so it is the
    whole security boundary and can be unit-tested without a socket."""
    if not isinstance(request, dict):
        return None, "request must be a JSON object"
    if request.get("method") != "send":
        return None, "only 'send' is available through this proxy"
    params = request.get("params")
    if not isinstance(params, dict):
        return None, "params must be a JSON object"
    if params.get("noteToSelf") is not True:
        return None, "only noteToSelf sends are allowed"
    for field in ADDRESSING_FIELDS:
        if field in params:
            return None, f"'{field}' is not allowed: this proxy only sends to yourself"
    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return None, "message must be a non-empty string"
    if len(message) > MAX_MESSAGE_CHARS:
        return None, f"message longer than {MAX_MESSAGE_CHARS} characters"
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "method": "send",
        "params": {"noteToSelf": True, "message": message},
    }, ""


def forward_ok(upstream: str, request: dict, timeout: float = 15.0) -> tuple[bool, str]:
    """Send one request upstream and wait for the reply with matching id."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(upstream)
        sock.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return False, "signal-cli closed the connection without a reply"
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != request.get("id"):
                    continue  # a pushed receive notification, not our reply
                if "error" in msg:
                    return False, f"signal-cli error: {msg['error']}"
                return True, ""
    except OSError as exc:
        return False, f"upstream unavailable: {exc}"
    finally:
        sock.close()


def handle(conn: socket.socket, upstream: str, limiter: Limiter) -> None:
    try:
        conn.settimeout(30.0)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 1_000_000:
                conn.sendall(b'{"jsonrpc":"2.0","error":{"code":-32700,"message":"request too large"},"id":null}\n')
                return
        line = buf.split(b"\n", 1)[0]
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            conn.sendall(b'{"jsonrpc":"2.0","error":{"code":-32700,"message":"parse error"},"id":null}\n')
            return

        clean, reason = sanitize(request)
        if clean is None:
            print(f"[signal-notify] rejected: {reason}", file=sys.stderr, flush=True)
            reply = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                     "error": {"code": -32600, "message": reason}}
            conn.sendall(json.dumps(reply).encode() + b"\n")
            return

        if not limiter.allow():
            print("[signal-notify] rate limited", file=sys.stderr, flush=True)
            reply = {"jsonrpc": "2.0", "id": clean.get("id"),
                     "error": {"code": -32000, "message": "rate limit exceeded, try again later"}}
            conn.sendall(json.dumps(reply).encode() + b"\n")
            return

        ok, err = forward_ok(upstream, clean)
        if ok:
            conn.sendall(json.dumps({"jsonrpc": "2.0", "id": clean.get("id"), "result": {}}).encode() + b"\n")
            print("[signal-notify] sent note-to-self", file=sys.stderr, flush=True)
        else:
            print(f"[signal-notify] upstream failed: {err}", file=sys.stderr, flush=True)
            conn.sendall(json.dumps({"jsonrpc": "2.0", "id": clean.get("id"),
                                     "error": {"code": -32000, "message": err}}).encode() + b"\n")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--listen", default=os.environ.get("SIGNAL_NOTIFY_SOCKET", DEFAULT_LISTEN))
    p.add_argument("--upstream", default=None,
                   help="signal-cli JSON-RPC socket (default $SIGNAL_CLI_SOCKET or "
                        "$XDG_RUNTIME_DIR/signal-cli/socket)")
    args = p.parse_args(argv)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    upstream = args.upstream or os.environ.get("SIGNAL_CLI_SOCKET") or f"{runtime}/signal-cli/socket"

    listen_dir = os.path.dirname(args.listen) or "."
    os.makedirs(listen_dir, exist_ok=True)
    try:
        os.unlink(args.listen)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.listen)
    os.chmod(args.listen, 0o770)  # group-writable so the `diary` group can ask
    srv.listen(16)

    print(f"[signal-notify] listening on {args.listen} -> {upstream} "
          f"(note-to-self only, {MAX_MESSAGES_PER_MINUTE}/min)", file=sys.stderr, flush=True)

    limiter = Limiter()
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn, upstream, limiter), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        try:
            os.unlink(args.listen)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
