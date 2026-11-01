#!/usr/bin/python3
"""Audit-only per-identity DNS forwarding with trusted query-name metadata."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import queue
import signal
import socket
import socketserver
import struct
import sys
import threading
from collections.abc import Callable
from typing import Any, cast

DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_PORT = 1053
DEFAULT_SOCKET = "/run/diary-audit/socket"
AUDIT_TIMEOUT = 2.0
UPSTREAM_TIMEOUT = 5.0
QUEUE_SIZE = 1024
MAX_CLIENTS = 128
SHUTDOWN_TIMEOUT = 5.0
MAX_AUDIT_RESPONSE = 64 * 1024


class DNSAuditError(ValueError):
    """DNS metadata or the trusted audit channel failed validation."""


def _labels(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    visited: set[int] = set()
    end: int | None = None
    for _ in range(128):
        if offset >= len(data):
            raise DNSAuditError("truncated DNS name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise DNSAuditError("truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer >= len(data) or pointer in visited:
                raise DNSAuditError("invalid DNS compression pointer")
            visited.add(pointer)
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        if length & 0xC0:
            raise DNSAuditError("unsupported DNS label type")
        offset += 1
        if length == 0:
            if not labels:
                raise DNSAuditError("root DNS name has no audit representation")
            name = ".".join(labels)
            if len(name) > 253:
                raise DNSAuditError("DNS name is too long")
            return name, end if end is not None else offset
        if offset + length > len(data):
            raise DNSAuditError("truncated DNS label")
        try:
            label = data[offset:offset + length].decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise DNSAuditError("DNS label is not ASCII") from exc
        if not re_valid_label(label):
            raise DNSAuditError("DNS label is not approved metadata")
        labels.append(label)
        offset += length
    raise DNSAuditError("too many DNS compression pointers")


def re_valid_label(label: str) -> bool:
    if not 1 <= len(label) <= 63 or not (label[0].isalnum() or label[0] == "_"):
        return False
    if not (label[-1].isalnum() or label[-1] == "_"):
        return False
    return all(character.isalnum() or character in "_-" for character in label)


def query_name(message: bytes, protocol: str) -> str | None:
    """Return one approved question name, never other question or answer data."""
    data = message[2:] if protocol == "tcp" else message
    if len(data) < 12 or data[2] & 0x80 or struct.unpack_from("!H", data, 4)[0] != 1:
        return None
    try:
        name, offset = _labels(data, 12)
    except DNSAuditError:
        return None
    if offset + 4 > len(data):
        return None
    return name


def query_event(
    message: bytes,
    protocol: str,
    uid: int,
    gid: int,
    upstream: str,
    upstream_port: int,
) -> dict[str, Any] | None:
    if protocol not in ("udp", "tcp"):
        raise DNSAuditError("unsupported DNS transport")
    name = query_name(message, protocol)
    if name is None:
        return None
    return {
        "event": "dns.query",
        "component": "policy_proxy",
        "operation": "resolve",
        "decision": "would_deny",
        "lock_state": "not_applicable",
        "reason": "audit_only",
        "destination_ip": upstream,
        "destination_port": upstream_port,
        "protocol": protocol,
        "dns_name": name,
        "caller": {"pid": 0, "uid": uid, "gid": gid},
    }


def request(socket_path: str, event: dict[str, Any]) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(AUDIT_TIMEOUT)
        sock.connect(socket_path)
        value = {"cmd": "write", "event": event}
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65_535)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_AUDIT_RESPONSE:
                raise DNSAuditError("audit response is too large")
        reply = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise DNSAuditError(f"cannot use trusted audit socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error", "audit request failed") if isinstance(reply, dict) else "audit request failed"
        raise DNSAuditError(str(error))
    return reply


class AuditSink:
    """A bounded, non-blocking audit queue that cannot apply DNS backpressure."""

    def __init__(
        self,
        socket_path: str,
        writer: Callable[[str, dict[str, Any]], dict[str, Any]] = request,
    ) -> None:
        self.socket_path = socket_path
        self.writer = writer
        self.work: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_SIZE)
        self.sentinel = object()
        self.thread = threading.Thread(target=self._run, name="diary-dns-audit-writer", daemon=True)
        self.lock = threading.Lock()
        self.dropped = 0
        self.unavailable = 0

    def start(self) -> None:
        self.thread.start()

    def observe(self, event: dict[str, Any] | None) -> None:
        if event is None:
            return
        try:
            self.work.put_nowait(event)
        except queue.Full:
            with self.lock:
                self.dropped += 1

    def _run(self) -> None:
        while True:
            event = self.work.get()
            try:
                if event is self.sentinel:
                    return
                try:
                    self.writer(self.socket_path, event)
                except (DNSAuditError, OSError):
                    with self.lock:
                        self.unavailable += 1
            finally:
                self.work.task_done()

    def close(self) -> None:
        discarded = 0
        try:
            self.work.put_nowait(self.sentinel)
        except queue.Full:
            try:
                self.work.get_nowait()
                self.work.task_done()
                discarded = 1
            except queue.Empty:
                pass
            self.work.put_nowait(self.sentinel)
        self.thread.join(SHUTDOWN_TIMEOUT)
        alive = self.thread.is_alive()
        with self.lock:
            if self.dropped or self.unavailable or discarded or alive:
                print(
                    "diary-dns-auditd: audit loss: "
                    f"queue_overflow={self.dropped} audit_unavailable={self.unavailable} "
                    f"shutdown_loss={self.work.qsize() + discarded + int(alive)}",
                    file=sys.stderr,
                    flush=True,
                )


def _receive_exact(sock: socket.socket, length: int) -> bytes | None:
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def exchange_udp(message: bytes, upstream: tuple[str, int]) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(UPSTREAM_TIMEOUT)
        sock.connect(upstream)
        sock.send(message)
        return sock.recv(65_535)
    except OSError:
        return None
    finally:
        sock.close()


def exchange_tcp(message: bytes, upstream: tuple[str, int]) -> bytes | None:
    try:
        with socket.create_connection(upstream, timeout=UPSTREAM_TIMEOUT) as sock:
            sock.settimeout(UPSTREAM_TIMEOUT)
            sock.sendall(struct.pack("!H", len(message)) + message)
            header = _receive_exact(sock, 2)
            if header is None:
                return None
            return _receive_exact(sock, struct.unpack("!H", header)[0])
    except OSError:
        return None


class _BoundedThreads(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.client_slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.client_slots.acquire(blocking=False):
            cast(Any, self).shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.client_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.client_slots.release()


class _UDPServer(_BoundedThreads, socketserver.UDPServer):
    allow_reuse_address = True
    owner: Any


class _TCPServer(_BoundedThreads, socketserver.TCPServer):
    allow_reuse_address = True
    owner: Any


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        message, sock = self.request
        cast(Any, self.server).owner.handle_udp(message, self.client_address, sock)


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        cast(Any, self.server).owner.handle_tcp(self.request)


class DNSProxy:
    def __init__(
        self,
        listen: str,
        port: int,
        upstream: str,
        upstream_port: int,
        uid: int,
        gid: int,
        audit_socket: str = DEFAULT_SOCKET,
        writer: Callable[[str, dict[str, Any]], dict[str, Any]] = request,
    ) -> None:
        self.listen = str(ipaddress.IPv4Address(listen))
        self.upstream = str(ipaddress.IPv4Address(upstream))
        self.upstream_port = upstream_port
        self.uid = uid
        self.gid = gid
        self.sink = AuditSink(audit_socket, writer)
        self.udp = _UDPServer((self.listen, port), _UDPHandler)
        try:
            self.tcp = _TCPServer((self.listen, self.udp.server_address[1]), _TCPHandler)
        except BaseException:
            self.udp.server_close()
            raise
        self.udp.owner = self
        self.tcp.owner = self
        self.threads = [
            threading.Thread(target=self.udp.serve_forever, name="diary-dns-udp", daemon=True),
            threading.Thread(target=self.tcp.serve_forever, name="diary-dns-tcp", daemon=True),
        ]
        self.started = False

    @property
    def port(self) -> int:
        return self.udp.server_address[1]

    def start(self) -> None:
        if self.started:
            return
        self.sink.start()
        for thread in self.threads:
            thread.start()
        self.started = True

    def observe(self, message: bytes, protocol: str) -> None:
        self.sink.observe(query_event(
            message, protocol, self.uid, self.gid, self.upstream, self.upstream_port,
        ))

    def handle_udp(self, message: bytes, client: tuple[str, int], listener: socket.socket) -> None:
        self.observe(message, "udp")
        response = exchange_udp(message, (self.upstream, self.upstream_port))
        if response is not None:
            listener.sendto(response, client)

    def handle_tcp(self, client: socket.socket) -> None:
        client.settimeout(UPSTREAM_TIMEOUT)
        try:
            while True:
                header = _receive_exact(client, 2)
                if header is None:
                    return
                length = struct.unpack("!H", header)[0]
                if length == 0:
                    return
                message = _receive_exact(client, length)
                if message is None:
                    return
                self.observe(header + message, "tcp")
                response = exchange_tcp(message, (self.upstream, self.upstream_port))
                if response is None:
                    return
                client.sendall(struct.pack("!H", len(response)) + response)
        except OSError:
            return

    def close(self) -> None:
        if not self.started:
            self.udp.server_close()
            self.tcp.server_close()
            return
        self.udp.shutdown()
        self.tcp.shutdown()
        self.udp.server_close()
        self.tcp.server_close()
        for thread in self.threads:
            thread.join(SHUTDOWN_TIMEOUT)
        self.sink.close()
        self.started = False


def sd_notify(message: str) -> bool:
    notify_path = os.environ.get("NOTIFY_SOCKET")
    if not notify_path:
        return False
    if notify_path.startswith("@"):
        notify_path = "\0" + notify_path[1:]
    elif not notify_path.startswith("/"):
        raise DNSAuditError("NOTIFY_SOCKET must be an absolute path or abstract socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2.0)
        sock.sendto(message.encode("ascii"), notify_path)
    except OSError as exc:
        raise DNSAuditError("could not send systemd readiness notification") from exc
    finally:
        sock.close()
    return True


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--upstream-port", type=int, default=53)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-dns-auditd must run as root")
    if not 0 <= args.port <= 65_535 or not 1 <= args.upstream_port <= 65_535:
        parser.error("invalid DNS port")
    if not 0 <= args.uid <= 2_147_483_647 or not 0 <= args.gid <= 2_147_483_647:
        parser.error("invalid agent identity")
    try:
        proxy = DNSProxy(
            args.listen, args.port, args.upstream, args.upstream_port,
            args.uid, args.gid, args.socket,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    stopping = threading.Event()
    previous_handler = signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())
    try:
        proxy.start()
        sd_notify("READY=1")
        stopping.wait()
        return 0
    except (DNSAuditError, OSError) as exc:
        print(f"diary-dns-auditd: {exc}", file=sys.stderr)
        return 1
    finally:
        proxy.close()
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(daemon_main())
