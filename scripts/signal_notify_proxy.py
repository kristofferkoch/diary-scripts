#!/usr/bin/python3
"""signal-notify — a narrow note-to-self and attachment proxy for signal-cli.

The signal-cli daemon exposes one JSON-RPC socket that can do everything:
subscribe to incoming messages, list accounts, and `send` to *any* recipient.
Anything that can reach that socket can therefore read the whole Signal history
and impersonate the user — the exfiltration leg of the lethal trifecta.

This proxy listens on its own socket (default ``/run/signal-notify/socket``)
and exposes only note-to-self sends and attachment reads:

* ``send`` requires ``params.noteToSelf == true``, rejects all addressing
  fields, caps message length and rate, and reconstructs the upstream request.
* ``getAttachment`` reads one regular file from signal-cli's attachment store.
  IDs are validated as basenames, symlinks and paths are rejected, and the
  response is base64-encoded. Nothing is forwarded upstream for this method.
* With explicit ``--enforce-lockdown``, lock and audit writes are mandatory,
  locked general-agent sends are denied, and only the kernel-authenticated
  pipeline UID may use ``sendPipelineNotification`` with the strict versioned
  PIPE-04 record. Its fixed rendered body is forwarded unchanged, and rejected
  sends are never queued.

The upstream send call is a plain JSON-RPC client request — no receive loop —
so the single-receiver rule (network/docs/signal-cli-mirror.md) is respected.

Run it as the account owner, with the socket directory group-readable by the
agent group:

    signal-notify-proxy --listen /run/signal-notify/socket
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pwd
import socket
import sys
import threading
import time
from collections.abc import Callable

if __package__:
    from scripts import broker_audit, diary_lock, pipeline_notification
    from scripts.attachment_store import read_attachment as _read_attachment
    from scripts.attachment_store import validate_attachment_id
else:
    import broker_audit  # ty: ignore[unresolved-import]
    import diary_lock  # ty: ignore[unresolved-import]
    import pipeline_notification  # ty: ignore[unresolved-import]
    from attachment_store import read_attachment as _read_attachment  # ty: ignore[unresolved-import]
    from attachment_store import validate_attachment_id  # ty: ignore[unresolved-import]

MAX_MESSAGE_CHARS = 4000
MAX_MESSAGES_PER_MINUTE = 20
MAX_PIPELINE_MESSAGES_PER_MINUTE = 6
PIPELINE_EVENT_INTERVAL = 300.0
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
DEFAULT_LISTEN = "/run/signal-notify/socket"
DEFAULT_PIPELINE_USER = "diary-pipeline"
PIPELINE_METHOD = "sendPipelineNotification"
ATTACHMENT_SLOTS = threading.BoundedSemaphore(2)

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
    send security boundary and can be unit-tested without a socket."""
    if not isinstance(request, dict):
        return None, "request must be a JSON object"
    if request.get("method") != "send":
        return None, "method is not available through this proxy"
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


class NotificationLimiter:
    """Atomic per-event and aggregate limits for accepted pipeline sends."""

    def __init__(
        self,
        burst: int = MAX_PIPELINE_MESSAGES_PER_MINUTE,
        period: float = 60.0,
        event_period: float = PIPELINE_EVENT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.burst = burst
        self.period = period
        self.event_period = event_period
        self.clock = clock
        self.tokens = float(burst)
        self.stamp = clock()
        self.events: dict[str, float] = {}
        self.lock = threading.Lock()

    def allow(self, event_ids: tuple[str, ...]) -> str:
        with self.lock:
            now = self.clock()
            if any(
                event_id in self.events and now - self.events[event_id] < self.event_period
                for event_id in event_ids
            ):
                return "pipeline event rate limit exceeded"
            self.tokens = min(
                self.burst,
                self.tokens + (now - self.stamp) * self.burst / self.period,
            )
            self.stamp = now
            if self.tokens < 1:
                return "pipeline aggregate rate limit exceeded"
            new_count = sum(event_id not in self.events for event_id in event_ids)
            while len(self.events) + new_count > 4096:
                oldest = min(self.events, key=self.events.__getitem__)
                del self.events[oldest]
            for event_id in event_ids:
                self.events[event_id] = now
            self.tokens -= 1
            return ""


class LockedSignalBoundary:
    """Fail-closed send authorization for the explicitly enabled locked mode."""

    def __init__(
        self,
        pipeline_user: str = DEFAULT_PIPELINE_USER,
        lock_socket: str = diary_lock.DEFAULT_SOCKET,
        audit_socket: str = broker_audit.DEFAULT_SOCKET,
        pipeline_uid: int | None = None,
        lock_request: Callable[..., dict] = diary_lock.request,
        audit_request: Callable[..., dict] = broker_audit.request,
        notification_limiter: NotificationLimiter | None = None,
    ) -> None:
        self.pipeline_user = pipeline_user
        self.lock_socket = lock_socket
        self.audit_socket = audit_socket
        self.pipeline_uid = pipeline_uid
        self.lock_request = lock_request
        self.audit_request = audit_request
        self.notification_limiter = notification_limiter or NotificationLimiter()

    def _lock_state(self) -> str:
        try:
            reply = self.lock_request(self.lock_socket, {"cmd": "status"}, timeout=2.0)
            state = reply.get("state") if isinstance(reply, dict) else None
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                return "UNKNOWN"
            if not isinstance(state, str) or state not in diary_lock.STATES:
                return "UNKNOWN"
            return state
        except (OSError, ValueError):
            return "UNKNOWN"

    def _trusted_pipeline_uid(self) -> int | None:
        if self.pipeline_uid is not None:
            return self.pipeline_uid
        try:
            uid = pwd.getpwnam(self.pipeline_user).pw_uid
        except (KeyError, OSError):
            return None
        return uid if type(uid) is int and uid >= 0 else None

    def _record(
        self,
        operation: str,
        decision: str,
        state: str,
        reason: str,
        caller: dict[str, int],
        event_id: str | None = None,
    ) -> str:
        event = {
            "event": "broker.decision",
            "component": "signal-notify",
            "operation": operation,
            "decision": decision,
            "lock_state": state.lower(),
            "reason": reason,
            "caller": {key: caller[key] for key in ("pid", "uid", "gid")},
        }
        if event_id is not None:
            event["event_id"] = event_id
        try:
            self.audit_request(self.audit_socket, event)
        except (OSError, ValueError):
            return "audit unavailable; send denied"
        return ""

    def _record_events(
        self,
        operation: str,
        decision: str,
        state: str,
        reason: str,
        caller: dict[str, int],
        event_ids: tuple[str, ...],
    ) -> str:
        for event_id in event_ids:
            error = self._record(
                operation, decision, state, reason, caller, event_id,
            )
            if error:
                return error
        return ""

    def _deny(
        self,
        operation: str,
        state: str,
        reason: str,
        caller: dict[str, int],
        message: str,
    ) -> tuple[dict | None, str]:
        audit_error = self._record(operation, "deny", state, reason, caller)
        return None, audit_error or message

    def authorize(
        self,
        request: object,
        caller: dict[str, int],
        general_limiter: Limiter,
    ) -> tuple[dict | None, str]:
        """Authorize and audit before returning any request that may execute."""
        state = self._lock_state()
        if state == "UNKNOWN":
            return self._deny(
                "send", state, "lock_unavailable", caller,
                "lock state unavailable; send denied",
            )
        if not isinstance(request, dict):
            return self._deny(
                "send", state, "invalid_request", caller,
                "request must be a JSON object",
            )
        request = {str(key): value for key, value in request.items()}

        if request.get("method") == PIPELINE_METHOD:
            operation = PIPELINE_METHOD
            trusted_uid = self._trusted_pipeline_uid()
            caller_uid = caller.get("uid")
            if trusted_uid is None:
                return self._deny(
                    operation, state, "pipeline_identity_unavailable", caller,
                    "pipeline identity unavailable; send denied",
                )
            if type(caller_uid) is not int or caller_uid != trusted_uid:
                return self._deny(
                    operation, state, "pipeline_identity_mismatch", caller,
                    "only the trusted pipeline identity may use this method",
                )
            params = request.get("params")
            if not isinstance(params, dict) or set(params) != {"notification"}:
                return self._deny(
                    operation, state, "invalid_pipeline_schema", caller,
                    "params must contain only notification",
                )
            params = {str(key): value for key, value in params.items()}
            try:
                notification = pipeline_notification.validate_notification(
                    params["notification"],
                )
            except (TypeError, ValueError) as exc:
                return self._deny(
                    operation, state, "invalid_pipeline_schema", caller, str(exc),
                )
            event_ids = tuple(notification["event_ids"])
            rate_error = self.notification_limiter.allow(event_ids)
            if rate_error:
                audit_error = self._record_events(
                    operation, "deny", state, "pipeline_rate_limited", caller, event_ids,
                )
                return None, audit_error or rate_error
            clean = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "method": "send",
                "params": {"noteToSelf": True, "message": notification["body"]},
            }
            audit_error = self._record_events(
                operation, "allow", state, "validated_pipeline_notification",
                caller, event_ids,
            )
            if audit_error:
                return None, audit_error
            return clean, ""

        if state != "ATTENDED":
            return self._deny(
                "send", state, "locked_general_agent_send", caller,
                "general-agent sends are rejected while locked",
            )
        clean, reason = sanitize(request)
        if clean is None:
            return self._deny("send", state, "invalid_general_send", caller, reason)
        if not general_limiter.allow():
            return self._deny(
                "send", state, "general_rate_limited", caller,
                "rate limit exceeded, try again later",
            )
        audit_error = self._record("send", "allow", state, "attended_note_to_self", caller)
        if audit_error:
            return None, audit_error
        return clean, ""


def attachment_id(request: dict) -> str:
    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("id"), str):
        raise TypeError("getAttachment requires a string params.id")
    return validate_attachment_id(params["id"])


def read_attachment(root: str, value: str) -> bytes:
    return _read_attachment(root, value, MAX_ATTACHMENT_BYTES)


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
                    continue
                if "error" in msg:
                    return False, f"signal-cli error: {msg['error']}"
                return True, ""
    except OSError as exc:
        return False, f"upstream unavailable: {exc}"
    finally:
        sock.close()


def send_error(conn: socket.socket, request_id: object, reason: str) -> None:
    code = -32000 if reason == "rate limit exceeded, try again later" else -32600
    reply = {"jsonrpc": "2.0", "id": request_id,
             "error": {"code": code, "message": reason}}
    conn.sendall(json.dumps(reply).encode() + b"\n")


def handle_attachment(conn: socket.socket, request: dict, root: str) -> None:
    request_id = request.get("id")
    with ATTACHMENT_SLOTS:
        try:
            value = attachment_id(request)
            data = read_attachment(root, value)
        except FileNotFoundError:
            send_error(conn, request_id, "attachment not found")
            return
        except ValueError as exc:
            send_error(conn, request_id, str(exc))
            return
        except OSError:
            send_error(conn, request_id, "attachment unavailable")
            return
        result = {
            "filename": value,
            "size": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        }
        conn.sendall(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode() + b"\n")


def handle(
    conn: socket.socket,
    upstream: str,
    limiter: Limiter,
    attachments_root: str | None = None,
    boundary: LockedSignalBoundary | None = None,
) -> None:
    try:
        caller = broker_audit.peer_credentials(conn)
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
            broker_audit.audit("signal-notify", "unknown", caller)
            conn.sendall(b'{"jsonrpc":"2.0","error":{"code":-32700,"message":"parse error"},"id":null}\n')
            return

        operation = request.get("method") if isinstance(request, dict) else "unknown"
        if not isinstance(operation, str):
            operation = "unknown"
        if boundary is None or operation == "getAttachment":
            broker_audit.audit("signal-notify", operation, caller)
        if isinstance(request, dict) and request.get("method") == "getAttachment":
            root = attachments_root or os.path.expanduser("~/.local/share/signal-cli/attachments")
            handle_attachment(conn, request, root)
            return

        if boundary is None:
            clean, reason = sanitize(request)
            if clean is not None and not limiter.allow():
                clean, reason = None, "rate limit exceeded, try again later"
        else:
            clean, reason = boundary.authorize(request, caller, limiter)
        if clean is None:
            print(f"[signal-notify] rejected: {reason}", file=sys.stderr, flush=True)
            send_error(conn, request.get("id") if isinstance(request, dict) else None, reason)
            return

        ok, err = forward_ok(upstream, clean)
        if ok:
            conn.sendall(json.dumps({"jsonrpc": "2.0", "id": clean.get("id"), "result": {}}).encode() + b"\n")
            print("[signal-notify] sent note-to-self", file=sys.stderr, flush=True)
        else:
            print(f"[signal-notify] upstream failed: {err}", file=sys.stderr, flush=True)
            reply = {"jsonrpc": "2.0", "id": clean.get("id"),
                     "error": {"code": -32000, "message": err}}
            conn.sendall(json.dumps(reply).encode() + b"\n")
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
    p.add_argument("--attachments", default=None,
                   help="attachment store (default $SIGNAL_CLI_ATTACHMENTS or "
                        "~/.local/share/signal-cli/attachments)")
    p.add_argument("--enforce-lockdown", action="store_true",
                   help="enable fail-closed PIPE-05 send policy (default: legacy audit-only)")
    p.add_argument("--pipeline-user", default=DEFAULT_PIPELINE_USER,
                   help="trusted pipeline account for kernel peer-UID checks")
    p.add_argument("--lock-socket", default=diary_lock.DEFAULT_SOCKET)
    p.add_argument("--audit-socket", default=broker_audit.DEFAULT_SOCKET)
    args = p.parse_args(argv)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    upstream = args.upstream or os.environ.get("SIGNAL_CLI_SOCKET") or f"{runtime}/signal-cli/socket"
    attachments = (args.attachments or os.environ.get("SIGNAL_CLI_ATTACHMENTS")
                   or os.path.expanduser("~/.local/share/signal-cli/attachments"))

    listen_dir = os.path.dirname(args.listen) or "."
    os.makedirs(listen_dir, exist_ok=True)
    try:
        os.unlink(args.listen)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.listen)
    os.chmod(args.listen, 0o770)
    srv.listen(16)

    print(f"[signal-notify] listening on {args.listen} -> {upstream} "
          f"(note-to-self + read-only attachments, {MAX_MESSAGES_PER_MINUTE} sends/min; "
          f"lockdown enforcement={args.enforce_lockdown})",
          file=sys.stderr, flush=True)

    limiter = Limiter()
    boundary = LockedSignalBoundary(
        pipeline_user=args.pipeline_user,
        lock_socket=args.lock_socket,
        audit_socket=args.audit_socket,
    ) if args.enforce_lockdown else None
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(
                target=handle,
                args=(conn, upstream, limiter, attachments, boundary),
                daemon=True,
            ).start()
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
