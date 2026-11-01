"""Isolated CTL-05 broker enforcement and failure-mode tests."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from scripts import broker_audit
from scripts import diary_sync_broker as sync
from scripts import sudo_broker as sudo


CALLER = {"pid": 123, "uid": 1001, "gid": 1001}
OWNER_UID = 1000


class FakeController:
    def __init__(self, path: Path, state: str = "LOCKED") -> None:
        self.state = state
        self.requests: list[dict] = []
        self.stopping = False
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(16)
        self.server.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping:
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(1.0)
                try:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data)
                    self.requests.append(request)
                    if request == {"cmd": "status"}:
                        reply = {"ok": True, "state": self.state}
                    elif request == {"cmd": "break_glass"}:
                        self.state = "ATTENDED"
                        reply = {"ok": True, "state": self.state}
                    else:
                        reply = {"ok": False, "error": "bad request"}
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.stopping = True
        self.server.close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


@pytest.fixture
def controller(tmp_path: Path):
    fake = FakeController(tmp_path / "lock.sock")
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []

    def record(path: str, event: dict) -> dict:
        captured.append(dict(event))
        return {"ok": True}

    monkeypatch.setattr(broker_audit, "request", record)
    return captured


def authorize(
    controller: FakeController,
    component: str,
    operation: str,
    *,
    caller: dict[str, int] = CALLER,
) -> tuple[bool, dict]:
    return broker_audit.authorize(
        component,
        operation,
        caller,
        enforce=True,
        socket_path="unused-audit.sock",
        lock_socket=str(controller.server.getsockname()),
        owner_uid=OWNER_UID,
    )


def test_audit_only_never_queries_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(path: str) -> str:
        raise AssertionError("audit-only mode must not query the controller")

    monkeypatch.setattr(broker_audit, "controller_state", unexpected)
    monkeypatch.setattr(broker_audit, "request", lambda path, event: {"ok": True})
    allowed, event = broker_audit.authorize("diary-sync", "sync", CALLER)
    assert allowed is True
    assert event["decision"] == "would_deny"


@pytest.mark.parametrize(
    ("component", "operation", "caller_uid", "expected", "reason"),
    [
        ("diary-sync", "ping", 1001, True, "read_only"),
        ("diary-sync", "status", 1001, True, "read_only"),
        ("diary-sync", "sync", 1001, False, "external_sync"),
        ("diary-sync", "start", 1001, False, "service_start"),
        ("agent-merge", "diary", 1001, False, "publication_request"),
        ("agent-merge", "scripts", 1001, False, "publication_request"),
        ("agent-merge", "approve", 1000, True, "owner_approval"),
        ("agent-merge", "approve", 1001, False, "owner_required"),
        ("sudo-broker", "execute", 1001, False, "privileged_execute"),
        ("sudo-broker", "break_glass", 1001, True, "owner_break_glass"),
        ("future-broker", "anything", 1001, False, "unknown_operation"),
    ],
)
def test_locked_policy_and_audit(
    controller: FakeController,
    events: list[dict],
    component: str,
    operation: str,
    caller_uid: int,
    expected: bool,
    reason: str,
) -> None:
    allowed, event = authorize(
        controller, component, operation, caller={**CALLER, "uid": caller_uid},
    )
    assert allowed is expected
    assert event["decision"] == ("allow" if expected else "deny")
    assert event["lock_state"] == "locked"
    assert event["reason"] == reason
    assert events[-1] == event


@pytest.mark.parametrize("state", ["LOCKING", "LOCKED"])
def test_external_action_denied_during_lock_transition(
    controller: FakeController, events: list[dict], state: str,
) -> None:
    controller.state = state
    allowed, event = authorize(controller, "diary-sync", "sync")
    assert allowed is False
    assert event["decision"] == "deny"
    assert event["lock_state"] == state.lower()


def test_attended_mode_does_not_change_owner_approval_requirement(
    controller: FakeController, events: list[dict],
) -> None:
    controller.state = "ATTENDED"
    allowed, event = authorize(controller, "diary-sync", "sync")
    assert allowed is True
    assert event["lock_state"] == "attended"
    assert event["reason"] == "attended"
    allowed, event = authorize(controller, "agent-merge", "approve")
    assert allowed is False
    assert event["reason"] == "owner_required"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"ok": False},
        {"ok": True},
        {"ok": True, "state": "UNKNOWN"},
        {"ok": True, "state": True},
    ],
)
def test_invalid_controller_replies_fail_closed(
    monkeypatch: pytest.MonkeyPatch, events: list[dict], reply: dict,
) -> None:
    monkeypatch.setattr(broker_audit, "controller_request", lambda path, value: reply)
    allowed, event = broker_audit.authorize(
        "future-broker", "anything", CALLER, enforce=True,
    )
    assert allowed is False
    assert event["decision"] == "deny"
    assert event["lock_state"] == "unknown"
    assert event["reason"] == "controller_unavailable"


@pytest.mark.parametrize("failure", [OSError("unavailable"), ValueError("bad JSON")])
def test_controller_transport_failures_fail_closed(
    monkeypatch: pytest.MonkeyPatch, events: list[dict], failure: Exception,
) -> None:
    def unavailable(path: str, value: dict) -> dict:
        raise failure

    monkeypatch.setattr(broker_audit, "controller_request", unavailable)
    allowed, event = broker_audit.authorize(
        "diary-sync", "status", CALLER, enforce=True,
    )
    assert allowed is False
    assert event["reason"] == "controller_unavailable"


@pytest.mark.parametrize(
    ("state", "component", "operation"),
    [
        ("LOCKED", "diary-sync", "status"),
        ("LOCKED", "sudo-broker", "break_glass"),
        ("ATTENDED", "diary-sync", "sync"),
    ],
)
def test_audit_failure_denies_once_without_retry(
    controller: FakeController,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    component: str,
    operation: str,
) -> None:
    controller.state = state
    calls: list[dict] = []

    def unavailable(path: str, event: dict) -> dict:
        calls.append(event)
        raise OSError("audit unavailable")

    monkeypatch.setattr(broker_audit, "request", unavailable)
    allowed, event = authorize(controller, component, operation)
    assert allowed is False
    assert event["decision"] == "deny"
    assert event["reason"] == "audit_unavailable"
    assert len(calls) == 1
    assert controller.requests == [{"cmd": "status"}]


def round_trip(handler, request: dict) -> list[dict]:
    broker, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.sendall(json.dumps(request).encode() + b"\n")
        handler(broker)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
        return [json.loads(line) for line in data.splitlines()]
    finally:
        broker.close()
        client.close()


def start_sync(
    path: Path,
    controller_path: str,
    runner,
) -> threading.Thread:
    thread = threading.Thread(
        target=sync.serve,
        args=(str(path),),
        kwargs={
            "runner": runner,
            "enforce_lockdown": True,
            "lock_socket": controller_path,
            "audit_socket": "unused-audit.sock",
        },
        daemon=True,
    )
    thread.start()
    for _ in range(100):
        try:
            sync.call({"cmd": "ping"}, str(path), timeout=1)
            return thread
        except sync.BrokerUnavailable:
            threading.Event().wait(0.02)
    pytest.fail("sync broker did not start")


def test_sync_locked_exceptions_denials_and_no_replay(
    controller: FakeController, events: list[dict], tmp_path: Path,
) -> None:
    external: list[list[str]] = []

    def runner(argv, timeout):
        if "start" in argv or (len(argv) > 2 and argv[2].endswith("-sync")):
            external.append(list(argv))
        return {"ok": True, "output": "active\n", "exit_code": 0}

    path = tmp_path / "sync.sock"
    start_sync(path, str(controller.server.getsockname()), runner)
    assert sync.call({"cmd": "status"}, str(path), timeout=2)["ok"] is True
    for request in (
        {"cmd": "sync", "target": "spond"},
        {"cmd": "start", "unit": "mail-sync.service"},
        {"cmd": "future"},
    ):
        reply = sync.call(request, str(path), timeout=2)
        assert reply["ok"] is False
        assert "locked-mode" in reply["error"]
    assert external == []

    controller.state = "ATTENDED"
    threading.Event().wait(0.1)
    assert external == [], "unlock replayed a rejected operation"
    assert sync.call({"cmd": "sync", "target": "spond"}, str(path), timeout=2)["ok"] is True
    assert len(external) == 1
    assert [event["operation"] for event in events] == [
        "ping", "status", "sync", "start", "unknown", "sync",
    ]


def test_sync_controller_uncertainty_denies_read_only_status(
    events: list[dict], tmp_path: Path,
) -> None:
    ran: list[list[str]] = []

    def runner(argv, timeout):
        ran.append(list(argv))
        return {"ok": True, "output": "active\n"}

    path = tmp_path / "sync.sock"
    start_sync(path, str(tmp_path / "missing-lock.sock"), runner)
    reply = sync.call({"cmd": "status"}, str(path), timeout=2)
    assert reply["ok"] is False
    assert ran == []
    assert events[-1]["reason"] == "controller_unavailable"


def test_sudo_locked_denies_before_prompt_or_execution(
    controller: FakeController,
    events: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("locked sudo must not prompt or execute")

    monkeypatch.setattr(sudo, "approve", unexpected)
    monkeypatch.setattr(sudo, "execute", unexpected)
    replies = round_trip(
        lambda conn: sudo.handle(
            conn,
            enforce_lockdown=True,
            lock_socket=str(controller.server.getsockname()),
            audit_socket="unused-audit.sock",
        ),
        {"cmd": "touch /private/must-not-run", "cwd": "/", "as": "root"},
    )
    assert replies == [
        {"t": "state", "s": "denied", "msg": "denied by locked-mode broker policy"},
    ]
    assert events[-1]["operation"] == "execute"
    assert events[-1]["decision"] == "deny"
    assert "must-not-run" not in json.dumps(events)


def test_sudo_break_glass_requires_confirmation_and_calls_only_controller(
    controller: FakeController,
    events: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals: list[dict] = []

    def approve(*args, **kwargs):
        approvals.append(kwargs)
        return True, ""

    def unexpected(*args, **kwargs):
        raise AssertionError("break-glass must not execute a shell command")

    monkeypatch.setattr(sudo, "approve", approve)
    monkeypatch.setattr(sudo, "execute", unexpected)
    replies = round_trip(
        lambda conn: sudo.handle(
            conn,
            enforce_lockdown=True,
            lock_socket=str(controller.server.getsockname()),
            audit_socket="unused-audit.sock",
        ),
        {"cmd": "", "cwd": "/", "client_pid": 123, "break_glass": True},
    )
    assert replies == [
        {"t": "state", "s": "approved", "as": "root"},
        {"t": "exit", "code": 0},
    ]
    assert approvals == [{"require_confirmation": True}]
    assert controller.requests == [{"cmd": "status"}, {"cmd": "break_glass"}]
    assert events[-1]["operation"] == "break_glass"
    assert events[-1]["decision"] == "allow"


def test_sudo_audit_failure_denies_break_glass_before_prompt(
    controller: FakeController, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(path: str, event: dict) -> dict:
        raise OSError("audit unavailable")

    def unexpected(*args, **kwargs):
        raise AssertionError("unaudited break-glass must not prompt or execute")

    monkeypatch.setattr(broker_audit, "request", unavailable)
    monkeypatch.setattr(sudo, "approve", unexpected)
    replies = round_trip(
        lambda conn: sudo.handle(
            conn,
            enforce_lockdown=True,
            lock_socket=str(controller.server.getsockname()),
            audit_socket="unused-audit.sock",
        ),
        {"cmd": "", "cwd": "/", "client_pid": 123, "break_glass": True},
    )
    assert replies[0]["s"] == "denied"
    assert controller.requests == [{"cmd": "status"}]
