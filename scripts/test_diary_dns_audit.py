"""Isolated DNS proxy, metadata, and alternate-resolution audit tests."""
from __future__ import annotations

import errno
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from scripts import diary_audit as audit
from scripts import diary_dns_audit as dns
from scripts import diary_dns_policy as policy
from scripts import diary_network_audit as network

UID = 1001
GID = 1002
GROUP = 30


def wire_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\0"


def query(name: str = "Example.COM") -> bytes:
    return struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + wire_name(name) + struct.pack("!HH", 1, 1)


def large_query() -> bytes:
    header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 18)
    question = wire_name("Example.COM") + struct.pack("!HH", 1, 1)
    rdata = bytes([255]) + b"a" * 255 + bytes([243]) + b"b" * 243
    additional = b"\0" + struct.pack("!HHIH", 16, 1, 0, len(rdata)) + rdata
    message = header + question + additional * 18
    assert 8192 < len(message) < 65_535
    return message


def ipv4(message: bytes, protocol: int = network.UDP, port: int = 53) -> bytes:
    transport = struct.pack("!HH", 4242, port)
    if protocol == network.UDP:
        transport += struct.pack("!HH", 8 + len(message), 0) + message
    else:
        transport += bytes(8) + bytes([5 << 4, 0]) + bytes(6) + struct.pack("!H", len(message))
        transport += message
    packet = bytearray(20)
    packet[0] = 0x45
    packet[2:4] = struct.pack("!H", 20 + len(transport))
    packet[8] = 64
    packet[9] = protocol
    packet[12:16] = socket.inet_aton("192.0.2.10")
    packet[16:20] = socket.inet_aton("198.51.100.20")
    return bytes(packet) + transport


def ipv6(message: bytes, protocol: int = network.UDP, port: int = 53, extension: bool = False) -> bytes:
    transport = struct.pack("!HH", 4242, port)
    if protocol == network.UDP:
        transport += struct.pack("!HH", 8 + len(message), 0) + message
    else:
        transport += bytes(8) + bytes([5 << 4, 0]) + bytes(6) + struct.pack("!H", len(message))
        transport += message
    packet = bytearray(40)
    packet[0] = 0x60
    packet[2:4] = struct.pack("!H", len(transport) + (8 if extension else 0))
    packet[6] = 0 if extension else protocol
    packet[7] = 64
    packet[8:24] = socket.inet_pton(socket.AF_INET6, "2001:db8::10")
    packet[24:40] = socket.inet_pton(socket.AF_INET6, "2001:db8::20")
    if extension:
        packet.extend(bytes([protocol, 0]) + bytes(6))
    return bytes(packet) + transport


def packet_message(packet: bytes, prefix: bytes, family: int = socket.AF_INET) -> bytes:
    body = struct.pack("BBH", family, 0, socket.htons(GROUP))
    body += network._attribute(network.NFULA_PREFIX, prefix)
    body += network._attribute(network.NFULA_UID, struct.pack("!I", UID))
    body += network._attribute(network.NFULA_GID, struct.pack("!I", GID))
    body += network._attribute(network.NFULA_SEQ, struct.pack("!I", 7))
    body += network._attribute(network.NFULA_PAYLOAD, packet)
    return network._message(network.NFULNL_MSG_PACKET, body, sequence=7)


def test_query_name_is_bounded_approved_metadata() -> None:
    message = query() + b"private additional payload"
    assert dns.query_name(message, "udp") == "example.com"
    framed = struct.pack("!H", len(message)) + message
    assert dns.query_name(framed, "tcp") == "example.com"


def test_compressed_query_name_and_pointer_loop() -> None:
    pointer = b"\xc0\x0c"
    message = query("www.example.com") + pointer
    assert dns.query_name(message, "udp") == "www.example.com"
    loop = bytearray(query())
    loop[12:14] = b"\xc0\x0c"
    assert dns.query_name(bytes(loop), "udp") is None


@pytest.mark.parametrize(
    "message",
    [
        b"",
        bytes(12),
        query()[:13],
        query()[:-1],
        struct.pack("!HHHHHH", 1, 0x8000, 1, 0, 0, 0) + bytes(20),
        struct.pack("!HHHHHH", 1, 0, 2, 0, 0, 0) + bytes(20),
        struct.pack("!HHHHHH", 1, 0x100, 1, 0, 0, 0) + b"\0" + struct.pack("!HH", 1, 1),
        struct.pack("!HHHHHH", 1, 0x100, 1, 0, 0, 0) + b"\x01\xff\0" + struct.pack("!HH", 1, 1),
    ],
)
def test_invalid_or_unapproved_query_has_no_name(message: bytes) -> None:
    assert dns.query_name(message, "udp") is None


def test_alternate_direct_udp_dns_records_name_without_payload() -> None:
    message = query() + b"sensitive answer data"
    event = network.events_from_datagram(
        packet_message(ipv4(message), b"diary-dns-direct\0"), GROUP, UID, GID,
    )[0]
    assert event == {
        "event": "dns.alternate",
        "component": "nftables",
        "operation": "direct",
        "decision": "would_deny",
        "lock_state": "not_applicable",
        "reason": "audit_only",
        "destination_ip": "198.51.100.20",
        "destination_port": 53,
        "protocol": "udp",
        "caller": {"pid": 0, "uid": UID, "gid": GID},
        "dns_name": "example.com",
    }
    normalized, redacted = audit.normalize_event(event)
    assert normalized == event and redacted == []
    assert "sensitive" not in json.dumps(event)


def test_alternate_direct_tcp_dns_records_name() -> None:
    event = network.events_from_datagram(
        packet_message(ipv4(query(), network.TCP), b"diary-dns-direct\0"), GROUP, UID, GID,
    )[0]
    assert event["protocol"] == "tcp"
    assert event["dns_name"] == "example.com"


@pytest.mark.parametrize(
    ("prefix", "protocol", "port", "operation"),
    [
        (b"diary-dns-dot\0", network.TCP, 853, "dot"),
        (b"diary-dns-doh\0", network.TCP, 443, "doh_path"),
        (b"diary-dns-mdns\0", network.UDP, 5353, "mdns"),
        (b"diary-dns-llmnr\0", network.UDP, 5355, "llmnr"),
    ],
)
def test_alternate_resolution_paths_are_would_block(
    prefix: bytes, protocol: int, port: int, operation: str,
) -> None:
    payload = b"private TLS or HTTP payload" if protocol == network.TCP else query()
    event = network.events_from_datagram(
        packet_message(ipv4(payload, protocol, port), prefix), GROUP, UID, GID,
    )[0]
    assert event["event"] == "dns.alternate"
    assert event["operation"] == operation
    assert event["decision"] == "would_deny"
    assert event["destination_port"] == port
    assert "private" not in json.dumps(event)
    if protocol == network.TCP:
        assert "dns_name" not in event
    else:
        assert event["dns_name"] == "example.com"


@pytest.mark.parametrize(
    ("prefix", "protocol", "port", "operation"),
    [
        (b"diary-dns-direct\0", network.UDP, 53, "direct"),
        (b"diary-dns-dot\0", network.TCP, 853, "dot"),
        (b"diary-dns-doh\0", network.TCP, 443, "doh_path"),
        (b"diary-dns-mdns\0", network.UDP, 5353, "mdns"),
        (b"diary-dns-llmnr\0", network.UDP, 5355, "llmnr"),
    ],
)
def test_ipv6_alternate_resolution_paths_and_plaintext_names(
    prefix: bytes, protocol: int, port: int, operation: str,
) -> None:
    payload = b"private TLS or HTTP payload" if protocol == network.TCP else query()
    event = network.events_from_datagram(
        packet_message(ipv6(payload, protocol, port), prefix, socket.AF_INET6), GROUP, UID, GID,
    )[0]
    assert event["event"] == "dns.alternate"
    assert event["operation"] == operation
    assert event["destination_ip"] == "2001:db8::20"
    assert event["destination_port"] == port
    assert event["caller"] == {"pid": 0, "uid": UID, "gid": GID}
    if protocol == network.TCP:
        assert "dns_name" not in event
    else:
        assert event["dns_name"] == "example.com"


def test_ipv6_direct_dns_name_survives_extension_header() -> None:
    event = network.events_from_datagram(
        packet_message(
            ipv6(query(), extension=True), b"diary-dns-direct\0", socket.AF_INET6,
        ),
        GROUP, UID, GID,
    )[0]
    assert event["destination_ip"] == "2001:db8::20"
    assert event["dns_name"] == "example.com"


def test_truncated_direct_dns_still_has_flow_evidence_but_no_invented_name() -> None:
    event = network.events_from_datagram(
        packet_message(ipv4(query()[:20]), b"diary-dns-direct\0"), GROUP, UID, GID,
    )[0]
    assert event["event"] == "dns.alternate"
    assert "dns_name" not in event


def test_alternate_identity_mismatch_fails_closed() -> None:
    message = bytearray(packet_message(ipv4(query()), b"diary-dns-direct\0"))
    uid_offset = message.find(struct.pack("!I", UID))
    message[uid_offset:uid_offset + 4] = struct.pack("!I", UID + 1)
    with pytest.raises(network.NetworkAuditError, match="identity"):
        network.events_from_datagram(bytes(message), GROUP, UID, GID)


class FakePolicy:
    def __init__(self, reply: policy.PolicyReply) -> None:
        self.reply = reply
        self.calls: list[tuple[str | None, str]] = []

    def authorize(self, name: str | None, protocol: str) -> policy.PolicyReply:
        self.calls.append((name, protocol))
        return self.reply


def dark_policy() -> FakePolicy:
    return FakePolicy(policy.PolicyReply(True, "would_allow", "not_applicable", "audit_only", 0))


def attended_policy() -> FakePolicy:
    return FakePolicy(policy.PolicyReply(True, "allow", "attended", "attended", 0))


def locked_policy() -> FakePolicy:
    return FakePolicy(policy.PolicyReply(False, "deny", "locked", "locked", 5))


def test_refusal_preserves_transaction_id_question_and_wire_framing() -> None:
    message = query()
    response = dns.refusal(message, "udp")
    assert response is not None
    identifier, flags, questions, answers, authority, additional = struct.unpack_from("!HHHHHH", response)
    assert identifier == 0x1234
    assert flags & 0x8000 and flags & 0x000F == 5
    assert (questions, answers, authority, additional) == (1, 0, 0, 0)
    assert response[12:] == message[12:]
    framed = dns.refusal(struct.pack("!H", len(message)) + message, "tcp")
    assert framed is not None
    assert struct.unpack_from("!H", framed)[0] == len(response)
    assert framed[2:] == response


def test_malformed_refusal_is_header_only_and_too_short_has_no_response() -> None:
    message = struct.pack("!HHHHHH", 0xBEEF, 0x0100, 0, 0, 0, 0)
    response = dns.refusal(message, "udp")
    assert response is not None
    assert struct.unpack("!HHHHHH", response) == (0xBEEF, 0x8105, 0, 0, 0, 0)
    assert dns.refusal(b"\x12", "udp") is None


def test_dark_udp_forwards_large_query_unchanged_once() -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.settimeout(2)
    received = []

    def serve_upstream() -> None:
        message, client = upstream.recvfrom(65_535)
        received.append(message)
        upstream.sendto(b"large query response", client)

    thread = threading.Thread(target=serve_upstream)
    thread.start()
    policy_client = dark_policy()
    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", upstream.getsockname()[1], UID, GID,
        policy_client=policy_client,
    )
    proxy.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        message = large_query()
        client.sendto(message, ("127.0.0.1", proxy.port))
        assert client.recv(65_535) == b"large query response"
        assert received == [message]
        assert policy_client.calls == [("example.com", "udp")]
    finally:
        client.close()
        proxy.close()
        upstream.close()
        thread.join(2)


def test_attended_tcp_forwards_after_exact_policy_allowance() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    received = []

    def serve_upstream() -> None:
        connection, _address = listener.accept()
        with connection:
            header = dns._receive_exact(connection, 2)
            assert header is not None
            message = dns._receive_exact(connection, struct.unpack("!H", header)[0])
            received.append(message)
            connection.sendall(struct.pack("!H", 8) + b"response")

    thread = threading.Thread(target=serve_upstream)
    thread.start()
    policy_client = attended_policy()
    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", listener.getsockname()[1], UID, GID,
        policy_client=policy_client,
    )
    proxy.start()
    try:
        message = query()
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as client:
            client.sendall(struct.pack("!H", len(message)) + message)
            assert dns._receive_exact(client, 2) == struct.pack("!H", 8)
            assert dns._receive_exact(client, 8) == b"response"
        assert received == [message]
        assert policy_client.calls == [("example.com", "tcp")]
    finally:
        proxy.close()
        listener.close()
        thread.join(2)


class RecordingUDPServer:
    """Stand-in UDP listener for construction-failure tests: records server_close."""

    def __init__(self, address: tuple[str, int]) -> None:
        self.server_address = address
        self.closed = False

    def server_close(self) -> None:
        self.closed = True


def test_ephemeral_bind_retries_a_tcp_port_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    real_udp = dns._UDPServer
    real_tcp = dns._TCPServer
    tcp_binds: list[tuple[str, int]] = []
    udp_servers: list[dns._UDPServer] = []

    def udp_factory(address: tuple[str, int], handler: type) -> dns._UDPServer:
        server = real_udp(address, handler)
        udp_servers.append(server)
        return server

    def tcp_factory(address: tuple[str, int], handler: type) -> dns._TCPServer:
        tcp_binds.append(address)
        if len(tcp_binds) == 1:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return real_tcp(address, handler)

    monkeypatch.setattr(dns, "_UDPServer", udp_factory)
    monkeypatch.setattr(dns, "_TCPServer", tcp_factory)
    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=locked_policy(),
    )
    try:
        assert len(tcp_binds) >= 2
        assert len(udp_servers) == len(tcp_binds)
        assert udp_servers[0].socket.fileno() == -1
        assert proxy.port == tcp_binds[-1][1] == udp_servers[-1].server_address[1]
    finally:
        proxy.close()


def test_fixed_port_tcp_collision_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    udp_servers: list[RecordingUDPServer] = []

    def udp_factory(address: tuple[str, int], handler: type) -> RecordingUDPServer:
        server = RecordingUDPServer(address)
        udp_servers.append(server)
        return server

    def tcp_factory(address: tuple[str, int], handler: type) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(dns, "_UDPServer", udp_factory)
    monkeypatch.setattr(dns, "_TCPServer", tcp_factory)
    with pytest.raises(OSError) as failure:
        dns.DNSProxy(
            "127.0.0.1", dns.DEFAULT_PORT, "127.0.0.1", 53, UID, GID,
            policy_client=locked_policy(),
        )
    assert failure.value.errno == errno.EADDRINUSE
    assert len(udp_servers) == 1
    assert udp_servers[0].closed


def test_ephemeral_bind_does_not_retry_permanent_tcp_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    udp_servers: list[RecordingUDPServer] = []

    def udp_factory(address: tuple[str, int], handler: type) -> RecordingUDPServer:
        server = RecordingUDPServer(address)
        udp_servers.append(server)
        return server

    def tcp_factory(address: tuple[str, int], handler: type) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(dns, "_UDPServer", udp_factory)
    monkeypatch.setattr(dns, "_TCPServer", tcp_factory)
    with pytest.raises(OSError) as failure:
        dns.DNSProxy(
            "127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=locked_policy(),
        )
    assert failure.value.errno == errno.EACCES
    assert len(udp_servers) == 1
    assert udp_servers[0].closed


def test_ephemeral_bind_gives_up_after_bounded_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns, "EPHEMERAL_BIND_ATTEMPTS", 2)
    udp_servers: list[RecordingUDPServer] = []

    def udp_factory(address: tuple[str, int], handler: type) -> RecordingUDPServer:
        server = RecordingUDPServer(address)
        udp_servers.append(server)
        return server

    def tcp_factory(address: tuple[str, int], handler: type) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(dns, "_UDPServer", udp_factory)
    monkeypatch.setattr(dns, "_TCPServer", tcp_factory)
    with pytest.raises(OSError) as failure:
        dns.DNSProxy(
            "127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=locked_policy(),
        )
    assert failure.value.errno == errno.EADDRINUSE
    assert len(udp_servers) == 2
    assert all(server.closed for server in udp_servers)


def test_armed_udp_and_tcp_denials_never_call_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_upstream(*_args):
        raise AssertionError("denied query reached upstream")

    monkeypatch.setattr(dns, "exchange_udp", no_upstream)
    monkeypatch.setattr(dns, "exchange_tcp", no_upstream)
    policy_client = locked_policy()
    proxy = dns.DNSProxy("127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=policy_client)
    proxy.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        message = query()
        client.sendto(message, ("127.0.0.1", proxy.port))
        response = client.recv(65_535)
        assert struct.unpack_from("!H", response)[0] == 0x1234
        assert struct.unpack_from("!H", response, 2)[0] & 0x000F == 5
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as tcp_client:
            tcp_client.sendall(struct.pack("!H", len(message)) + message)
            header = dns._receive_exact(tcp_client, 2)
            assert header == struct.pack("!H", len(response))
            assert dns._receive_exact(tcp_client, len(response)) == response
        assert policy_client.calls == [("example.com", "udp"), ("example.com", "tcp")]
    finally:
        client.close()
        proxy.close()


def test_real_policy_timeout_becomes_refused_without_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "slow-policy.sock"))
    listener.listen(1)

    def slow_policy() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(2048)
            threading.Event().wait(0.2)
        listener.close()

    thread = threading.Thread(target=slow_policy, daemon=True)
    thread.start()
    monkeypatch.setattr(dns, "exchange_udp", lambda *_: pytest.fail("timeout reached upstream"))
    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", 53, UID, GID,
        policy_client=policy.PolicyClient(str(tmp_path / "slow-policy.sock"), timeout=0.02),
    )
    proxy.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(1)
    try:
        client.sendto(query(), ("127.0.0.1", proxy.port))
        response = client.recv(65_535)
        assert struct.unpack_from("!H", response)[0] == 0x1234
        assert struct.unpack_from("!H", response, 2)[0] & 0x000F == 5
    finally:
        client.close()
        proxy.close()
        thread.join(1)
    assert not thread.is_alive()


def test_malformed_message_is_policy_gated_without_inventing_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns, "exchange_udp", lambda *_: pytest.fail("denial reached upstream"))
    policy_client = locked_policy()
    proxy = dns.DNSProxy("127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=policy_client)
    proxy.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        message = struct.pack("!HHHHHH", 0xBEEF, 0x0100, 0, 0, 0, 0)
        client.sendto(message, ("127.0.0.1", proxy.port))
        response = client.recv(65_535)
        assert struct.unpack("!HHHHHH", response) == (0xBEEF, 0x8105, 0, 0, 0, 0)
        assert policy_client.calls == [(None, "udp")]
    finally:
        client.close()
        proxy.close()


def test_too_short_armed_message_is_dropped_without_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns, "exchange_udp", lambda *_: pytest.fail("short denial reached upstream"))
    policy_client = locked_policy()
    proxy = dns.DNSProxy("127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=policy_client)
    allowed, response = proxy.policy_decision(b"\x12", "udp")
    assert allowed is False and response is None
    assert policy_client.calls == [(None, "udp")]
    proxy.close()


def test_persistent_tcp_shutdown_remains_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class StalledPolicy(FakePolicy):
        def authorize(self, name: str | None, protocol: str) -> policy.PolicyReply:
            started.set()
            release.wait(2)
            return super().authorize(name, protocol)

    monkeypatch.setattr(dns, "SHUTDOWN_TIMEOUT", 0.05)
    policy_client = StalledPolicy(policy.PolicyReply(True, "allow", "attended", "attended", 0))
    proxy = dns.DNSProxy("127.0.0.1", 0, "127.0.0.1", 53, UID, GID, policy_client=policy_client)
    proxy.start()
    client = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    try:
        message = query()
        client.sendall(struct.pack("!H", len(message)) + message)
        assert started.wait(1)
        proxy.close()
        assert proxy.started is False
    finally:
        release.set()
        client.close()
        proxy.close()
    assert proxy.tcp.wait_idle(1)
    assert not proxy.tcp_clients
