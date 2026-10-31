"""Tests for the restricted Signal proxy — validation is the boundary."""
from __future__ import annotations

import base64
import json
import os
import socket
import threading

import pytest

from scripts.signal_notify_proxy import Limiter, handle, read_attachment, sanitize


def _send(**params):
    return {"jsonrpc": "2.0", "id": 1, "method": "send", "params": params}


def _get(attachment_id):
    return {"jsonrpc": "2.0", "id": 1, "method": "getAttachment",
            "params": {"id": attachment_id}}


def test_note_to_self_is_forwarded_with_only_two_fields():
    clean, why = sanitize(_send(noteToSelf=True, message="hi"))
    assert why == ""
    assert clean is not None
    assert clean["params"] == {"noteToSelf": True, "message": "hi"}


def test_recipient_is_rejected_not_silently_dropped():
    clean, why = sanitize(_send(noteToSelf=True, message="hi", recipient="+4790000000"))
    assert clean is None and "recipient" in why


def test_group_send_is_rejected():
    clean, why = sanitize(_send(noteToSelf=True, message="hi", groupId="abc"))
    assert clean is None and "groupId" in why


def test_missing_note_to_self_is_rejected():
    clean, why = sanitize(_send(message="hi"))
    assert clean is None


def test_other_methods_are_rejected():
    for method in ("receive", "listAccounts", "subscribe", "sendReceipt"):
        clean, why = sanitize({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
        assert clean is None, method


def test_empty_and_oversized_messages_rejected():
    assert sanitize(_send(noteToSelf=True, message="   "))[0] is None
    assert sanitize(_send(noteToSelf=True, message="x" * 4001))[0] is None


def test_limiter_bursts_then_blocks():
    lim = Limiter(burst=2, period=60.0)
    assert lim.allow() and lim.allow()
    assert not lim.allow()


def test_attachment_read_rejects_paths_and_symlinks(tmp_path):
    (tmp_path / "blob.csv").write_bytes(b"ok")
    (tmp_path / "outside").write_bytes(b"secret")
    (tmp_path / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "directory").mkdir()
    os.mkfifo(tmp_path / "fifo")
    assert read_attachment(str(tmp_path), "blob.csv") == b"ok"
    for value in ("../outside", "/etc/passwd", "dir/blob", ".", "..", "", "link",
                  "directory", "fifo"):
        with pytest.raises((ValueError, OSError)):
            read_attachment(str(tmp_path), value)


def _bind(path):
    """Bind+listen in the caller's thread so the path exists before clients
    connect — otherwise the test races its own helper threads."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    return srv


def _fake_upstream(srv, received):
    conn, _ = srv.accept()
    buf = b""
    while b"\n" not in buf:
        buf += conn.recv(65536)
    received.append(json.loads(buf.split(b"\n", 1)[0]))
    conn.sendall(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    conn.close()
    srv.close()


def _proxy(srv, upstream, connections=2, attachments=None):
    for _ in range(connections):
        conn, _ = srv.accept()
        handle(conn, upstream, Limiter(), attachments)
    srv.close()


def _ask(path, req):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


def test_end_to_end_forward_and_reject(tmp_path):
    upstream_path = str(tmp_path / "up.sock")
    listen_path = str(tmp_path / "listen.sock")
    received: list[dict] = []

    upstream_srv = _bind(upstream_path)
    proxy_srv = _bind(listen_path)
    threading.Thread(target=_fake_upstream, args=(upstream_srv, received), daemon=True).start()
    threading.Thread(target=_proxy, args=(proxy_srv, upstream_path), daemon=True).start()

    assert "result" in _ask(listen_path, _send(noteToSelf=True, message="hello"))
    assert received[0]["params"] == {"noteToSelf": True, "message": "hello"}

    assert "error" in _ask(listen_path, _send(noteToSelf=True, message="spam",
                                             recipient="+4790000000"))
    assert len(received) == 1


def test_end_to_end_attachment_read_and_reject(tmp_path):
    listen_path = str(tmp_path / "listen.sock")
    (tmp_path / "blob.csv").write_bytes(b"a;b\n1;2\n")
    proxy_srv = _bind(listen_path)
    threading.Thread(target=_proxy,
                     args=(proxy_srv, str(tmp_path / "unused.sock"), 2, str(tmp_path)),
                     daemon=True).start()

    response = _ask(listen_path, _get("blob.csv"))
    assert base64.b64decode(response["result"]["data"]) == b"a;b\n1;2\n"
    assert "error" in _ask(listen_path, _get("../secret"))
