#!/usr/bin/python3
"""Forward trusted NFLOG flow metadata to the existing diary-lock audit writer."""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

DEFAULT_GROUP = 30
DEFAULT_SOCKET = "/run/diary-audit/socket"
SNAPLEN = 256
RECEIVE_BUFFER = 4 * 1024 * 1024
QUEUE_SIZE = 1024
AUDIT_TIMEOUT = 2.0
HEALTH_INTERVAL = 10.0
SHUTDOWN_TIMEOUT = 5.0
NETLINK_NETFILTER = 12
NFNL_SUBSYS_ULOG = 4
NFULNL_MSG_PACKET = (NFNL_SUBSYS_ULOG << 8)
NFULNL_MSG_CONFIG = NFULNL_MSG_PACKET | 1
NFULNL_CFG_CMD_BIND = 1
NFULNL_COPY_PACKET = 2
NFULNL_CFG_F_SEQ = 0x0001
NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04
NLMSG_ERROR = 0x02
NLA_TYPE_MASK = 0x3FFF
NFULA_PACKET_HDR = 1
NFULA_PAYLOAD = 9
NFULA_PREFIX = 10
NFULA_UID = 11
NFULA_SEQ = 12
NFULA_GID = 14
NFULA_CFG_CMD = 1
NFULA_CFG_MODE = 2
NFULA_CFG_FLAGS = 5
PREFIX_OPERATIONS = {
    "diary-audit-new": "connect",
    "diary-audit-established": "flow",
}
TCP = 6
UDP = 17
EXTENSION_HEADERS = {0, 43, 60, 135, 139, 140}
FRAGMENT = 44
AUTHENTICATION = 51
NO_NEXT_HEADER = 59
MAX_NETLINK_DATAGRAM = 1024 * 1024
MAX_AUDIT_RESPONSE = 64 * 1024


class NetworkAuditError(ValueError):
    """A kernel message or audit response failed closed validation."""


class UnsupportedPacket(ValueError):
    """A packet has no auditable transport flow in the copied bytes."""


class ShutdownRequested(Exception):
    """SIGTERM requested a bounded, graceful forwarding shutdown."""


class WorkerState:
    def __init__(self) -> None:
        self.reported_version = 0
        self.in_flight: str | None = None
        self.sentinel_done = False
        self.exited = False


_WORK_SENTINEL = object()


class LossTracker:
    """Thread-safe, bounded-cardinality observation and writer failure counters."""

    KINDS = ("sequence_gap", "enobufs", "queue_overflow", "audit_unavailable", "shutdown_loss")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {kind: 0 for kind in self.KINDS}
        self._version = 0
        self._last_sequence: int | None = None

    def add(self, kind: str, count: int = 1) -> None:
        if kind not in self.KINDS or count < 1:
            raise NetworkAuditError("invalid network audit loss counter")
        with self._lock:
            self._counts[kind] += count
            self._version += 1

    def sequence(self, value: int) -> None:
        with self._lock:
            if self._last_sequence is not None:
                expected = (self._last_sequence + 1) & 0xFFFF_FFFF
                if value != expected:
                    gap = (value - expected) & 0xFFFF_FFFF
                    self._counts["sequence_gap"] += min(gap, 0x7FFF_FFFF) or 1
                    self._version += 1
            self._last_sequence = value

    def snapshot(self) -> tuple[int, dict[str, int]]:
        with self._lock:
            return self._version, {kind: count for kind, count in self._counts.items() if count}


def _aligned(length: int) -> int:
    return (length + 3) & ~3


def _attribute(kind: int, payload: bytes) -> bytes:
    length = 4 + len(payload)
    return struct.pack("HH", length, kind) + payload + bytes(_aligned(length) - length)


def _attributes(data: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise NetworkAuditError("truncated netlink attribute header")
        length, kind = struct.unpack_from("HH", data, offset)
        if length < 4 or length > len(data) - offset:
            raise NetworkAuditError("invalid netlink attribute length")
        yield kind & NLA_TYPE_MASK, data[offset + 4:offset + length]
        next_offset = offset + _aligned(length)
        if next_offset > len(data) and next_offset != _aligned(len(data)):
            raise NetworkAuditError("truncated netlink attribute padding")
        offset = next_offset


def _message(kind: int, payload: bytes, sequence: int = 0) -> bytes:
    length = 16 + len(payload)
    return struct.pack("IHHII", length, kind, 0, sequence, 0) + payload


def _messages(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise NetworkAuditError("truncated netlink message header")
        length, kind, _flags, sequence, _pid = struct.unpack_from("IHHII", data, offset)
        if length < 16 or length > len(data) - offset:
            raise NetworkAuditError("invalid netlink message length")
        yield kind, sequence, data[offset + 16:offset + length]
        next_offset = offset + _aligned(length)
        if next_offset > len(data) and next_offset != _aligned(len(data)):
            raise NetworkAuditError("truncated netlink message padding")
        offset = next_offset


def bind_request(group: int, sequence: int) -> bytes:
    if not 1 <= group <= 65_535:
        raise NetworkAuditError("NFLOG group must be between 1 and 65535")
    body = struct.pack("BBH", socket.AF_UNSPEC, 0, socket.htons(group))
    body += _attribute(NFULA_CFG_CMD, bytes([NFULNL_CFG_CMD_BIND]))
    body += _attribute(NFULA_CFG_MODE, struct.pack("!IBB", SNAPLEN, NFULNL_COPY_PACKET, 0))
    body += _attribute(NFULA_CFG_FLAGS, struct.pack("!H", NFULNL_CFG_F_SEQ))
    header = struct.pack(
        "IHHII", 16 + len(body), NFULNL_MSG_CONFIG, NLM_F_REQUEST | NLM_F_ACK, sequence, 0,
    )
    return header + body


def _acknowledge(sock: socket.socket, sequence: int) -> None:
    while True:
        data = sock.recv(65_535)
        for kind, received_sequence, payload in _messages(data):
            if kind != NLMSG_ERROR or received_sequence != sequence:
                continue
            if len(payload) < 4:
                raise NetworkAuditError("truncated netlink acknowledgement")
            error = struct.unpack_from("i", payload)[0]
            if error:
                raise OSError(-error, os.strerror(-error))
            return


def open_nflog(group: int) -> socket.socket:
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER)
        sock.bind((0, 0))
        sock.settimeout(5.0)
        sequence = int(time.monotonic_ns() & 0x7FFF_FFFF)
        sock.sendto(bind_request(group, sequence), (0, 0))
        _acknowledge(sock, sequence)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def flow_from_packet(packet: bytes, family: int) -> tuple[str, int, str]:
    if family == socket.AF_INET:
        if len(packet) < 20 or packet[0] >> 4 != 4:
            raise UnsupportedPacket("invalid IPv4 packet")
        header_length = (packet[0] & 0x0F) * 4
        if header_length < 20 or len(packet) < header_length:
            raise UnsupportedPacket("invalid IPv4 header length")
        fragment = struct.unpack_from("!H", packet, 6)[0]
        if fragment & 0x1FFF:
            raise UnsupportedPacket("non-initial IPv4 fragment")
        protocol_number = packet[9]
        destination = str(ipaddress.IPv4Address(packet[16:20]))
        offset = header_length
    elif family == socket.AF_INET6:
        if len(packet) < 40 or packet[0] >> 4 != 6:
            raise UnsupportedPacket("invalid IPv6 packet")
        destination = str(ipaddress.IPv6Address(packet[24:40]))
        protocol_number = packet[6]
        offset = 40
        for _ in range(16):
            if protocol_number in EXTENSION_HEADERS:
                if len(packet) < offset + 2:
                    raise UnsupportedPacket("truncated IPv6 extension header")
                next_header = packet[offset]
                length = (packet[offset + 1] + 1) * 8
            elif protocol_number == FRAGMENT:
                if len(packet) < offset + 8:
                    raise UnsupportedPacket("truncated IPv6 fragment header")
                next_header = packet[offset]
                fragment = struct.unpack_from("!H", packet, offset + 2)[0]
                if fragment & 0xFFF8:
                    raise UnsupportedPacket("non-initial IPv6 fragment")
                length = 8
            elif protocol_number == AUTHENTICATION:
                if len(packet) < offset + 2:
                    raise UnsupportedPacket("truncated IPv6 authentication header")
                next_header = packet[offset]
                length = (packet[offset + 1] + 2) * 4
            else:
                break
            if length < 8 or len(packet) < offset + length:
                raise UnsupportedPacket("invalid IPv6 extension length")
            protocol_number = next_header
            offset += length
        else:
            raise UnsupportedPacket("too many IPv6 extension headers")
        if protocol_number == NO_NEXT_HEADER:
            raise UnsupportedPacket("IPv6 packet has no transport header")
    else:
        raise UnsupportedPacket("NFLOG packet is not IPv4 or IPv6")
    if protocol_number not in (TCP, UDP) or len(packet) < offset + 4:
        raise UnsupportedPacket("packet has no complete TCP/UDP header")
    destination_port = struct.unpack_from("!H", packet, offset + 2)[0]
    if destination_port == 0:
        raise UnsupportedPacket("destination port is zero")
    protocol = "tcp" if protocol_number == TCP else "udp"
    return destination, destination_port, protocol


def event_from_packet_message(
    payload: bytes,
    expected_group: int,
    expected_uid: int,
    expected_gid: int,
    sequence_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    if len(payload) < 4:
        raise NetworkAuditError("truncated NFLOG packet header")
    family, version, resource = struct.unpack_from("BBH", payload)
    if version != 0 or socket.ntohs(resource) != expected_group:
        raise NetworkAuditError("unexpected NFLOG version or group")
    attributes = dict(_attributes(payload[4:]))
    required = {NFULA_PREFIX, NFULA_PAYLOAD, NFULA_UID, NFULA_GID, NFULA_SEQ}
    if not required <= attributes.keys():
        raise NetworkAuditError("NFLOG packet lacks payload, sequence, or trusted identity")
    try:
        prefix = attributes[NFULA_PREFIX].rstrip(b"\0").decode("ascii")
    except UnicodeDecodeError as exc:
        raise NetworkAuditError("NFLOG prefix is not ASCII") from exc
    operation = PREFIX_OPERATIONS.get(prefix)
    if operation is None:
        raise NetworkAuditError("NFLOG packet has an unexpected prefix")
    if any(len(attributes[field]) != 4 for field in (NFULA_UID, NFULA_GID, NFULA_SEQ)):
        raise NetworkAuditError("invalid NFLOG identity or sequence attribute")
    uid = struct.unpack("!I", attributes[NFULA_UID])[0]
    gid = struct.unpack("!I", attributes[NFULA_GID])[0]
    sequence = struct.unpack("!I", attributes[NFULA_SEQ])[0]
    if uid != expected_uid or gid != expected_gid:
        raise NetworkAuditError("NFLOG packet identity does not match agent")
    if sequence_callback is not None:
        sequence_callback(sequence)
    destination, port, protocol = flow_from_packet(attributes[NFULA_PAYLOAD], family)
    return {
        "event": "network.flow",
        "component": "nftables",
        "operation": operation,
        "decision": "would_deny",
        "lock_state": "not_applicable",
        "reason": "audit_only",
        "destination_ip": destination,
        "destination_port": port,
        "protocol": protocol,
        "caller": {"pid": 0, "uid": uid, "gid": gid},
    }


def events_from_datagram(
    data: bytes,
    expected_group: int,
    expected_uid: int,
    expected_gid: int,
    sequence_callback: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    events = []
    for kind, _sequence, payload in _messages(data):
        if kind != NFULNL_MSG_PACKET:
            continue
        try:
            events.append(event_from_packet_message(
                payload, expected_group, expected_uid, expected_gid, sequence_callback,
            ))
        except UnsupportedPacket:
            continue
    return events


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
                raise NetworkAuditError("audit response is too large")
        reply = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise NetworkAuditError(f"cannot use trusted audit socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error", "audit request failed") if isinstance(reply, dict) else "audit request failed"
        raise NetworkAuditError(str(error))
    return reply


def sd_notify(message: str) -> bool:
    notify_path = os.environ.get("NOTIFY_SOCKET")
    if not notify_path:
        return False
    if notify_path.startswith("@"):
        notify_path = "\0" + notify_path[1:]
    elif not notify_path.startswith("/"):
        raise NetworkAuditError("NOTIFY_SOCKET must be an absolute path or abstract socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2.0)
        sock.sendto(message.encode("ascii"), notify_path)
    except OSError as exc:
        raise NetworkAuditError("could not send systemd readiness notification") from exc
    finally:
        sock.close()
    return True


def health_event(counts: dict[str, int]) -> dict[str, Any]:
    operation = "receive" if any(counts.get(kind) for kind in LossTracker.KINDS[:3]) else "audit_write"
    return {
        "event": "network.audit_health",
        "component": "nflog",
        "operation": operation,
        "decision": "error",
        "lock_state": "not_applicable",
        "reason": "observation_loss",
        "unit": "diary-network-auditd.service",
    }


def _write_event(
    state: WorkerState,
    kind: str,
    socket_path: str,
    writer: Callable[[str, dict[str, Any]], dict[str, Any]],
    event: dict[str, Any],
) -> dict[str, Any]:
    state.in_flight = kind
    try:
        return writer(socket_path, event)
    finally:
        state.in_flight = None


def _emit_health(
    state: WorkerState,
    loss: LossTracker,
    socket_path: str,
    writer: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> None:
    version, counts = loss.snapshot()
    summary = " ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"diary-network-auditd: observation loss: {summary}", file=sys.stderr, flush=True)
    try:
        _write_event(state, "health", socket_path, writer, health_event(counts))
    except (NetworkAuditError, OSError):
        loss.add("audit_unavailable")
    else:
        state.reported_version = version


def _audit_worker(
    work: queue.Queue[Any],
    loss: LossTracker,
    socket_path: str,
    writer: Callable[[str, dict[str, Any]], dict[str, Any]],
    health_interval: float,
    state: WorkerState | None = None,
) -> None:
    state = state or WorkerState()
    last_attempt = 0.0
    try:
        while True:
            version, _counts = loss.snapshot()
            now = time.monotonic()
            if version != state.reported_version and now - last_attempt >= health_interval:
                last_attempt = now
                _emit_health(state, loss, socket_path, writer)
            try:
                event = work.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is _WORK_SENTINEL:
                try:
                    if loss.snapshot()[0] != state.reported_version:
                        _emit_health(state, loss, socket_path, writer)
                    return
                finally:
                    work.task_done()
                    state.sentinel_done = True
            try:
                _write_event(state, "flow", socket_path, writer, event)
            except (NetworkAuditError, OSError):
                loss.add("audit_unavailable")
            finally:
                work.task_done()
    finally:
        state.exited = True


def serve(
    group: int,
    uid: int,
    gid: int,
    socket_path: str,
    opener: Callable[[int], Any] = open_nflog,
    writer: Callable[[str, dict[str, Any]], dict[str, Any]] = request,
    queue_size: int = QUEUE_SIZE,
    health_interval: float = HEALTH_INTERVAL,
    loss: LossTracker | None = None,
    state: WorkerState | None = None,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT,
) -> None:
    loss = loss or LossTracker()
    state = state or WorkerState()
    work: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
    sock = opener(group)
    worker = threading.Thread(
        target=_audit_worker,
        args=(work, loss, socket_path, writer, health_interval, state),
        name="diary-network-audit-writer",
        daemon=True,
    )
    worker.start()
    try:
        sd_notify("READY=1")
        while True:
            try:
                data = sock.recv(MAX_NETLINK_DATAGRAM)
            except OSError as exc:
                if exc.errno == errno.ENOBUFS:
                    loss.add("enobufs")
                    continue
                raise
            events = events_from_datagram(data, group, uid, gid, loss.sequence)
            for event in events:
                try:
                    work.put_nowait(event)
                except queue.Full:
                    loss.add("queue_overflow")
    finally:
        sock.close()
        discarded = 0
        try:
            work.put_nowait(_WORK_SENTINEL)
        except queue.Full:
            try:
                work.get_nowait()
                work.task_done()
                discarded = 1
            except queue.Empty:
                pass
            work.put_nowait(_WORK_SENTINEL)
        worker.join(shutdown_timeout)
        with work.mutex:
            unfinished = work.unfinished_tasks
            sentinel_done = state.sentinel_done
        discarded += max(0, unfinished - (0 if sentinel_done else 1))
        if loss.snapshot()[0] != state.reported_version:
            discarded += 1
        if discarded:
            loss.add("shutdown_loss", discarded)
            _version, counts = loss.snapshot()
            summary = " ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
            print(f"diary-network-auditd: shutdown loss: {summary}", file=sys.stderr, flush=True)


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    raise ShutdownRequested


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--group", type=int, default=DEFAULT_GROUP)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-network-auditd must run as root")
    if not 1 <= args.group <= 65_535 or not 0 <= args.uid <= 2_147_483_647 or not 0 <= args.gid <= 2_147_483_647:
        parser.error("invalid NFLOG group or agent identity")
    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        try:
            serve(args.group, args.uid, args.gid, args.socket)
        except ShutdownRequested:
            return 0
        except (NetworkAuditError, OSError) as exc:
            print(f"diary-network-auditd: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(daemon_main())
