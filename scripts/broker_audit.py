#!/usr/bin/python3
"""Audit-only and fail-closed locked-mode decisions for trusted brokers.

The default remains AUD-04 observation: record a hypothetical locked-mode
classification without changing the broker response. Explicit enforcement asks
the lock controller for each request, requires the audit write to succeed, and
never queues or retries the broker operation.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct

DEFAULT_SOCKET = "/run/diary-audit/socket"
DEFAULT_LOCK_SOCKET = "/run/diary-lock/socket"
MAX_RESPONSE_BYTES = 64 * 1024
CONTROLLER_STATES = frozenset({"LOCKING", "LOCKED", "ATTENDED"})

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
        "break_glass": ("allow", "owner_break_glass"),
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


def _event(
    component: str,
    operation: str,
    decision: str,
    lock_state: str,
    reason: str,
    caller: dict[str, int],
) -> dict:
    return {
        "event": "broker.decision",
        "component": component,
        "operation": operation,
        "decision": decision,
        "lock_state": lock_state,
        "reason": reason,
        "caller": {key: caller[key] for key in ("pid", "uid", "gid")},
    }


def build_event(
    component: str,
    operation: str,
    caller: dict[str, int],
    owner_uid: int | None = None,
) -> dict:
    operation, decision, reason, lock_state = classify(
        component, operation, caller["uid"], owner_uid,
    )
    return _event(component, operation, decision, lock_state, reason, caller)


def _enforcement_event(
    component: str,
    operation: str,
    caller: dict[str, int],
    owner_uid: int | None,
    state: str,
) -> dict:
    operation, decision, reason, _ = classify(
        component, operation, caller["uid"], owner_uid,
    )
    if state == "ATTENDED":
        if reason == "owner_required":
            return _event(component, operation, "deny", "attended", reason, caller)
        if reason != "owner_approval":
            reason = "attended"
        return _event(component, operation, "allow", "attended", reason, caller)
    if decision == "would_deny":
        decision = "deny"
    return _event(component, operation, decision, state.lower(), reason, caller)


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


def controller_request(
    socket_path: str,
    value: dict,
    timeout: float = 2.0,
) -> dict:
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
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("controller response is too large")
        reply = json.loads(data)
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise ValueError("lock controller rejected the request")
    return reply


def controller_state(socket_path: str) -> str:
    reply = controller_request(socket_path, {"cmd": "status"})
    state = reply.get("state")
    if not isinstance(state, str) or state not in CONTROLLER_STATES:
        raise ValueError("lock controller returned an unknown state")
    return state


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


def authorize(
    component: str,
    operation: str,
    caller: dict[str, int],
    *,
    enforce: bool = False,
    socket_path: str = DEFAULT_SOCKET,
    lock_socket: str = DEFAULT_LOCK_SOCKET,
    owner_uid: int | None = None,
) -> tuple[bool, dict]:
    if not enforce:
        return True, audit(component, operation, caller, socket_path, owner_uid)
    try:
        state = controller_state(lock_socket)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        operation, _, _, _ = classify(component, operation, caller["uid"], owner_uid)
        event = _event(
            component, operation, "deny", "unknown", "controller_unavailable", caller,
        )
    else:
        event = _enforcement_event(component, operation, caller, owner_uid, state)
    try:
        request(socket_path, event)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        event["decision"] = "deny"
        event["reason"] = "audit_unavailable"
        return False, event
    return event["decision"] == "allow", event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--component", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--peer-fd", type=int, required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--enforce-lockdown", action="store_true")
    parser.add_argument("--lock-socket", default=DEFAULT_LOCK_SOCKET)
    args = parser.parse_args(argv)
    try:
        caller = peer_credentials_fd(args.peer_fd)
    except OSError:
        return 1 if args.enforce_lockdown else 0
    allowed, _ = authorize(
        args.component,
        args.operation,
        caller,
        enforce=args.enforce_lockdown,
        socket_path=args.socket,
        lock_socket=args.lock_socket,
        owner_uid=os.geteuid(),
    )
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
