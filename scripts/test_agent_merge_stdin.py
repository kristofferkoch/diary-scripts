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


def receive_audit(path: Path, events: list[dict], ok: bool = True) -> None:
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
            reply = b'{"ok":true}\n' if ok else b'{"ok":false}\n'
            conn.sendall(reply)
    except (TimeoutError, OSError):
        pass
    finally:
        server.close()


def receive_controller(path: Path, state: str) -> None:
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
            assert json.loads(data) == {"cmd": "status"}
            conn.sendall(json.dumps({"ok": True, "state": state}).encode() + b"\n")
    except (TimeoutError, OSError):
        pass
    finally:
        server.close()


def invoke(
    frontend: Path,
    request: str,
    tmp_path: Path,
    *,
    lock_state: str | None = None,
    audit_ok: bool = True,
) -> tuple[int, bytes, list[dict]]:
    audit_path = tmp_path / "audit.sock"
    events: list[dict] = []
    receiver = threading.Thread(
        target=receive_audit, args=(audit_path, events, audit_ok), daemon=True,
    )
    receiver.start()
    controller = None
    lock_path = tmp_path / "lock.sock"
    if lock_state is not None and lock_state != "MISSING":
        controller = threading.Thread(
            target=receive_controller, args=(lock_path, lock_state), daemon=True,
        )
        controller.start()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        env = {
            **os.environ,
            "AUDIT_TEST_SOCKET": str(audit_path),
            "QUEUE_TEST_LOG": str(tmp_path / "queue.log"),
        }
        if lock_state is not None:
            env["DIARY_BROKER_ENFORCE_LOCKDOWN"] = "1"
            env["DIARY_LOCK_SOCKET"] = str(lock_path)
        proc = subprocess.Popen(
            ["/bin/bash", str(frontend)],
            stdin=child.fileno(),
            stdout=child.fileno(),
            stderr=child.fileno(),
            env=env,
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
    if controller is not None:
        controller.join(timeout=10)
        assert not controller.is_alive()
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


@pytest.mark.parametrize("repo", ["diary", "scripts"])
def test_locked_publication_is_denied_before_queue(
    frontend: tuple[Path, Path], tmp_path: Path, repo: str,
) -> None:
    script, queue_log = frontend
    oid = "b" * 40
    result, output, events = invoke(
        script, f"{repo} dev/locked-test {oid}", tmp_path, lock_state="LOCKED",
    )
    assert result == 1
    assert output == b"RESULT fail 1 lockdown-denied\n"
    assert not queue_log.exists()
    event = events[0]["event"]
    assert event["operation"] == repo
    assert event["decision"] == "deny"
    assert event["reason"] == "publication_request"
    assert oid not in json.dumps(events)


def test_locked_owner_approval_remains_available(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, queue_log = frontend
    oid = "c" * 40
    result, output, events = invoke(
        script, f"approve scripts {oid}", tmp_path, lock_state="LOCKED",
    )
    assert result == 0
    assert output == b"RESULT ok 0\n"
    assert queue_log.read_text() == f"approve --repo scripts --peer-fd 0 {oid}\n"
    assert events[0]["event"]["decision"] == "allow"
    assert events[0]["event"]["reason"] == "owner_approval"


def test_locked_unknown_operation_is_denied_before_dispatch(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, queue_log = frontend
    result, output, events = invoke(script, "future-operation", tmp_path, lock_state="LOCKED")
    assert result == 1
    assert output == b"RESULT fail 1 lockdown-denied\n"
    assert not queue_log.exists()
    assert events[0]["event"]["operation"] == "unknown"
    assert events[0]["event"]["decision"] == "deny"


@pytest.mark.parametrize("state", ["MISSING", "UNKNOWN"])
def test_merge_controller_uncertainty_denies_without_queueing(
    frontend: tuple[Path, Path], tmp_path: Path, state: str,
) -> None:
    script, queue_log = frontend
    result, output, events = invoke(
        script, f"scripts dev/uncertain {'d' * 40}", tmp_path, lock_state=state,
    )
    assert result == 1
    assert output == b"RESULT fail 1 lockdown-denied\n"
    assert not queue_log.exists()
    assert events[0]["event"]["reason"] == "controller_unavailable"


def test_merge_audit_failure_denies_attended_publication(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, queue_log = frontend
    result, output, events = invoke(
        script,
        f"scripts dev/audit-failure {'e' * 40}",
        tmp_path,
        lock_state="ATTENDED",
        audit_ok=False,
    )
    assert result == 1
    assert output == b"RESULT fail 1 lockdown-denied\n"
    assert not queue_log.exists()
    assert events[0]["event"]["decision"] == "allow"
    assert events[0]["event"]["reason"] == "attended"


def test_attended_publication_runs_only_new_request(
    frontend: tuple[Path, Path], tmp_path: Path,
) -> None:
    script, queue_log = frontend
    result, output, events = invoke(
        script, f"scripts dev/attended {'f' * 40}", tmp_path, lock_state="ATTENDED",
    )
    assert result == 0
    assert output == b"RESULT ok 0\n"
    assert queue_log.read_text() == f"queue --repo scripts dev/attended {'f' * 40}\n"
    assert events[0]["event"]["lock_state"] == "attended"
    assert events[0]["event"]["decision"] == "allow"
