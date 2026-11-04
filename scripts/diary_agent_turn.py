#!/usr/bin/python3
"""Connect DSH/Kimi lifecycle hooks to root-owned per-turn systemd scopes."""
from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

DEFAULT_SOCKET = "/run/diary-lock/turns.sock"
MAX_HOOK_BYTES = 65536


def request(socket_path: str, value: dict[str, str], timeout: float = 10.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_HOOK_BYTES:
                raise ValueError("turn response is too large")
        reply = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot use turn socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error", "turn request failed") if isinstance(reply, dict) else "turn request failed"
        raise ValueError(error)
    return reply


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    result.add_argument("--socket", default=DEFAULT_SOCKET)
    result.add_argument("--runtime", required=True, choices=("dsh", "kimi"))
    result.add_argument("command", choices=("start", "finish"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            raise ValueError("hook payload is too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str):
            raise TypeError("hook payload has no session ID")
        reply = request(args.socket, {
            "cmd": args.command,
            "runtime": args.runtime,
            "session": payload["session_id"],
        })
        if args.command == "start" and reply.get("scope"):
            print(reply["scope"])
        return 0
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"diary-agent-turn: {exc}", file=sys.stderr)
        if args.command == "start":
            return 2
        # A failing Stop hook must never ask the model to continue the turn.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
