"""State-machine, peer-credential, and fail-closed tests for diary-lockd."""
from __future__ import annotations

import grp
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import diary_audit as audit
from scripts import diary_lock as lock


class FakeModes:
    def __init__(self) -> None:
        self.active = {"diary-locked.service"}
        self.calls: list[str] = []
        self.fail = False

    def activate(self, mode: str) -> None:
        self.calls.append(mode)
        if self.fail:
            raise lock.LockError("injected mode failure")
        selected = f"diary-{mode}.service"
        if mode == "locked":
            self.active.discard("diary-attended.service")
            self.active.add(selected)
        else:
            self.active.discard("diary-locked.service")
            self.active.discard("diary-lock-failsafe.service")
            self.active.add(selected)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.pings = 0
        self.fail_emit = False
        self.fail_ping = False

    def emit(self, event: str, operation: str, decision: str, state: str, reason: str, caller=None) -> None:
        if self.fail_emit:
            raise lock.LockError("injected audit failure")
        self.events.append({
            "event": event,
            "operation": operation,
            "decision": decision,
            "state": state,
            "reason": reason,
            "caller": caller,
        })

    def ping(self) -> None:
        if self.fail_ping:
            raise lock.LockError("injected audit health failure")
        self.pings += 1


def peer(**changes) -> dict[str, int]:
    value = {"pid": 1234, "uid": 1000, "gid": 1000}
    value.update(changes)
    return value


def controller(tmp_path: Path, now=None, monotonic=None):
    now = now if now is not None else [1_000_000.0]
    monotonic = monotonic if monotonic is not None else [0.0]
    store = lock.StateStore(str(tmp_path / "state.json"), "boot-test")
    modes = FakeModes()
    events = FakeAudit()
    svc = lock.Controller(
        store,
        modes,
        events,
        attended_seconds=900,
        clock=lambda: now[0],
        monotonic=lambda: monotonic[0],
    )
    return svc, modes, events, now, monotonic


def test_boot_and_controller_restart_always_enter_durable_locked_state(tmp_path: Path) -> None:
    svc, modes, events, _, _ = controller(tmp_path)
    svc.store.save("ATTENDED", 999.0, 1999.0)
    svc.startup()
    assert svc.status()["state"] == "LOCKED"
    assert modes.active == {"diary-locked.service"}
    assert events.events[-1]["reason"] == "controller_restart"
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["state"] == "LOCKED" and saved["expires_at"] is None
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600

    rebooted = lock.Controller(
        lock.StateStore(str(tmp_path / "state.json"), "boot-next"),
        modes,
        events,
        clock=lambda: 2000.0,
    )
    rebooted.startup()
    assert rebooted.status()["state"] == "LOCKED"
    assert json.loads((tmp_path / "state.json").read_text())["boot_id"] == "boot-next"


@pytest.mark.parametrize("kind", ["malformed", "unknown", "symlink", "loose"])
def test_unreadable_or_untrusted_state_fails_locked(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "state.json"
    if kind == "malformed":
        path.write_text("not json")
    elif kind == "unknown":
        path.write_text(json.dumps({"version": 1, "state": []}))
    elif kind == "symlink":
        (tmp_path / "target").write_text("{}")
        path.symlink_to(tmp_path / "target")
    else:
        path.write_text("{}")
        path.chmod(0o644)
    svc, modes, events, _, _ = controller(tmp_path)
    svc.startup()
    assert svc.status()["state"] == "LOCKED"
    assert "diary-attended.service" not in modes.active
    assert events.events[-1]["reason"] == "unreadable_state"


@pytest.mark.parametrize("kind", ["fifo", "directory", "socket", "symlink"])
def test_state_rejects_non_regular_files_without_blocking(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "state.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
    else:
        (tmp_path / "target").write_text("{}")
        path.symlink_to(tmp_path / "target")
    with pytest.raises(lock.LockError):
        lock.StateStore(str(path), "boot-test").load()


def test_state_rejects_foreign_ownership(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}")
    path.chmod(0o600)
    real_fstat = os.fstat

    def foreign_fstat(fd: int):
        info = real_fstat(fd)
        return SimpleNamespace(st_uid=info.st_uid + 1, st_mode=info.st_mode)

    with patch.object(lock.os, "fstat", side_effect=foreign_fstat):
        with pytest.raises(lock.LockError, match="ownership or mode"):
            lock.StateStore(str(path), "boot-test").load()


def test_locking_is_persisted_before_mode_transition(tmp_path: Path) -> None:
    svc, _, _, _, _ = controller(tmp_path)
    observed: list[tuple[str, str]] = []
    original = svc.modes.activate

    def activate(mode: str) -> None:
        observed.append((mode, json.loads((tmp_path / "state.json").read_text())["state"]))
        original(mode)

    svc.modes.activate = activate
    svc.enter_locked("explicit_lock")
    assert observed == [("locked", "LOCKING")]


def test_break_glass_uses_kernel_caller_and_monotonic_expiry(tmp_path: Path) -> None:
    svc, modes, events, now, monotonic = controller(tmp_path)
    svc.startup()
    caller = peer(uid=1000)
    result = svc.break_glass(caller)
    assert result["state"] == "ATTENDED"
    assert result["remaining_seconds"] == 900
    assert result["expires_at"] == now[0] + 900
    assert modes.active == {"diary-attended.service"}
    assert [event["event"] for event in events.events[-2:]] == ["controller.break_glass"] * 2
    assert all(event["caller"] is caller for event in events.events[-2:])

    monotonic[0] += 899
    now[0] -= 100_000
    svc.maintenance()
    assert svc.status()["state"] == "ATTENDED"
    assert svc.status()["remaining_seconds"] == 1
    now[0] += 200_000
    svc.maintenance()
    assert svc.status()["state"] == "ATTENDED"
    monotonic[0] += 1
    svc.maintenance()
    assert svc.status()["state"] == "LOCKED"
    assert events.events[-1]["reason"] == "attendance_expired"


def test_agent_requests_and_spoofed_identity_cannot_change_or_extend_state(tmp_path: Path) -> None:
    svc, _, _, _, _ = controller(tmp_path)
    svc.startup()
    svc.break_glass(peer())
    before = svc.status()
    for value in (
        {"cmd": "break_glass", "uid": 0},
        {"cmd": "status", "asserted_user": "owner"},
        {"cmd": "attend"},
    ):
        with pytest.raises(lock.LockError):
            svc.handle(value, peer(uid=1001), owner_uid=1000)
    with pytest.raises(lock.LockError):
        svc.handle({"cmd": "status"}, peer(uid=1001), owner_uid=1000)
    assert svc.status() == before


def test_invalid_owner_request_does_not_trigger_failsafe(tmp_path: Path) -> None:
    svc, _, _, _, _ = controller(tmp_path)
    svc.startup()
    svc.break_glass(peer())
    before = svc.status()
    with patch.object(svc, "failsafe") as failsafe:
        with pytest.raises(lock.LockError):
            svc.handle({"cmd": "unknown"}, peer(), owner_uid=1000)
    failsafe.assert_not_called()
    assert svc.status() == before


def test_audit_failure_during_unlock_rolls_back_to_locked(tmp_path: Path) -> None:
    svc, modes, events, _, _ = controller(tmp_path)
    svc.startup()
    events.fail_emit = True
    with pytest.raises(lock.LockError):
        svc.break_glass(peer())
    events.fail_emit = False
    svc.failsafe("request_failed")
    assert svc.status()["state"] == "LOCKED"
    assert "diary-attended.service" not in modes.active


def test_mode_verification_failure_never_leaves_attended_state(tmp_path: Path) -> None:
    svc, modes, _, _, _ = controller(tmp_path)
    svc.startup()
    modes.fail = True
    with pytest.raises(lock.LockError):
        svc.break_glass(peer())
    modes.fail = False
    svc.failsafe("request_failed")
    assert svc.status()["state"] == "LOCKED"


def test_audit_health_failure_invokes_failsafe(tmp_path: Path) -> None:
    svc, _, events, _, monotonic = controller(tmp_path)
    svc.startup()
    svc.break_glass(peer())
    events.fail_ping = True
    monotonic[0] += 31
    with pytest.raises(lock.LockError):
        svc.maintenance()
    svc.failsafe("supervision_failed")
    assert svc.status()["state"] == "LOCKED"


def test_failsafe_propagates_write_errors_and_retains_locking(tmp_path: Path) -> None:
    svc, modes, events, _, _ = controller(tmp_path)
    events.fail_emit = True
    with patch.object(svc.store, "save", side_effect=OSError("read-only state")):
        with pytest.raises(lock.LockError, match="could not prove locked"):
            svc.failsafe("state_failed")
    assert svc.status()["state"] == "LOCKING"
    assert modes.active == {"diary-locked.service"}


def test_failsafe_does_not_persist_locked_when_mode_verification_fails(tmp_path: Path) -> None:
    svc, modes, _, _, _ = controller(tmp_path)
    svc.startup()
    modes.fail = True
    with pytest.raises(lock.LockError, match="could not prove locked"):
        svc.failsafe("request_failed")
    assert svc.status()["state"] == "LOCKING"
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "LOCKING"


def test_real_socket_enforces_peer_credentials_without_fail_closed_auth_bypass(tmp_path: Path) -> None:
    svc, _, _, _, _ = controller(tmp_path)
    svc.startup()
    group = grp.getgrgid(os.getegid()).gr_name
    server = lock.LockDaemon(str(tmp_path / "lock.sock"), svc, group, os.geteuid())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert stat.S_IMODE(os.stat(tmp_path / "lock.sock").st_mode) == 0o660
        reply = lock.request(str(tmp_path / "lock.sock"), {"cmd": "break_glass"})
        assert reply["state"] == "ATTENDED"
        before = svc.status()
        server.owner_uid = os.geteuid() + 1
        with pytest.raises(lock.LockError, match="not authorized"):
            lock.request(str(tmp_path / "lock.sock"), {"cmd": "lock", "uid": 0})
        assert svc.status() == before
        server.owner_uid = os.geteuid()
        reply = lock.request(str(tmp_path / "lock.sock"), {"cmd": "lock"})
        assert reply["state"] == "LOCKED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unauthorized_partial_request_is_rejected_before_read_timeout(tmp_path: Path) -> None:
    svc, _, _, _, _ = controller(tmp_path)
    svc.startup()
    svc.break_glass(peer())
    before = svc.status()
    group = grp.getgrgid(os.getegid()).gr_name
    server = lock.LockDaemon(str(tmp_path / "lock.sock"), svc, group, os.geteuid() + 1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(svc, "failsafe") as failsafe:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(str(tmp_path / "lock.sock"))
            client.sendall(b'{"cmd":')
            reply = json.loads(client.makefile("rb").readline(4096))
            client.close()
        assert reply["ok"] is False and "not authorized" in reply["error"]
        failsafe.assert_not_called()
        assert server.fatal_error is None
        assert svc.status() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_peer_credentials_come_from_kernel_socketpair() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        credentials = lock.peer_credentials(left)
        assert credentials == {"pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid()}
    finally:
        left.close()
        right.close()


def test_systemd_mode_activation_uses_fixed_argv_and_verifies_conflicts() -> None:
    active = {"diary-locked.service"}
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert "shell" not in kwargs
        if argv[1] == "start":
            active.clear()
            active.add(argv[2])
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0 if argv[-1] in active else 3)

    modes = lock.SystemdModes(runner=run)
    modes.activate("attended")
    assert active == {"diary-attended.service"}
    assert all(call[0] == "/usr/bin/systemctl" for call in calls)

    def conflicting(argv, **kwargs):
        if argv[1] == "start":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0)

    with pytest.raises(lock.LockError, match="conflicting"):
        lock.SystemdModes(runner=conflicting).activate("locked")


def test_systemd_uncertainty_is_not_treated_as_inactive() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0 if argv[1] == "start" else 4)

    with pytest.raises(lock.LockError, match="cannot verify"):
        lock.SystemdModes(runner=run).activate("locked")


def test_break_glass_audit_event_is_warning_priority(tmp_path: Path) -> None:
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(tmp_path / "journal.sock"))
    sink.settimeout(2)
    store = audit.DedupStore(str(tmp_path / "dedup.json"))
    svc = audit.AuditService(store, audit.JournalWriter(str(tmp_path / "journal.sock")))
    try:
        svc.write({
            "event": "controller.break_glass",
            "component": "diary-lockd",
            "operation": "attended",
            "decision": "allow",
            "lock_state": "attended",
            "reason": "local_owner_override",
        }, peer())
        fields = dict(line.split("=", 1) for line in sink.recv(65535).decode("ascii").splitlines())
        assert fields["PRIORITY"] == "4"
        assert fields["DIARY_AUDIT_EVENT"] == "controller.break_glass"
    finally:
        sink.close()


def test_audit_heartbeat_writes_and_reads_back_unique_emission(tmp_path: Path) -> None:
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(tmp_path / "journal.sock"))
    sink.settimeout(2)
    svc = audit.AuditService(
        audit.DedupStore(str(tmp_path / "dedup.json")),
        audit.JournalWriter(str(tmp_path / "journal.sock")),
    )

    def readback(namespace: str, emission_id: str, since) -> list[dict]:
        fields = dict(line.split("=", 1) for line in sink.recv(65535).decode("ascii").splitlines())
        assert fields["DIARY_AUDIT_EMISSION_ID"] == emission_id
        assert fields["DIARY_AUDIT_EVENT"] == "audit.heartbeat"
        assert fields["DIARY_AUDIT_OPERATION"] == "journal_write"
        return [fields]

    svc.heartbeat_reader = readback
    try:
        reply = svc.handle({"cmd": "ping"}, peer())
        assert reply["ok"] is True and reply["pong"] is True
        assert audit.EMISSION_ID_RE.fullmatch(reply["emission_id"])
    finally:
        sink.close()


def test_audit_heartbeat_fails_when_journal_socket_is_missing(tmp_path: Path) -> None:
    svc = audit.AuditService(
        audit.DedupStore(str(tmp_path / "dedup.json")),
        audit.JournalWriter(str(tmp_path / "missing-journal.sock")),
    )
    with pytest.raises(OSError):
        svc.heartbeat(peer())


def test_audit_heartbeat_fails_when_socket_accepts_without_recording(tmp_path: Path) -> None:
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(tmp_path / "journal.sock"))
    sink.settimeout(2)
    svc = audit.AuditService(
        audit.DedupStore(str(tmp_path / "dedup.json")),
        audit.JournalWriter(str(tmp_path / "journal.sock")),
        heartbeat_reader=lambda *_: [],
    )
    try:
        with patch.object(audit.time, "sleep") as sleep:
            with pytest.raises(audit.AuditError, match="not readable"):
                svc.heartbeat(peer())
        assert sleep.call_count == 9
        assert b"DIARY_AUDIT_EVENT=audit.heartbeat" in sink.recv(65535)
    finally:
        sink.close()


def test_audit_heartbeat_rejects_private_request_fields() -> None:
    svc = object.__new__(audit.AuditService)
    with pytest.raises(audit.AuditError):
        svc.handle({"cmd": "ping", "message": "private"}, peer())


def test_sd_notify_and_expiry_health_use_systemd_watchdog_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "notify.sock"
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(path))
    sink.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    assert lock.sd_notify("WATCHDOG=1") is True
    assert sink.recv(1024) == b"WATCHDOG=1"
    sink.close()


def test_sd_notify_abstract_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"diary-lock-test-{os.getpid()}"
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind("\0" + name)
    sink.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    assert lock.sd_notify("WATCHDOG=1") is True
    assert sink.recv(1024) == b"WATCHDOG=1"
    sink.close()


def test_real_daemon_exits_when_transition_and_first_failsafe_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingModes(FakeModes):
        started = False

        def activate(self, mode: str) -> None:
            if mode == "locked" and not self.started:
                self.started = True
                super().activate(mode)
                return
            if mode == "attended":
                super().activate(mode)
                raise lock.LockError("attended verification failed")
            self.calls.append(mode)
            raise lock.LockError("first failsafe failed")

    modes = FailingModes()
    events = FakeAudit()
    monkeypatch.setattr(lock, "SystemdModes", lambda: modes)
    monkeypatch.setattr(lock, "AuditClient", lambda _: events)
    monkeypatch.setattr(lock.signal, "signal", lambda *_: None)
    notifications: list[str] = []
    monkeypatch.setattr(lock, "sd_notify", lambda message: notifications.append(message) or True)
    real_uid = os.geteuid()
    root_check = True

    def effective_uid() -> int:
        nonlocal root_check
        if root_check:
            root_check = False
            return 0
        return real_uid

    monkeypatch.setattr(lock.os, "geteuid", effective_uid)
    boot_file = tmp_path / "boot-id"
    boot_file.write_text("01234567-0123-0123-0123-012345678901\n")
    listen = tmp_path / "lock.sock"
    owner = lock.pwd.getpwuid(real_uid).pw_name
    group = grp.getgrgid(os.getegid()).gr_name
    client_errors: list[str] = []

    def call_break_glass() -> None:
        for _ in range(100):
            try:
                lock.request(str(listen), {"cmd": "break_glass"}, timeout=0.1)
                return
            except lock.LockError as exc:
                if "failsafe" in str(exc):
                    client_errors.append(str(exc))
                    return
                time.sleep(0.02)

    client = threading.Thread(target=call_break_glass)
    client.start()
    result = lock.daemon_main([
        "--listen", str(listen),
        "--state", str(tmp_path / "state.json"),
        "--boot-id-file", str(boot_file),
        "--owner", owner,
        "--group", group,
    ])
    client.join(timeout=5)
    assert result == 1
    assert client_errors and "could not prove locked" in client_errors[0]
    assert modes.calls == ["locked", "attended", "locked"]
    assert modes.active == {"diary-attended.service"}
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "LOCKING"
    assert notifications.count("READY=1") == 1


def test_client_break_glass_requires_owner_and_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(lock.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000))
    assert lock.main(["break-glass"]) == 2


def test_daemon_refuses_unprivileged_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        lock.daemon_main([])
