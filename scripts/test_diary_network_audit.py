"""Parser and audit-boundary tests for UID-based network flow observation."""
from __future__ import annotations

import grp
import json
import os
import socket
import struct
import threading
from pathlib import Path

import pytest

from scripts import diary_audit as audit
from scripts import diary_network_audit as network


UID = 1001
GID = 1002
GROUP = 30
WIRE_IPV4 = bytes.fromhex(
    "640000000004000007000000000000000200001e080001000000000014000a00"
    "64696172792d61756469742d6e65770008000b00000003e908000e00000003ea"
    "08000c00000000071c000900450000180000000040060000c000020ac6336414109201bb"
)
WIRE_IPV6 = bytes.fromhex(
    "780000000004000007000000000000000a00001e080001000000000014000a00"
    "64696172792d61756469742d6e65770008000b00000003e908000e00000003ea"
    "08000c000000000730000900600000040000064020010db8000000000000000000000010"
    "20010db8000000000000000000000020109201bb"
)


def ipv4(protocol: int = network.TCP, port: int = 443, fragment: int = 0) -> bytes:
    packet = bytearray(24)
    packet[0] = 0x45
    packet[2:4] = struct.pack("!H", len(packet))
    packet[6:8] = struct.pack("!H", fragment)
    packet[8] = 64
    packet[9] = protocol
    packet[12:16] = socket.inet_aton("192.0.2.10")
    packet[16:20] = socket.inet_aton("198.51.100.20")
    packet[20:24] = struct.pack("!HH", 4242, port)
    return bytes(packet)


def ipv6(protocol: int = network.TCP, port: int = 443, extension: bool = False) -> bytes:
    first = network.EXTENSION_HEADERS.copy().pop() if extension else protocol
    packet = bytearray(40)
    packet[0] = 0x60
    packet[6] = first
    packet[7] = 64
    packet[8:24] = socket.inet_pton(socket.AF_INET6, "2001:db8::10")
    packet[24:40] = socket.inet_pton(socket.AF_INET6, "2001:db8::20")
    if extension:
        packet[2:4] = struct.pack("!H", 12)
        packet.extend([protocol, 0, 0, 0, 0, 0, 0, 0])
    else:
        packet[2:4] = struct.pack("!H", 4)
    packet.extend(struct.pack("!HH", 4242, port))
    return bytes(packet)


def packet_message(
    packet: bytes,
    family: int = socket.AF_INET,
    prefix: bytes = b"diary-audit-new\0",
    uid: int = UID,
    gid: int = GID,
    group: int = GROUP,
    sequence: int = 7,
) -> bytes:
    body = struct.pack("BBH", family, 0, socket.htons(group))
    body += network._attribute(network.NFULA_PACKET_HDR, bytes(4))
    body += network._attribute(network.NFULA_PREFIX, prefix)
    body += network._attribute(network.NFULA_UID, struct.pack("!I", uid))
    body += network._attribute(network.NFULA_GID, struct.pack("!I", gid))
    body += network._attribute(network.NFULA_SEQ, struct.pack("!I", sequence))
    body += network._attribute(network.NFULA_PAYLOAD, packet)
    return network._message(network.NFULNL_MSG_PACKET, body, sequence=7)


def test_bind_request_subscribes_to_bounded_packet_copy() -> None:
    request = network.bind_request(GROUP, 123)
    length, kind, flags, sequence, _pid = struct.unpack_from("IHHII", request)
    assert length == len(request)
    assert kind == network.NFULNL_MSG_CONFIG
    assert flags == network.NLM_F_REQUEST | network.NLM_F_ACK
    assert sequence == 123
    family, version, resource = struct.unpack_from("BBH", request, 16)
    assert family == socket.AF_UNSPEC and version == 0
    assert socket.ntohs(resource) == GROUP
    attributes = dict(network._attributes(request[20:]))
    assert attributes[network.NFULA_CFG_CMD] == bytes([network.NFULNL_CFG_CMD_BIND])
    assert struct.unpack("!IBB", attributes[network.NFULA_CFG_MODE]) == (
        network.SNAPLEN, network.NFULNL_COPY_PACKET, 0,
    )
    assert struct.unpack("!H", attributes[network.NFULA_CFG_FLAGS])[0] == network.NFULNL_CFG_F_SEQ


@pytest.mark.parametrize(("wire", "destination"), [(WIRE_IPV4, "198.51.100.20"), (WIRE_IPV6, "2001:db8::20")])
def test_literal_kernel_wire_fixtures_use_big_endian_identity(wire: bytes, destination: str) -> None:
    events = network.events_from_datagram(wire, GROUP, UID, GID)
    assert len(events) == 1
    assert events[0]["caller"] == {"pid": 0, "uid": 1001, "gid": 1002}
    assert events[0]["destination_ip"] == destination


@pytest.mark.parametrize("group", [0, -1, 65_536])
def test_bind_request_rejects_invalid_group(group: int) -> None:
    with pytest.raises(network.NetworkAuditError):
        network.bind_request(group, 1)


@pytest.mark.parametrize(
    ("packet", "family", "destination", "protocol"),
    [
        (ipv4(), socket.AF_INET, "198.51.100.20", "tcp"),
        (ipv4(network.UDP, 53), socket.AF_INET, "198.51.100.20", "udp"),
        (ipv6(), socket.AF_INET6, "2001:db8::20", "tcp"),
        (ipv6(network.UDP, 53), socket.AF_INET6, "2001:db8::20", "udp"),
        (ipv6(extension=True), socket.AF_INET6, "2001:db8::20", "tcp"),
    ],
)
def test_flow_parser_covers_families_protocols_and_extensions(
    packet: bytes, family: int, destination: str, protocol: str,
) -> None:
    assert network.flow_from_packet(packet, family) == (destination, 443 if protocol == "tcp" else 53, protocol)


def test_new_flow_event_has_kernel_agent_attribution_and_no_payload() -> None:
    message = packet_message(ipv4() + b"private request body")
    event = network.events_from_datagram(message, GROUP, UID, GID)[0]
    assert event == {
        "event": "network.flow",
        "component": "nftables",
        "operation": "connect",
        "decision": "would_deny",
        "lock_state": "not_applicable",
        "reason": "audit_only",
        "destination_ip": "198.51.100.20",
        "destination_port": 443,
        "protocol": "tcp",
        "caller": {"pid": 0, "uid": UID, "gid": GID},
    }
    normalized, redacted = audit.normalize_event(event)
    assert normalized == event and redacted == []
    assert "private" not in json.dumps(event)


def test_established_flow_is_would_block_without_changing_lock_state() -> None:
    message = packet_message(ipv6(), socket.AF_INET6, b"diary-audit-established\0")
    event = network.events_from_datagram(message, GROUP, UID, GID)[0]
    assert event["operation"] == "flow"
    assert event["decision"] == "would_deny"
    assert event["lock_state"] == "not_applicable"


def test_one_datagram_can_contain_multiple_packet_messages() -> None:
    data = packet_message(ipv4()) + packet_message(ipv6(), socket.AF_INET6)
    events = network.events_from_datagram(data, GROUP, UID, GID)
    assert [event["destination_ip"] for event in events] == ["198.51.100.20", "2001:db8::20"]


@pytest.mark.parametrize(
    "change",
    [
        {"prefix": b"untrusted-prefix\0"},
        {"uid": UID + 1},
        {"gid": GID + 1},
        {"group": GROUP + 1},
    ],
)
def test_unexpected_prefix_or_identity_fails_closed(change: dict) -> None:
    message = packet_message(ipv4(), **change)
    with pytest.raises(network.NetworkAuditError):
        network.events_from_datagram(message, GROUP, UID, GID)


def test_missing_kernel_identity_is_not_replaced_with_writer_identity() -> None:
    message = bytearray(packet_message(ipv4()))
    with pytest.raises(network.NetworkAuditError):
        network.event_from_packet_message(b"\x02\x00\x00\x00", GROUP, UID, GID)
    assert message


@pytest.mark.parametrize(
    "packet",
    [
        ipv4(fragment=1),
        ipv4(port=0),
        ipv4(network.TCP)[:19],
        ipv4(1),
        ipv6()[:39],
        ipv6(network.NO_NEXT_HEADER),
        b"not an IP packet",
    ])
def test_unsupported_or_truncated_packets_are_not_invented_as_flows(packet: bytes) -> None:
    family = socket.AF_INET6 if packet.startswith(b"\x60") else socket.AF_INET
    with pytest.raises(network.UnsupportedPacket):
        network.flow_from_packet(packet, family)


def test_noninitial_ipv6_fragment_is_skipped_without_ports() -> None:
    packet = bytearray(48)
    packet[0] = 0x60
    packet[6] = network.FRAGMENT
    packet[40] = network.TCP
    packet[42:44] = struct.pack("!H", 8)
    packet[24:40] = socket.inet_pton(socket.AF_INET6, "2001:db8::20")
    with pytest.raises(network.UnsupportedPacket, match="non-initial"):
        network.flow_from_packet(bytes(packet), socket.AF_INET6)


@pytest.mark.parametrize("data", [b"", b"short", bytes(16), b"\x14\x00\x00\x00\x00\x04" + bytes(10)])
def test_malformed_netlink_messages_fail_closed(data: bytes) -> None:
    if not data:
        assert network.events_from_datagram(data, GROUP, UID, GID) == []
    else:
        with pytest.raises(network.NetworkAuditError):
            network.events_from_datagram(data, GROUP, UID, GID)


def test_sequence_gaps_and_wraparound_are_counted() -> None:
    loss = network.LossTracker()
    data = packet_message(ipv4(), sequence=10) + packet_message(ipv4(), sequence=12)
    assert len(network.events_from_datagram(data, GROUP, UID, GID, loss.sequence)) == 2
    assert loss.snapshot()[1] == {"sequence_gap": 1}
    wrapped = network.LossTracker()
    wrapped.sequence(0xFFFF_FFFF)
    wrapped.sequence(0)
    assert wrapped.snapshot()[1] == {}


def test_unsupported_fragment_does_not_create_false_sequence_gap() -> None:
    loss = network.LossTracker()
    data = b"".join([
        packet_message(ipv4(), sequence=1),
        packet_message(ipv4(fragment=1), sequence=2),
        packet_message(ipv4(), sequence=3),
    ])
    events = network.events_from_datagram(data, GROUP, UID, GID, loss.sequence)
    assert len(events) == 2
    assert loss.snapshot()[1] == {}


def test_serve_forwards_only_parsed_events_and_closes_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def recv(self, _size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return packet_message(ipv4())
            raise OSError("stop test")

        def close(self) -> None:
            self.closed = True

    sock = FakeSocket()
    written = []
    delivered = threading.Event()
    ready = []

    def writer(path, event):
        written.append((path, event))
        delivered.set()
        return {"ok": True}

    state = network.WorkerState()
    loss = network.LossTracker()
    monkeypatch.setattr(network, "sd_notify", lambda message: ready.append(message) or True)
    with pytest.raises(OSError, match="stop test"):
        network.serve(
            GROUP, UID, GID, "/run/test/audit.sock",
            opener=lambda group: sock,
            writer=writer, state=state, loss=loss,
        )
    assert delivered.wait(2)
    assert state.exited is True
    assert loss.snapshot()[1] == {}
    assert ready == ["READY=1"]
    assert sock.closed is True
    assert written[0][0] == "/run/test/audit.sock"
    assert written[0][1]["caller"]["uid"] == UID


def test_open_nflog_sizes_receive_buffer_before_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.options = []
            self.bound = None
            self.timeouts = []
            self.sent = None

        def setsockopt(self, *args) -> None:
            self.options.append(args)

        def bind(self, address) -> None:
            self.bound = address

        def settimeout(self, value) -> None:
            self.timeouts.append(value)

        def sendto(self, data, address) -> None:
            self.sent = (data, address)

        def close(self) -> None:
            pass

    sock = FakeSocket()
    monkeypatch.setattr(network.socket, "socket", lambda *args: sock)
    monkeypatch.setattr(network, "_acknowledge", lambda *args: None)
    assert network.open_nflog(GROUP) is sock
    assert sock.options == [(socket.SOL_SOCKET, socket.SO_RCVBUF, network.RECEIVE_BUFFER)]
    assert sock.bound == (0, 0)
    assert sock.timeouts == [5.0, None]
    assert sock.sent is not None
    assert sock.sent[1] == (0, 0)


def test_audit_socket_stall_has_short_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class StalledSocket:
        def __init__(self) -> None:
            self.timeout = None
            self.closed = False

        def settimeout(self, value) -> None:
            self.timeout = value

        def connect(self, _path) -> None:
            pass

        def sendall(self, _data) -> None:
            pass

        def recv(self, _size) -> bytes:
            raise TimeoutError("stalled")

        def close(self) -> None:
            self.closed = True

    sock = StalledSocket()
    monkeypatch.setattr(network.socket, "socket", lambda *args: sock)
    with pytest.raises(network.NetworkAuditError, match="stalled"):
        network.request("/run/test/stalled.sock", {"event": "test"})
    assert sock.timeout == network.AUDIT_TIMEOUT == 2.0
    assert sock.closed is True


def test_burst_overflow_does_not_block_receiver_and_emits_health(monkeypatch: pytest.MonkeyPatch) -> None:
    receive_done = threading.Event()

    class FakeSocket:
        def __init__(self) -> None:
            self.reads = 0

        def recv(self, _size) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return packet_message(ipv4(), sequence=1)
            if self.reads == 2:
                if not writer_started.wait(2):
                    raise OSError("writer did not start")
                return b"".join(packet_message(ipv4(), sequence=index) for index in range(2, 6))
            receive_done.set()
            raise OSError("stop test")

        def close(self) -> None:
            pass

    writer_started = threading.Event()
    release_writer = threading.Event()
    health_written = threading.Event()
    flows = []
    health = []

    def writer(_path, event):
        if event["event"] == "network.audit_health":
            health.append(event)
            health_written.set()
        else:
            flows.append(event)
            writer_started.set()
            release_writer.wait(2)
        return {"ok": True}

    loss = network.LossTracker()
    state = network.WorkerState()
    errors = []
    monkeypatch.setattr(network, "sd_notify", lambda _: True)

    def run() -> None:
        try:
            network.serve(
                GROUP, UID, GID, "/run/test/audit.sock",
                opener=lambda _: FakeSocket(), writer=writer,
                queue_size=1, health_interval=0, loss=loss, state=state,
            )
        except OSError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert writer_started.wait(2)
    assert receive_done.wait(2)
    assert not release_writer.is_set()
    assert loss.snapshot()[1]["queue_overflow"] >= 1
    assert thread.is_alive()
    release_writer.set()
    thread.join(2)
    assert not thread.is_alive()
    assert state.exited is True
    assert len(errors) == 1 and str(errors[0]) == "stop test"
    assert health_written.wait(2)
    assert health[0]["operation"] == "receive"
    assert health[0]["reason"] == "observation_loss"
    assert len(flows) < 4
    assert loss.snapshot()[1]["shutdown_loss"] == 1


def test_enobufs_is_reported_and_receiving_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.reads = 0

        def recv(self, _size) -> bytes:
            self.reads += 1
            if self.reads == 1:
                raise OSError(network.errno.ENOBUFS, "receive buffer exhausted")
            if self.reads == 2:
                return packet_message(ipv4())
            raise OSError("stop test")

        def close(self) -> None:
            pass

    flow_written = threading.Event()
    health_written = threading.Event()

    def writer(_path, event):
        if event["event"] == "network.flow":
            flow_written.set()
        else:
            health_written.set()
        return {"ok": True}

    loss = network.LossTracker()
    monkeypatch.setattr(network, "sd_notify", lambda _: True)
    with pytest.raises(OSError, match="stop test"):
        network.serve(
            GROUP, UID, GID, "/run/test/audit.sock",
            opener=lambda _: FakeSocket(), writer=writer,
            health_interval=0, loss=loss,
        )
    assert flow_written.wait(2)
    assert health_written.wait(2)
    assert loss.snapshot()[1]["enobufs"] == 1


def test_audit_writer_outage_emits_bounded_health_after_recovery() -> None:
    work = network.queue.Queue()
    loss = network.LossTracker()
    state = network.WorkerState()
    recovered = threading.Event()
    attempts = []

    def writer(_path, event):
        attempts.append(event)
        if len(attempts) == 1:
            raise network.NetworkAuditError("audit socket unavailable")
        recovered.set()
        return {"ok": True}

    thread = threading.Thread(
        target=network._audit_worker,
        args=(work, loss, "/run/test/audit.sock", writer, 0, state),
        daemon=True,
    )
    thread.start()
    work.put({"event": "network.flow"})
    assert recovered.wait(2)
    assert attempts[1]["event"] == "network.audit_health"
    assert attempts[1]["operation"] == "audit_write"
    assert attempts[1]["decision"] == "error"
    assert loss.snapshot()[1]["audit_unavailable"] == 1
    work.put(network._WORK_SENTINEL)
    thread.join(2)
    assert not thread.is_alive()
    assert state.exited is True


def test_shutdown_budget_counts_queued_flows_and_pending_health(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.reads = 0

        def recv(self, _size) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return packet_message(ipv4(), sequence=1) + packet_message(ipv4(), sequence=2)
            raise OSError("stop test")

        def close(self) -> None:
            pass

    writer_started = threading.Event()
    release_writer = threading.Event()

    def writer(_path, _event):
        writer_started.set()
        release_writer.wait(2)
        return {"ok": True}

    loss = network.LossTracker()
    loss.add("enobufs")
    state = network.WorkerState()
    monkeypatch.setattr(network, "sd_notify", lambda _: True)
    with pytest.raises(OSError, match="stop test"):
        network.serve(
            GROUP, UID, GID, "/run/test/audit.sock",
            opener=lambda _: FakeSocket(), writer=writer,
            health_interval=0, loss=loss, state=state, shutdown_timeout=0.05,
        )
    assert writer_started.is_set()
    assert state.exited is False
    assert loss.snapshot()[1]["shutdown_loss"] == 3
    assert "shutdown loss:" in capsys.readouterr().err
    release_writer.set()
    for _ in range(200):
        if state.exited:
            break
        threading.Event().wait(0.01)
    assert state.exited is True


def test_sigterm_is_graceful_and_handler_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network.os, "geteuid", lambda: 0)
    previous = network.signal.getsignal(network.signal.SIGTERM)

    def interrupt_serve(*_args) -> None:
        network.os.kill(network.os.getpid(), network.signal.SIGTERM)

    monkeypatch.setattr(network, "serve", interrupt_serve)
    assert network.daemon_main(["--uid", str(UID), "--gid", str(GID)]) == 0
    assert network.signal.getsignal(network.signal.SIGTERM) == previous


def test_daemon_requires_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        network.daemon_main(["--uid", str(UID), "--gid", str(GID)])


def test_sd_notify_uses_existing_namespace_independent_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "notify.sock"
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(path))
    sink.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    assert network.sd_notify("READY=1") is True
    assert sink.recv(1024) == b"READY=1"
    sink.close()


def test_real_audit_socket_preserves_kernel_flow_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    journal.bind(str(tmp_path / "journal.sock"))
    journal.settimeout(2)
    store = audit.DedupStore(str(tmp_path / "dedup.json"))
    service = audit.AuditService(store, audit.JournalWriter(str(tmp_path / "journal.sock")))
    group = grp.getgrgid(os.getegid()).gr_name
    server = audit.AuditDaemon(str(tmp_path / "audit.sock"), service, group)
    monkeypatch.setattr(server, "authorized", lambda _: True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event = network.events_from_datagram(packet_message(ipv4()), GROUP, UID, GID)[0]
        reply = network.request(str(tmp_path / "audit.sock"), event)
        assert reply["ok"] is True
        record = dict(
            line.split("=", 1)
            for line in journal.recv(65_535).decode("ascii").splitlines()
        )
        assert record["DIARY_AUDIT_EVENT"] == "network.flow"
        assert record["DIARY_AUDIT_DECISION"] == "would_deny"
        assert record["DIARY_AUDIT_CALLER_UID"] == str(UID)
        assert record["DIARY_AUDIT_CALLER_GID"] == str(GID)
        assert record["DIARY_AUDIT_CALLER_PID"] == "0"
        assert record["DIARY_AUDIT_WRITER_UID"] == str(os.geteuid())
        assert record["DIARY_AUDIT_DESTINATION_IP"] == "198.51.100.20"
        assert record["DIARY_AUDIT_DESTINATION_PORT"] == "443"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        journal.close()
