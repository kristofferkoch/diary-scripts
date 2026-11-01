"""Socket integration tests for the real diary agent-merge front-end."""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from scripts import broker_audit


@pytest.fixture
def frontend(tmp_path: Path) -> tuple[Path, Path]:
    source = os.environ.get("AGENT_MERGE_STDIN")
    if source is None:
        pytest.skip("set AGENT_MERGE_STDIN to test the diary merge front-end")
    audit_wrapper = tmp_path / "audit-helper"
    audit_wrapper.write_text(
        "#!/bin/bash\n"
        f"exec /usr/bin/python3 {shlex.quote(broker_audit.__file__)} "
        '--socket "$AUDIT_TEST_SOCKET" "$@"\n'
    )
    queue_log = tmp_path / "queue.log"
    queue = tmp_path / "merge-queue"
    queue.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$QUEUE_TEST_LOG"\n'
    )
    audit_wrapper.chmod(0o755)
    queue.chmod(0o755)
    text = Path(source).read_text()
    text = text.replace("/usr/local/libexec/diary-broker-audit", str(audit_wrapper))
    text = text.replace("/usr/local/libexec/agent-merge-queue", str(queue))
    script = tmp_path / "agent-merge-stdin"
    script.write_text(text)
    script.chmod(0o755)
    return script, queue_log


def receive_audit(path: Path, events: list[dict]) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.settimeout(5)
    server.bind(str(path))
    server.listen(1)
    try:
        conn, _ = server.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            events.append(json.loads(data))
            conn.sendall(b'{"ok":true}\n')
    except (TimeoutError, OSError):
        pass
    finally:
        server.close()


def invoke(frontend: Path, request: str, tmp_path: Path) -> tuple[int, bytes, list[dict]]:
    audit_path = tmp_path / "audit.sock"
    events: list[dict] = []
    receiver = threading.Thread(target=receive_audit, args=(audit_path, events), daemon=True)
    receiver.start()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        proc = subprocess.Popen(
            ["/bin/bash", str(frontend)],
            stdin=child.fileno(),
            stdout=child.fileno(),
            stderr=child.fileno(),
            env={
                **os.environ,
                "AUDIT_TEST_SOCKET": str(audit_path),
                "QUEUE_TEST_LOG": str(tmp_path / "queue.log"),
            },
        )
        child.close()
        parent.sendall(request.encode("ascii") + b"\n")
        parent.shutdown(socket.SHUT_WR)
        output = b""
        while chunk := parent.recv(65536):
            output += chunk
        result = proc.wait(timeout=10)
    finally:
        parent.close()
        child.close()
    receiver.join(timeout=10)
    assert not receiver.is_alive()
    return result, output, events


def test_option_like_unknown_action_emits_one_event_and_preserves_protocol(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, _ = frontend
    result, output, events = invoke(script, "--help", tmp_path)
    assert result == 2
    assert output == b"RESULT fail 2 bad-repo\n"
    assert len(events) == 1
    event = events[0]["event"]
    assert event["component"] == "agent-merge"
    assert event["operation"] == "unknown"
    assert event["decision"] == "would_deny"
    assert event["reason"] == "unknown_operation"
    assert event["caller"]["uid"] == os.geteuid()


def test_owner_approval_emits_one_allow_and_preserves_protocol(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, queue_log = frontend
    oid = "a" * 40
    result, output, events = invoke(script, f"approve scripts {oid}", tmp_path)
    assert result == 0
    assert output == b"RESULT ok 0\n"
    assert queue_log.read_text() == f"approve --repo scripts --peer-fd 0 {oid}\n"
    assert len(events) == 1
    event = events[0]["event"]
    assert event["operation"] == "approve"
    assert event["decision"] == "allow"
    assert event["reason"] == "owner_approval"
    assert event["caller"]["uid"] == os.geteuid()
