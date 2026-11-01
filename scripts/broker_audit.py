#!/usr/bin/python3
"""Best-effort AUD-04 broker decisions for the trusted diary audit socket.

Every event describes what the broker would do in locked mode. Recording never
changes the broker's response, executes an operation, or retries a request.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct

DEFAULT_SOCKET = "/run/diary-audit/socket"
MAX_RESPONSE_BYTES = 64 * 1024

POLICY = {
    "signal-notify": {
        "getAttachment": ("allow", "read_only"),
        "send": ("would_deny", "external_send"),
    },
    "diary-sync": {
        "ping": ("allow", "read_only"),
        "status": ("allow", "read_only"),
        "start": ("would_deny", "service_start"),
        "sync": ("would_deny", "external_sync"),
    },
    "agent-merge": {
        "diary": ("would_deny", "publication_request"),
        "scripts": ("would_deny", "publication_request"),
    },
    "sudo-broker": {
        "execute": ("would_deny", "privileged_execute"),
    },
}


def peer_credentials(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def peer_credentials_fd(fd: int) -> dict[str, int]:
    sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return peer_credentials(sock)
    finally:
        sock.close()


def classify(
    component: str,
    operation: str,
    caller_uid: int,
    owner_uid: int | None,
) -> tuple[str, str, str, str]:
    if component == "agent-merge" and operation == "approve":
        if caller_uid == owner_uid:
            return operation, "allow", "owner_approval", "locked"
        return operation, "would_deny", "owner_required", "locked"
    known = POLICY.get(component, {}).get(operation)
    if known is None:
        return "unknown", "would_deny", "unknown_operation", "locked"
    decision, reason = known
    return operation, decision, reason, "locked"


def build_event(
    component: str,
    operation: str,
    caller: dict[str, int],
    owner_uid: int | None = None,
) -> dict:
    operation, decision, reason, lock_state = classify(
        component, operation, caller["uid"], owner_uid,
    )
    return {
        "event": "broker.decision",
        "component": component,
        "operation": operation,
        "decision": decision,
        "lock_state": lock_state,
        "reason": reason,
        "caller": {key: caller[key] for key in ("pid", "uid", "gid")},
    }


def request(socket_path: str, event: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(socket_path)
        sock.sendall(json.dumps(
            {"cmd": "write", "event": event}, separators=(",", ":"),
        ).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("audit response is too large")
        reply = json.loads(data)
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise ValueError("trusted audit writer rejected the event")
    return reply


def audit(
    component: str,
    operation: str,
    caller: dict[str, int],
    socket_path: str = DEFAULT_SOCKET,
    owner_uid: int | None = None,
) -> dict:
    event = build_event(component, operation, caller, owner_uid)
    try:
        request(socket_path, event)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        pass
    return event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--component", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--peer-fd", type=int, required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    try:
        caller = peer_credentials_fd(args.peer_fd)
    except OSError:
        return 0
    audit(args.component, args.operation, caller, args.socket, os.geteuid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
