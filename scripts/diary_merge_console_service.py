#!/usr/bin/python3
"""Fixed-argv merge/deny executor behind the independent owner console.

The web UI runs as the unprivileged owner-UI identity and holds no repository,
publication, or audit authority. This process runs with the owner's
(`kek`) authority and exposes exactly two verbs over a group-restricted Unix
socket: read the reviewed queue snapshot, and execute one already-reviewed
merge or deny decision. Every decision additionally requires a consumed,
single-use owner grant issued by the root-owned fresh-authorization service and
bound to the action plus the exact queue and approved-base OIDs.

It is not a general broker: there is no shell, no arbitrary Git command, no
path from the caller, and no verb that does not resolve through
`agent_merge_console.MergeConsole`. A caller that fully controls the web UI can
therefore only replay the exact decision an owner just authorized.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import socketserver
import struct
import sys
from pathlib import Path
from typing import Any, Protocol

from scripts import agent_merge_console as console
from scripts import agent_merge_queue as queue
from scripts import agent_merge_review as review

DEFAULT_SOCKET = "/run/diary-merge-console/socket"
DEFAULT_VERIFY_SOCKET = "/run/diary-fresh-unlock/verify.sock"
DEFAULT_CONTROLLER_SOCKET = "/run/diary-lock/socket"
DEFAULT_UI_USER = "diary-lock-ui"
MAX_REQUEST_BYTES = 16 * 1024
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_DIFF_BYTES + 64 * 1024


class ExecutorError(RuntimeError):
    """The executor cannot answer without weakening the boundary."""


def peer_uid(connection: socket.socket) -> int:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


class GrantVerifier(Protocol):
    def verify(
        self, repo: str, action: str, queue_oid: str, base_oid: str, transaction: str,
    ) -> bool:
        """True only when a consumed owner grant matches this exact decision."""


class UnixGrantVerifier:
    """Ask the root-owned verifier whether this exact grant was consumed."""

    def __init__(self, path: str = DEFAULT_VERIFY_SOCKET, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout

    def verify(
        self, repo: str, action: str, queue_oid: str, base_oid: str, transaction: str,
    ) -> bool:
        request = {
            "cmd": "verify",
            "repository": repo,
            "transaction": transaction,
            "action": action,
            "queue_oid": queue_oid,
            "base_oid": base_oid,
        }
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            sock.sendall(json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 8192:
                    raise ExecutorError("grant verifier response is oversized")
        except OSError as exc:
            raise ExecutorError("grant verifier is unavailable") from exc
        finally:
            sock.close()
        try:
            reply = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutorError("grant verifier response is invalid") from exc
        return isinstance(reply, dict) and reply.get("ok") is True


class LockStateReader:
    """Read the controller state for the audit record; unknown reads as locked."""

    def __init__(self, path: str = DEFAULT_CONTROLLER_SOCKET, timeout: float = 3.0) -> None:
        self.path = path
        self.timeout = timeout

    def __call__(self) -> str:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            sock.sendall(b'{"cmd":"status"}\n')
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 64 * 1024:
                    raise ExecutorError("controller response is oversized")
        except OSError:
            return "locked"
        finally:
            sock.close()
        try:
            reply = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "locked"
        state = reply.get("state") if isinstance(reply, dict) else None
        return "attended" if state == "ATTENDED" else "locked"


def _snapshot_payload(snapshot: console.QueueSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"pending": False}
    if len(snapshot.diff) > MAX_DIFF_BYTES:
        raise ExecutorError("the diff is too large to review in the browser")
    return {
        "pending": True,
        "repo": snapshot.repo,
        "queue_oid": snapshot.queue_oid,
        "base_oid": snapshot.base_oid,
        "commits": [{"oid": item.oid, "subject": item.subject} for item in snapshot.commits],
        "diff": snapshot.diff,
        "advisory": {"status": snapshot.advisory.status, "text": snapshot.advisory.text},
    }


def _console(
    paths: queue.QueuePaths,
    verifier: GrantVerifier,
    authorizer: console.OwnerAuthorizer,
    *,
    auditor: console.DecisionAuditor,
    publisher: console.Publisher,
    lock_state: str,
    repo: str = "scripts",
) -> console.MergeConsole:
    return console.MergeConsole(
        paths,
        authorizer,
        auditor,
        report_dir=review.DEFAULT_REPORT_DIR,
        publisher=publisher,
        lock_state=lock_state,
        repo=repo,
    )


def _publication(repo: str) -> console.Publisher:
    """Publish with the same environment handling the CLI uses per repository.

    A diary approval has no Python environment to sync; only the scripts
    repository does. The CLI disables the venv sync for diary, and the console
    must not reintroduce it.
    """
    def publish(
        reviewed: str, paths: queue.QueuePaths, /, *, expected_base: str | None = None,
    ) -> bool:
        return queue.approve_queue(
            reviewed, paths,
            expected_base=expected_base,
            sync_environment=(repo == "scripts"),
        )

    return publish


def _paths_for(paths_by_repo: dict[str, queue.QueuePaths], repo: object) -> queue.QueuePaths:
    if not isinstance(repo, str) or repo not in console.REPOS:
        raise ExecutorError("request requires a known repository")
    try:
        return paths_by_repo[repo]
    except KeyError as exc:
        raise ExecutorError("requested repository is not configured") from exc


def handle_request(
    request: object,
    paths_by_repo: dict[str, queue.QueuePaths],
    verifier: GrantVerifier,
    *,
    auditor: console.DecisionAuditor,
    publisher: console.Publisher | None = None,
    lock_state: str = "locked",
) -> dict[str, Any]:
    """Serve one executor request without trusting any caller-supplied authority."""
    if not isinstance(request, dict):
        raise ExecutorError("request must be a JSON object")
    command = request.get("cmd")
    if command == "snapshot":
        if set(request) != {"cmd", "repo"}:
            raise ExecutorError("snapshot takes exactly one repository")
        repo = request["repo"]
        paths = _paths_for(paths_by_repo, repo)
        try:
            snapshot = _console(
                paths, verifier, _deny_all, auditor=auditor,
                publisher=publisher or _publication(repo),
                lock_state=lock_state, repo=repo,
            ).snapshot()
        except queue.QueueError as exc:
            raise ExecutorError("the reviewed queue cannot be read") from exc
        return {"ok": True, "snapshot": _snapshot_payload(snapshot)}
    if command != "decide":
        raise ExecutorError("unsupported request")

    if set(request) != {"cmd", "repo", "action", "queue_oid", "base_oid", "transaction"}:
        raise ExecutorError("decision request has missing or extra fields")
    repo = request["repo"]
    paths = _paths_for(paths_by_repo, repo)
    action = request["action"]
    queue_oid = request["queue_oid"]
    base_oid = request["base_oid"]
    transaction = request["transaction"]
    if action not in {"merge", "deny"} or not all(
        isinstance(value, str) for value in (queue_oid, base_oid, transaction)
    ):
        raise ExecutorError("decision request is invalid")
    if not queue.OID_RE.fullmatch(queue_oid) or not queue.OID_RE.fullmatch(base_oid):
        raise ExecutorError("decision requires full queue and base OIDs")
    if not transaction:
        raise ExecutorError("decision requires a fresh authorization transaction")

    def authorize(granted: console.DecisionAction, oid: str, base: str) -> console.OwnerGrant:
        if (granted, oid, base) != (action, queue_oid, base_oid):
            raise console.OwnerAuthorizationRequired("authorization does not match the decision")
        if not verifier.verify(repo, granted, oid, base, transaction):
            raise console.OwnerAuthorizationRequired(
                "no consumed owner grant matches this action and OID pair"
            )
        return console.OwnerGrant("owner", transaction)

    result = _console(
        paths, verifier, authorize, auditor=auditor,
        publisher=publisher or _publication(repo),
        lock_state=lock_state, repo=repo,
    ).decide(action, queue_oid, base_oid)
    return {"ok": True, "action": result.action, "outcome": result.outcome}


def _deny_all(action: console.DecisionAction, queue_oid: str, base_oid: str) -> console.OwnerGrant:
    raise console.OwnerAuthorizationRequired("the snapshot path holds no decision authority")


def error_reply(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, console.OwnerAuthorizationRequired):
        return {"ok": False, "error": "unauthorized"}
    if isinstance(exc, console.ApprovedBaseChanged):
        return {"ok": False, "error": "base_changed"}
    if isinstance(exc, console.QueueChanged):
        return {"ok": False, "error": "queue_changed"}
    if isinstance(exc, ExecutorError):
        return {"ok": False, "error": "unavailable"}
    if isinstance(exc, console.PublicationNotStarted):
        return {"ok": False, "error": "not_published"}
    if isinstance(exc, console.PublicationFailed):
        return {
            "ok": False,
            "error": {
                "published": "published_unconfirmed",
                "not_published": "not_published",
                "unknown": "publication_unknown",
            }[exc.state],
        }
    if isinstance(exc, console.ConsoleError):
        return {"ok": False, "error": "invalid"}
    return {"ok": False, "error": "merge_failed"}


def _serve_connection(
    connection: socket.socket,
    paths_by_repo: dict[str, queue.QueuePaths],
    verifier: GrantVerifier,
    auditor: console.DecisionAuditor,
    publisher: console.Publisher,
    lock_state: Any,
) -> None:
    try:
        line = b""
        while not line.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            line += chunk
            if len(line) > MAX_REQUEST_BYTES:
                raise ExecutorError("request is oversized")
        if not line.endswith(b"\n"):
            raise ExecutorError("request is incomplete")
        request = json.loads(line)
        reply = handle_request(
            request, paths_by_repo, verifier,
            auditor=auditor, publisher=publisher,
            lock_state=lock_state() if callable(lock_state) else lock_state,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        reply = {"ok": False, "error": "invalid"}
    except Exception as exc:  # noqa: BLE001 - mapped to a fixed code, never echoed
        reply = error_reply(exc)
    try:
        connection.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
    except OSError:
        pass


def serve_stdio(
    paths_by_repo: dict[str, queue.QueuePaths],
    verifier: GrantVerifier,
    allowed_uid: int,
    *,
    auditor: console.DecisionAuditor | None = None,
    publisher: console.Publisher | None = None,
    lock_state: Any = "locked",
    connection: socket.socket | None = None,
) -> int:
    """Socket-activated mode: one connection on stdin/stdout, peer UID checked."""
    conn = connection or socket.socket(fileno=0)
    try:
        uid = peer_uid(conn)
    except OSError:
        uid = -1
    if uid != allowed_uid:
        conn.sendall(b'{"ok":false,"error":"caller is not authorized"}\n')
        return 1
    _serve_connection(
        conn, paths_by_repo, verifier, auditor or console.DiaryDecisionAudit(), publisher, lock_state,
    )
    return 0


class ExecutorServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: str,
        paths_by_repo: dict[str, queue.QueuePaths],
        verifier: GrantVerifier,
        allowed_uid: int,
        *,
        auditor: console.DecisionAuditor,
        publisher: console.Publisher | None = None,
        lock_state: Any = "locked",
    ) -> None:
        self.paths_by_repo = paths_by_repo
        self.verifier = verifier
        self.allowed_uid = allowed_uid
        self.auditor = auditor
        self.publisher = publisher
        self.lock_state = lock_state
        super().__init__(path, ExecutorHandler)


class ExecutorHandler(socketserver.BaseRequestHandler):
    server: ExecutorServer

    def handle(self) -> None:
        server = self.server
        try:
            uid = peer_uid(self.request)
        except OSError:
            return
        if uid != server.allowed_uid:
            try:
                self.request.sendall(b'{"ok":false,"error":"caller is not authorized"}\n')
            except OSError:
                pass
            return
        auditor = server.auditor
        lock_state = server.lock_state() if callable(server.lock_state) else server.lock_state
        _serve_connection(
            self.request, server.paths_by_repo, server.verifier, auditor, server.publisher, lock_state,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="socket-activated: one request per connection")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--verify-socket", default=DEFAULT_VERIFY_SOCKET)
    parser.add_argument("--controller-socket", default=DEFAULT_CONTROLLER_SOCKET)
    parser.add_argument("--ui-user", default=DEFAULT_UI_USER)
    parser.add_argument("--lock-state", choices=("auto", "locked", "attended"), default="auto")
    args = parser.parse_args()

    paths_by_repo = {
        "scripts": queue.default_paths(),
        "diary": queue.diary_paths(),
    }
    verifier = UnixGrantVerifier(args.verify_socket)
    lock_state: Any = args.lock_state
    if args.lock_state == "auto":
        lock_state = LockStateReader(args.controller_socket)

    if args.stdio:
        ui_uid = pwd.getpwnam(args.ui_user).pw_uid
        raise SystemExit(serve_stdio(paths_by_repo, verifier, ui_uid, lock_state=lock_state))

    ui_uid = pwd.getpwnam(args.ui_user).pw_uid
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    server = ExecutorServer(
        args.socket, paths_by_repo, verifier, ui_uid,
        auditor=console.DiaryDecisionAudit(), lock_state=lock_state,
    )
    os.chmod(args.socket, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
