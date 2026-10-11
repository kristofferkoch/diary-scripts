#!/usr/bin/env python3
"""Tests for dsh_web_notify: URL rewriting, journal regex, signal send."""

import json
import socket
import threading

import pytest

from scripts.dsh_web_notify import TOKEN_RE, public_url, signal_note


def test_token_re_finds_login_url():
    line = ('12:00:00 INFO  WebServer - Web UI available at '
            'http://127.0.0.1:8802/?token=abc123-DEF_456 (login)')
    assert TOKEN_RE.findall(line) == ["http://127.0.0.1:8802/?token=abc123-DEF_456"]


def test_token_re_ignores_plain_urls():
    assert TOKEN_RE.findall("see https://server:8444/ for docs") == []


def test_public_url_swaps_authority_keeps_query():
    assert (public_url("http://127.0.0.1:8802/?token=abc", "https://server:8444")
            == "https://server:8444/?token=abc")


def test_public_url_strips_trailing_slash_on_base():
    assert (public_url("http://localhost:8802/?token=t", "https://server:8444/")
            == "https://server:8444/?token=t")


def test_public_url_no_path():
    assert (public_url("http://127.0.0.1:8802?token=t", "https://server:8444")
            == "https://server:8444/?token=t")


def _fake_daemon(tmp_path, replies):
    """Minimal JSON-RPC peer on a unix socket: records requests, replies
    with the queued responses (and one unrelated notification first)."""
    path = str(tmp_path / "sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        data = b""
        while b"\n" not in data:
            data += conn.recv(65536)
        requests.append(json.loads(data))
        conn.sendall(json.dumps({"jsonrpc": "2.0", "method": "receive",
                                 "params": {}}).encode() + b"\n")
        for r in replies:
            conn.sendall(json.dumps(r).encode() + b"\n")
        conn.close()
        srv.close()

    requests: list[dict] = []
    t = threading.Thread(target=serve)
    t.start()
    return path, requests, t


def test_signal_note_sends_note_to_self(tmp_path):
    path, requests, t = _fake_daemon(tmp_path, [
        {"jsonrpc": "2.0", "result": {"timestamp": 1}, "id": 1},
    ])
    result = signal_note("hello", socket_path=path)
    t.join(timeout=5)
    assert result == {"timestamp": 1}
    req = requests[0]
    assert req["method"] == "send"
    assert req["params"] == {"noteToSelf": True, "message": "hello"}


def test_signal_note_raises_on_rpc_error(tmp_path):
    path, _, t = _fake_daemon(tmp_path, [
        {"jsonrpc": "2.0", "error": {"code": -1, "message": "boom"}, "id": 1},
    ])
    with pytest.raises(RuntimeError, match="boom"):
        signal_note("hello", socket_path=path)
    t.join(timeout=5)
