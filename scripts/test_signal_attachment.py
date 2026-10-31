"""Tests for downloading blobs through the restricted Signal proxy."""
from __future__ import annotations

import base64
import json
import socket
import threading

from scripts.signal_attachment import main


def _serve_attachment(srv: socket.socket, payload: bytes) -> None:
    conn, _ = srv.accept()
    request = json.loads(conn.recv(65536).split(b"\n", 1)[0])
    response = {"jsonrpc": "2.0", "id": request["id"],
                "result": {"data": base64.b64encode(payload).decode("ascii")}}
    conn.sendall(json.dumps(response).encode() + b"\n")
    conn.close()
    srv.close()


def test_cli_writes_downloaded_attachment(tmp_path):
    socket_path = str(tmp_path / "proxy.sock")
    output = tmp_path / "export.csv"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    srv.listen(1)
    threading.Thread(target=_serve_attachment, args=(srv, b"a;b\n1;2\n"),
                     daemon=True).start()
    assert main(["blob.csv", "--socket", socket_path, "--output", str(output)]) == 0
    assert output.read_bytes() == b"a;b\n1;2\n"
