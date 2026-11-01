"""Isolated DNS proxy, metadata, and alternate-resolution audit tests."""
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
from scripts import diary_dns_audit as dns
from scripts import diary_network_audit as network


UID = 1001
GID = 1002
GROUP = 30


def wire_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\0"


def query(name: str = "Example.COM") -> bytes:
    return struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + wire_name(name) + struct.pack("!HH", 1, 1)


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
    event = dns.query_event(message, "udp", UID, GID, "192.0.2.53", 53)
    assert event is not None
    assert event["dns_name"] == "example.com"
    assert event["caller"] == {"pid": 0, "uid": UID, "gid": GID}
    assert "private" not in json.dumps(event)
    normalized, redacted = audit.normalize_event(event)
    assert normalized == event and redacted == []


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


def test_udp_proxy_forwards_unchanged_when_audit_is_unavailable() -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.settimeout(2)
    received = []

    def serve_upstream() -> None:
        message, client = upstream.recvfrom(65_535)
        received.append(message)
        upstream.sendto(b"response bytes", client)

    thread = threading.Thread(target=serve_upstream)
    thread.start()

    def failed_writer(_path, _event):
        raise dns.DNSAuditError("audit unavailable")

    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", upstream.getsockname()[1], UID, GID,
        writer=failed_writer,
    )
    proxy.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2)
    try:
        message = query()
        client.sendto(message, ("127.0.0.1", proxy.port))
        assert client.recv(65_535) == b"response bytes"
        assert received == [message]
        assert proxy.sink.unavailable == 1
    finally:
        client.close()
        proxy.close()
        upstream.close()
        thread.join(2)


def test_tcp_proxy_forwards_and_records_fixed_identity() -> None:
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
    written = []
    delivered = threading.Event()

    def writer(_path, event):
        written.append(event)
        delivered.set()
        return {"ok": True}

    proxy = dns.DNSProxy(
        "127.0.0.1", 0, "127.0.0.1", listener.getsockname()[1], UID, GID, writer=writer,
    )
    proxy.start()
    try:
        message = query()
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as client:
            client.sendall(struct.pack("!H", len(message)) + message)
            header = dns._receive_exact(client, 2)
            assert header == struct.pack("!H", 8)
            assert dns._receive_exact(client, 8) == b"response"
        assert delivered.wait(2)
        assert received == [message]
        assert written[0]["event"] == "dns.query"
        assert written[0]["protocol"] == "tcp"
        assert written[0]["caller"] == {"pid": 0, "uid": UID, "gid": GID}
        assert written[0]["dns_name"] == "example.com"
    finally:
        proxy.close()
        listener.close()
        thread.join(2)


def test_real_audit_socket_preserves_proxy_identity_and_name(
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
        event = dns.query_event(query(), "udp", UID, GID, "192.0.2.53", 53)
        assert event is not None
        reply = dns.request(str(tmp_path / "audit.sock"), event)
        assert reply["ok"] is True
        record = dict(line.split("=", 1) for line in journal.recv(65_535).decode("ascii").splitlines())
        assert record["DIARY_AUDIT_EVENT"] == "dns.query"
        assert record["DIARY_AUDIT_DNS_NAME"] == "example.com"
        assert record["DIARY_AUDIT_CALLER_UID"] == str(UID)
        assert record["DIARY_AUDIT_CALLER_GID"] == str(GID)
        assert record["DIARY_AUDIT_DECISION"] == "would_deny"
        assert "DIARY_AUDIT_QUERY" not in record
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        journal.close()


def test_full_stalled_audit_queue_has_bounded_shutdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    writer_started = threading.Event()
    release_writer = threading.Event()

    def stalled_writer(_path, _event):
        writer_started.set()
        release_writer.wait(2)
        return {"ok": True}

    sink = dns.AuditSink("/run/test/audit.sock", stalled_writer)
    sink.work = dns.queue.Queue(maxsize=1)
    monkeypatch.setattr(dns, "SHUTDOWN_TIMEOUT", 0.05)
    sink.start()
    try:
        sink.observe({"event": "first"})
        assert writer_started.wait(2)
        sink.observe({"event": "second"})
        sink.close()
        assert "shutdown_loss=" in capsys.readouterr().err
    finally:
        release_writer.set()
        sink.thread.join(2)
    assert not sink.thread.is_alive()
