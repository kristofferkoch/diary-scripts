"""Representative broker calls emit AUD-04 events without behavior changes."""
from __future__ import annotations

import base64
import json
import os
import socket
import threading

import pytest

from scripts import broker_audit
from scripts import diary_sync_broker as sync
from scripts import signal_notify_proxy as signal
from scripts import sudo_broker as sudo


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []

    def record(path: str, event: dict) -> dict:
        captured.append(event)
        return {"ok": True}

    monkeypatch.setattr(broker_audit, "request", record)
    return captured


def round_trip(handler, request: dict) -> dict:
    broker, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.sendall(json.dumps(request).encode() + b"\n")
        handler(broker)
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(65536)
        return json.loads(data)
    finally:
        broker.close()
        client.close()


def test_signal_send_is_would_deny_but_still_forwards(
    events: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded = []
    monkeypatch.setattr(
        signal, "forward_ok", lambda upstream, request: forwarded.append(request) or (True, ""),
    )
    reply = round_trip(
        lambda conn: signal.handle(conn, "unused", signal.Limiter()),
        {"jsonrpc": "2.0", "id": 1, "method": "send", "params": {
            "noteToSelf": True, "message": "private body",
        }},
    )
    assert reply["result"] == {} and len(forwarded) == 1
    assert events[0]["operation"] == "send"
    assert events[0]["decision"] == "would_deny"
    assert events[0]["caller"]["uid"] == os.geteuid()
    assert "private body" not in json.dumps(events)


def test_signal_attachment_read_is_allowed_and_unchanged(
    events: list[dict], tmp_path,
) -> None:
    (tmp_path / "blob").write_bytes(b"contents")
    reply = round_trip(
        lambda conn: signal.handle(conn, "unused", signal.Limiter(), str(tmp_path)),
        {"jsonrpc": "2.0", "id": 2, "method": "getAttachment", "params": {"id": "blob"}},
    )
    assert base64.b64decode(reply["result"]["data"]) == b"contents"
    assert events[0]["operation"] == "getAttachment"
    assert events[0]["decision"] == "allow"


def test_signal_unknown_method_is_would_deny_and_still_rejected(events: list[dict]) -> None:
    reply = round_trip(
        lambda conn: signal.handle(conn, "unused", signal.Limiter()),
        {"jsonrpc": "2.0", "id": 3, "method": "receive", "params": {}},
    )
    assert "error" in reply
    assert events[0]["operation"] == "unknown"
    assert events[0]["decision"] == "would_deny"


def test_sync_socket_allow_would_deny_unknown_and_no_enforcement(
    events: list[dict], tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path = str(tmp_path / "sync.sock")
    ran = []

    def runner(argv, timeout):
        ran.append(list(argv))
        return {"ok": True, "output": "active\n", "exit_code": 0}

    thread = threading.Thread(target=sync.serve, args=(path,), kwargs={"runner": runner}, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            assert sync.call({"cmd": "ping"}, path, timeout=1)["ok"] is True
            break
        except sync.BrokerUnavailable:
            threading.Event().wait(0.05)
    else:
        pytest.fail("sync broker did not start")
    assert sync.call({"cmd": "status"}, path, timeout=5)["ok"] is True
    assert sync.call({"cmd": "sync", "target": "spond"}, path, timeout=5)["ok"] is True
    assert sync.call({"cmd": "future"}, path, timeout=5)["ok"] is False
    assert [event["decision"] for event in events] == [
        "allow", "allow", "would_deny", "would_deny",
    ]
    assert [event["operation"] for event in events] == ["ping", "status", "sync", "unknown"]
    assert any(argv[2] == "spond-sync" for argv in ran)
    assert all(event["caller"]["uid"] == os.geteuid() for event in events)


def test_sudo_is_would_deny_but_operator_flow_is_unchanged(
    events: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sudo, "approve", lambda *args, **kwargs: (False, "test denial"))
    reply = round_trip(
        sudo.handle,
        {"cmd": "touch /private/command-must-not-be-logged", "cwd": "/", "as": "root"},
    )
    assert reply == {"t": "state", "s": "denied", "msg": "test denial"}
    assert events[0]["operation"] == "execute"
    assert events[0]["decision"] == "would_deny"
    assert events[0]["caller"]["uid"] == os.geteuid()
    assert "command-must-not-be-logged" not in json.dumps(events)
