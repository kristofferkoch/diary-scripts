#!/usr/bin/python3
"""Fail-closed lock-state policy for the separately deployed AUD-03 DNS proxy."""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import socket
import socketserver
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

DEFAULT_SOCKET = "/run/diary-dns-policy/socket"
DEFAULT_LOCK_SOCKET = "/run/diary-lock/socket"
DEFAULT_AUDIT_SOCKET = "/run/diary-audit/socket"
MAX_REQUEST_BYTES = 2048
MAX_RESPONSE_BYTES = 4096
REFUSED = 5
NOERROR = 0
PROTOCOLS = frozenset({"udp", "tcp"})
STATES = frozenset({"LOCKING", "LOCKED", "ATTENDED"})
LABEL_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?\Z")


class DNSPolicyError(ValueError):
    """The policy request, lock state, or trusted audit path is uncertain."""


@dataclass(frozen=True)
class PolicyReply:
    allow: bool
    decision: str
    lock_state: str
    reason: str
    rcode: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "allow": self.allow,
            "decision": self.decision,
            "lock_state": self.lock_state,
            "reason": self.reason,
            "rcode": self.rcode,
        }


REFUSE_UNAVAILABLE = PolicyReply(False, "error", "unknown", "policy_unavailable", REFUSED)


def peer_credentials(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def dns_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise DNSPolicyError("dns_name must be a bounded DNS name")
    try:
        normalized = value.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise DNSPolicyError("dns_name is not valid IDNA") from exc
    if not normalized or len(normalized) > 253:
        raise DNSPolicyError("dns_name is empty or too long")
    labels = normalized.split(".")
    if any(not label or len(label) > 63 or not LABEL_RE.fullmatch(label) for label in labels):
        raise DNSPolicyError("dns_name contains an invalid label")
    return normalized


def request(socket_path: str, value: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(MAX_RESPONSE_BYTES + 1)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_BYTES:
                raise DNSPolicyError("trusted service response is too large")
        reply = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DNSPolicyError(f"cannot use trusted service socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        error = reply.get("error", "trusted service request failed") if isinstance(reply, dict) else "trusted service request failed"
        raise DNSPolicyError(str(error))
    return reply


class LockState(Protocol):
    def state(self) -> str: ...


class AuditSink(Protocol):
    def write(self, event: dict[str, Any]) -> None: ...


class ControllerState:
    def __init__(self, socket_path: str = DEFAULT_LOCK_SOCKET) -> None:
        self.socket_path = socket_path

    def state(self) -> str:
        reply = request(self.socket_path, {"cmd": "status"})
        state = reply.get("state")
        if not isinstance(state, str) or state not in STATES:
            raise DNSPolicyError("controller returned an unknown lock state")
        return state


class AuditClient:
    def __init__(self, socket_path: str = DEFAULT_AUDIT_SOCKET) -> None:
        self.socket_path = socket_path

    def write(self, event: dict[str, Any]) -> None:
        request(self.socket_path, {"cmd": "write", "event": event})


class DNSPolicyService:
    def __init__(self, lock_state: LockState, audit: AuditSink, agent_uid: int, agent_gid: int) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647 for value in (agent_uid, agent_gid)):
            raise DNSPolicyError("invalid fixed agent identity")
        self.lock_state = lock_state
        self.audit = audit
        self.agent_uid = agent_uid
        self.agent_gid = agent_gid

    def _reply(self, decision: str, state: str, reason: str) -> PolicyReply:
        allow = decision == "allow"
        return PolicyReply(allow, decision, state.lower(), reason, NOERROR if allow else REFUSED)

    def _record(self, name: str, protocol: str, reply: PolicyReply) -> None:
        self.audit.write({
            "event": "dns.query",
            "component": "diary-dns-policyd",
            "operation": "query",
            "decision": reply.decision,
            "lock_state": reply.lock_state,
            "reason": reply.reason,
            "dns_name": name,
            "protocol": protocol,
            "caller": {"pid": 0, "uid": self.agent_uid, "gid": self.agent_gid},
        })

    def authorize(self, value: object) -> PolicyReply:
        if not isinstance(value, dict) or set(value) != {"dns_name", "protocol"}:
            raise DNSPolicyError("policy request requires exactly dns_name and protocol")
        value = cast(dict[str, Any], value)
        name = dns_name(value["dns_name"])
        protocol = value["protocol"]
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise DNSPolicyError("protocol must be udp or tcp")
        try:
            state = self.lock_state.state()
            if state not in STATES:
                raise DNSPolicyError("controller returned an unknown lock state")
        except (DNSPolicyError, OSError):
            reply = self._reply("error", "unknown", "lock_unavailable")
            try:
                self._record(name, protocol, reply)
            except (DNSPolicyError, OSError):
                pass
            return reply
        if state == "ATTENDED":
            reply = self._reply("allow", state, "attended")
        else:
            reply = self._reply("deny", state, state.lower())
        try:
            self._record(name, protocol, reply)
        except (DNSPolicyError, OSError):
            return self._reply("error", state, "audit_unavailable")
        return reply


def _validate_reply(value: object) -> PolicyReply:
    if not isinstance(value, dict) or set(value) != {"ok", "allow", "decision", "lock_state", "reason", "rcode"}:
        raise DNSPolicyError("policy response has invalid fields")
    value = cast(dict[str, Any], value)
    if value["ok"] is not True or not isinstance(value["allow"], bool):
        raise DNSPolicyError("policy response is not authoritative")
    if not isinstance(value["decision"], str) or not isinstance(value["lock_state"], str) or not isinstance(value["reason"], str):
        raise DNSPolicyError("policy response has invalid metadata")
    if isinstance(value["rcode"], bool) or not isinstance(value["rcode"], int):
        raise DNSPolicyError("policy response has an invalid rcode")
    reply = PolicyReply(value["allow"], value["decision"], value["lock_state"], value["reason"], value["rcode"])
    if reply.allow:
        if (reply.decision, reply.lock_state, reply.reason, reply.rcode) != ("allow", "attended", "attended", NOERROR):
            raise DNSPolicyError("policy response has inconsistent allow fields")
    elif reply.rcode != REFUSED or reply.decision not in {"deny", "error"}:
        raise DNSPolicyError("policy response has inconsistent refusal fields")
    return reply


class PolicyClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    def authorize(self, name: str, protocol: str) -> PolicyReply:
        try:
            name = dns_name(name)
            if protocol not in PROTOCOLS:
                raise DNSPolicyError("protocol must be udp or tcp")
            reply = request(self.socket_path, {"dns_name": name, "protocol": protocol})
            return _validate_reply(reply)
        except (DNSPolicyError, OSError):
            return REFUSE_UNAVAILABLE


class PolicyRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        conn = self.request
        server = cast(PolicyDaemon, self.server)
        conn.settimeout(3.0)
        peer = peer_credentials(conn)
        try:
            if not server.authorized(peer):
                raise DNSPolicyError("caller is not the configured DNS proxy")
            line = conn.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
            if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise DNSPolicyError("policy request is missing, oversized, or incomplete")
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DNSPolicyError("policy request is not valid JSON") from exc
            reply = server.service.authorize(value).as_dict()
        except (DNSPolicyError, OSError) as exc:
            reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
        except OSError:
            pass


class PolicyDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, service: DNSPolicyService, proxy_uid: int, proxy_gid: int) -> None:
        self.path = path
        self.service = service
        self.proxy_uid = proxy_uid
        parent = Path(path).parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise DNSPolicyError("policy socket directory must be private to root")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise DNSPolicyError("refusing to replace a non-socket or foreign socket")
            os.unlink(path)
        super().__init__(path, PolicyRequestHandler)
        os.chown(path, -1, proxy_gid)
        os.chmod(path, 0o660)

    def authorized(self, peer: dict[str, int]) -> bool:
        return peer["uid"] in {0, self.proxy_uid}

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def sd_notify_ready() -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    elif not address.startswith("/"):
        raise DNSPolicyError("NOTIFY_SOCKET must be an absolute path or abstract socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2.0)
        sock.sendto(b"READY=1", address)
    finally:
        sock.close()


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--listen", default=DEFAULT_SOCKET)
    parser.add_argument("--lock-socket", default=DEFAULT_LOCK_SOCKET)
    parser.add_argument("--audit-socket", default=DEFAULT_AUDIT_SOCKET)
    parser.add_argument("--proxy-user", default="diary-dns-proxy")
    parser.add_argument("--agent-user", default="agent")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-dns-policyd must run as root")
    server: PolicyDaemon | None = None
    try:
        proxy = pwd.getpwnam(args.proxy_user)
        agent = pwd.getpwnam(args.agent_user)
        service = DNSPolicyService(
            ControllerState(args.lock_socket), AuditClient(args.audit_socket), agent.pw_uid, agent.pw_gid,
        )
        server = PolicyDaemon(args.listen, service, proxy.pw_uid, proxy.pw_gid)
        sd_notify_ready()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (DNSPolicyError, KeyError, OSError) as exc:
        print(f"diary-dns-policyd: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the trusted DNS lock policy without forwarding DNS.")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--protocol", choices=sorted(PROTOCOLS), default="udp")
    parser.add_argument("dns_name")
    args = parser.parse_args(argv)
    reply = PolicyClient(args.socket).authorize(args.dns_name, args.protocol)
    print(json.dumps(reply.as_dict(), indent=2, sort_keys=True))
    return 0 if reply.allow else 1


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "diary-dns-policyd":
        raise SystemExit(daemon_main())
    raise SystemExit(main())
