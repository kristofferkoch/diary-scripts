#!/usr/bin/python3
"""Per-identity UDP/TCP DNS forwarding gated by trusted lock policy."""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from typing import Any, Protocol, cast

if __package__ in {None, ""}:
    sys.path.insert(0, "/usr/local/lib/diary")
from scripts.diary_dns_policy import PolicyClient

DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_PORT = 1053
DEFAULT_POLICY_SOCKET = "/run/diary-dns-policy/socket"
UPSTREAM_TIMEOUT = 5.0
MAX_CLIENTS = 128
SHUTDOWN_TIMEOUT = 5.0


class DNSAuditError(ValueError):
    """DNS metadata or the trusted policy channel failed validation."""


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


def refusal(message: bytes, protocol: str) -> bytes | None:
    """Build a local REFUSED response without contacting a resolver."""
    data = message[2:] if protocol == "tcp" else message
    if len(data) < 12:
        return None
    identifier, flags, questions = struct.unpack_from("!HHH", data)
    response_flags = 0x8000 | (flags & 0x7900) | 5
    question = b""
    question_count = 0
    if not flags & 0x8000 and questions == 1:
        try:
            _name, offset = _labels(data, 12)
        except DNSAuditError:
            pass
        else:
            if offset + 4 <= len(data):
                question = data[12:offset + 4]
                question_count = 1
    response = struct.pack("!HHHHHH", identifier, response_flags, question_count, 0, 0, 0) + question
    return struct.pack("!H", len(response)) + response if protocol == "tcp" else response


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
    block_on_close = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.client_slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.active_condition = threading.Condition()
        self.active = 0
        super().__init__(*args, **kwargs)

    def _request_done(self) -> None:
        with self.active_condition:
            self.active -= 1
            if self.active == 0:
                self.active_condition.notify_all()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.client_slots.acquire(blocking=False):
            cast(Any, self).shutdown_request(request)
            return
        with self.active_condition:
            self.active += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_done()
            self.client_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_done()
            self.client_slots.release()

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.active_condition:
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.active_condition.wait(remaining)
            return True


class _UDPServer(_BoundedThreads, socketserver.UDPServer):
    allow_reuse_address = True
    max_packet_size = 65_535
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


class PolicyClientProtocol(Protocol):
    def authorize(self, name: str | None, protocol: str) -> Any: ...


class DNSProxy:
    def __init__(
        self,
        listen: str,
        port: int,
        upstream: str,
        upstream_port: int,
        uid: int,
        gid: int,
        policy_socket: str = DEFAULT_POLICY_SOCKET,
        policy_client: PolicyClientProtocol | None = None,
    ) -> None:
        self.listen = str(ipaddress.IPv4Address(listen))
        self.upstream = str(ipaddress.IPv4Address(upstream))
        self.upstream_port = upstream_port
        self.uid = uid
        self.gid = gid
        self.policy = policy_client or PolicyClient(policy_socket)
        self.stopping = threading.Event()
        self.tcp_lock = threading.Lock()
        self.tcp_clients: set[socket.socket] = set()
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
        for thread in self.threads:
            thread.start()
        self.started = True

    def policy_decision(self, message: bytes, protocol: str) -> tuple[bool, bytes | None]:
        decision = self.policy.authorize(query_name(message, protocol), protocol)
        return decision.allow, None if decision.allow else refusal(message, protocol)

    def handle_udp(self, message: bytes, client: tuple[str, int], listener: socket.socket) -> None:
        allowed, rejected = self.policy_decision(message, "udp")
        if not allowed:
            if rejected is not None:
                listener.sendto(rejected, client)
            return
        response = exchange_udp(message, (self.upstream, self.upstream_port))
        if response is not None:
            listener.sendto(response, client)

    def handle_tcp(self, client: socket.socket) -> None:
        with self.tcp_lock:
            if self.stopping.is_set():
                return
            self.tcp_clients.add(client)
        try:
            client.settimeout(UPSTREAM_TIMEOUT)
            while not self.stopping.is_set():
                header = _receive_exact(client, 2)
                if header is None:
                    return
                length = struct.unpack("!H", header)[0]
                if length == 0:
                    return
                message = _receive_exact(client, length)
                if message is None:
                    return
                framed = header + message
                allowed, rejected = self.policy_decision(framed, "tcp")
                if not allowed:
                    if rejected is not None:
                        client.sendall(rejected)
                    continue
                response = exchange_tcp(message, (self.upstream, self.upstream_port))
                if response is None:
                    return
                client.sendall(struct.pack("!H", len(response)) + response)
        except OSError:
            return
        finally:
            with self.tcp_lock:
                self.tcp_clients.discard(client)

    def _close_tcp_clients(self) -> None:
        with self.tcp_lock:
            clients = list(self.tcp_clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        self.stopping.set()
        if not self.started:
            self.udp.server_close()
            self.tcp.server_close()
            return
        self.udp.shutdown()
        self.tcp.shutdown()
        self.udp.server_close()
        self.tcp.server_close()
        self._close_tcp_clients()
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        self.udp.wait_idle(max(0, deadline - time.monotonic()))
        self.tcp.wait_idle(max(0, deadline - time.monotonic()))
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
    parser.add_argument("--policy-socket", default=DEFAULT_POLICY_SOCKET)
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
            args.uid, args.gid, args.policy_socket,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(daemon_main())
