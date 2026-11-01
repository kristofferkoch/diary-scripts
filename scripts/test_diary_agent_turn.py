"""Per-turn scope, cancellation, and no-resume tests for diary-lockd."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from scripts import diary_agent_turn as client
from scripts import diary_lock as lock


class FakeSystemd:
    def __init__(self) -> None:
        self.services = {"dsh-web.service": True, "kimi-web.service": True}
        self.supervisor_pids = {"dsh-web.service": 1099, "kimi-web.service": 2199}
        self.pids = {"dsh-web.service": 1100, "kimi-web.service": 2200}
        self.membership = {"dsh-web.service": {1100}, "kimi-web.service": {2200}}
        self.scopes_by_name: dict[str, set[int]] = {}
        self.inactive_scopes: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.replaced_pids: set[int] = set()

    def identity(self, pid: int, ppid: int = 1) -> lock.ProcessIdentity:
        return lock.ProcessIdentity(pid, ppid, pid * 100, os.geteuid())

    def service_identities(
        self, service: str, wait: bool = False,
    ) -> tuple[lock.ProcessIdentity, lock.ProcessIdentity]:
        if not self.services[service]:
            raise lock.LockError("service is stopped")
        supervisor = self.supervisor_pids[service]
        return self.identity(supervisor), self.identity(self.pids[service], supervisor)

    def runtime_pid(self, service: str) -> int:
        return self.service_identities(service)[1].pid

    def verify_identity(self, identity: lock.ProcessIdentity) -> None:
        if identity.pid in self.replaced_pids:
            raise lock.LockError("PID was replaced")

    def start_scope(self, scope: str, pid: int) -> None:
        self.calls.append(("start_scope", scope))
        for members in self.membership.values():
            members.discard(pid)
        self.membership[scope] = {pid}
        self.scopes_by_name[scope] = {pid}
        self.inactive_scopes.discard(scope)

    def verify_membership(self, unit: str, pid: int) -> None:
        if pid not in self.membership.get(unit, set()):
            raise lock.LockError("wrong scope")

    def attach(self, service: str, pid: int) -> None:
        self.calls.append(("attach", service))
        for members in self.membership.values():
            members.discard(pid)
        self.membership[service] = {pid}

    def all_scopes(self) -> list[str]:
        return sorted(self.scopes_by_name)

    def scopes(self, runtime: str) -> list[str]:
        prefix = f"diary-{runtime}-turn-"
        return sorted(scope for scope in self.scopes_by_name if scope.startswith(prefix))

    def active(self, unit: str) -> bool:
        if unit.endswith(".scope"):
            return unit in self.scopes_by_name and unit not in self.inactive_scopes
        return self.services[unit]

    def stop(self, unit: str, no_block: bool = False) -> None:
        self.calls.append(("stop", unit))
        if unit.endswith(".scope"):
            self.inactive_scopes.add(unit)
            self.membership[unit] = set()
        else:
            self.services[unit] = False
            self.membership[unit] = set()

    def wait_inactive(self, unit: str) -> None:
        if self.active(unit):
            raise lock.LockError("still active")

    def start(self, service: str) -> None:
        self.calls.append(("start", service))
        if self.services[service]:
            raise lock.LockError("old service was not stopped")
        self.services[service] = True
        self.supervisor_pids[service] += 1
        self.pids[service] += 1
        self.membership[service] = {self.pids[service]}


class FakeProcesses:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop_and_kill(self) -> int:
        self.calls.append("stop_and_kill")
        return 0

    def require_empty(self) -> None:
        self.calls.append("require_empty")


def turns(
    fake: FakeSystemd,
    attended: bool = True,
    processes: Any = None,
) -> lock.SystemdTurns:
    return lock.SystemdTurns(
        None, lambda: attended, systemd=fake,
        processes=processes or FakeProcesses(),
    )


def test_turn_moves_real_service_pid_to_scope_and_finishes_without_killing_background() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    started = svc.start("dsh", "session-one")
    scope = started["scope"]
    assert scope.startswith("diary-dsh-turn-") and scope.endswith(".scope")
    fake.verify_membership(scope, 1100)
    assert 1100 not in fake.membership["dsh-web.service"]

    assert svc.start("dsh", "session-one") == started
    with pytest.raises(lock.LockError, match="concurrent"):
        svc.start("dsh", "session-two")

    fake.membership[scope].add(1101)
    fake.scopes_by_name[scope].add(1101)
    assert svc.finish("dsh", "session-one")["scope"] == scope
    fake.verify_membership("dsh-web.service", 1100)
    assert fake.scopes_by_name[scope] == {1100, 1101}


def test_locked_turn_is_denied_and_cancel_stops_services_before_complete_scopes() -> None:
    fake = FakeSystemd()
    svc = turns(fake, attended=False)
    with pytest.raises(lock.LockError, match="not attended"):
        svc.start("kimi", "session-one")

    svc = turns(fake)
    dsh_scope = svc.start("dsh", "session-one")["scope"]
    kimi_scope = svc.start("kimi", "session-one")["scope"]
    for scope, child in ((dsh_scope, 1101), (kimi_scope, 2201)):
        fake.membership[scope].add(child)
        fake.scopes_by_name[scope].add(child)

    assert svc.cancel() == 2
    assert not any(fake.services.values())
    assert fake.inactive_scopes == {dsh_scope, kimi_scope}
    stop_calls = [call for call in fake.calls if call[0] == "stop"]
    assert stop_calls[:2] == [("stop", "dsh-web.service"), ("stop", "kimi-web.service")]
    assert ("stop", dsh_scope) in stop_calls and ("stop", kimi_scope) in stop_calls
    assert svc.current == {}

    svc.attend()
    assert all(fake.services.values())
    assert fake.supervisor_pids == {"dsh-web.service": 1100, "kimi-web.service": 2200}
    assert fake.pids == {"dsh-web.service": 1101, "kimi-web.service": 2201}
    assert svc.current == {}


def test_lock_also_stops_tool_created_scopes_outside_controller_prefix() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    fake.scopes_by_name["tool-created-escape.scope"] = {9999}
    fake.membership["tool-created-escape.scope"] = {9999}
    assert svc.cancel() == 1
    assert "tool-created-escape.scope" in fake.inactive_scopes


def test_cancellation_attempts_every_scope_before_failing_closed() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    first = svc.start("dsh", "session-one")["scope"]
    second = svc.start("kimi", "session-one")["scope"]
    original = fake.stop

    def stop(unit: str, no_block: bool = False) -> None:
        if unit == first:
            raise lock.LockError("injected scope failure")
        original(unit, no_block=no_block)

    cast(Any, fake).stop = stop
    with pytest.raises(lock.LockError, match="cancellation failed"):
        svc.cancel()
    assert fake.inactive_scopes == {second}
    assert not any(fake.services.values())
    assert svc.current


def test_scope_created_during_cancellation_is_found_by_next_rescan() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    first = svc.start("dsh", "session-one")["scope"]
    late = "tool-created-during-lock.scope"
    original = fake.stop
    injected = False

    def stop(unit: str, no_block: bool = False) -> None:
        nonlocal injected
        original(unit, no_block=no_block)
        if unit == first and not injected:
            injected = True
            fake.scopes_by_name[late] = {9999}
            fake.membership[late] = {9999}
            fake.inactive_scopes.discard(late)

    cast(Any, fake).stop = stop
    assert svc.cancel() == 2
    assert injected
    assert late in fake.inactive_scopes
    assert not any(fake.active(scope) for scope in fake.scopes_by_name)


def test_attend_refuses_active_service_and_reused_fresh_pids() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    svc.cancel()
    fake.services["dsh-web.service"] = True
    with pytest.raises(lock.LockError, match="refusing to resume active"):
        svc.attend()

    fake.services["dsh-web.service"] = False
    original = fake.start

    def reuse(service: str) -> None:
        original(service)
        fake.supervisor_pids[service] -= 1
        fake.pids[service] -= 1

    cast(Any, fake).start = reuse
    with pytest.raises(lock.LockError, match="reused a supervisor or runtime PID"):
        svc.attend()


def test_pid_replacement_is_rejected_before_scope_start_and_reattach() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    original = fake.start_scope

    def replace_during_start(scope: str, pid: int) -> None:
        original(scope, pid)
        fake.replaced_pids.add(pid)

    cast(Any, fake).start_scope = replace_during_start
    with pytest.raises(lock.LockError, match="PID was replaced"):
        svc.start("dsh", "session-one")

    fake = FakeSystemd()
    svc = turns(fake)
    svc.start("dsh", "session-one")
    fake.replaced_pids.add(1100)
    with pytest.raises(lock.LockError, match="PID was replaced"):
        svc.finish("dsh", "session-one")
    assert not any(call[0] == "attach" for call in fake.calls)


class FakeTurns:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def cancel(self) -> int:
        self.calls.append("cancel")
        if self.fail:
            raise lock.LockError("injected cancellation failure")
        return 2

    def attend(self) -> None:
        self.calls.append("attend")


def controller(tmp_path: Path, turns: FakeTurns) -> tuple[lock.Controller, list[str]]:
    from scripts.test_diary_lock import FakeAudit, FakeModes

    order: list[str] = []
    modes = FakeModes()
    original = modes.activate

    def activate(mode: str) -> None:
        order.append(mode)
        original(mode)

    cast(Any, modes).activate = activate
    svc = lock.Controller(lock.StateStore(str(tmp_path / "state.json"), "boot-test"), modes, FakeAudit())
    svc.turns = turns
    return svc, order


def test_locking_is_durable_before_cancellation_and_unlock_starts_fresh_services(tmp_path: Path) -> None:
    lifecycle = FakeTurns()
    svc, order = controller(tmp_path, lifecycle)
    conversation = tmp_path / "conversation.json"
    conversation.write_text('{"turn":"working","prompt":"retain me"}\n')
    svc.startup()
    assert svc.status()["state"] == "LOCKED"
    assert lifecycle.calls == ["cancel"]
    assert order == ["locked"]
    assert json.loads(conversation.read_text())["turn"] == "working"

    svc.break_glass({"pid": 1, "uid": 1000, "gid": 1000})
    assert lifecycle.calls == ["cancel", "attend"]
    assert json.loads(conversation.read_text())["turn"] == "working"
    assert not hasattr(svc, "replay")

    lifecycle.calls.clear()
    svc.enter_locked("attendance_expired")
    assert lifecycle.calls == ["cancel"]
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "LOCKED"
    assert json.loads(conversation.read_text())["turn"] == "working"


def test_fresh_turn_preparation_precedes_attended_mode_activation(tmp_path: Path) -> None:
    lifecycle = FakeTurns()
    svc, order = controller(tmp_path, lifecycle)
    original = lifecycle.attend

    def attend() -> None:
        order.append("fresh_processes_verified")
        original()

    cast(Any, lifecycle).attend = attend
    svc.startup()
    svc.break_glass({"pid": 1, "uid": 1000, "gid": 1000})
    assert order == ["locked", "fresh_processes_verified", "attended"]


def test_cancellation_failure_does_not_record_locked_or_start_attended_services(tmp_path: Path) -> None:
    lifecycle = FakeTurns()
    lifecycle.fail = True
    svc, _ = controller(tmp_path, lifecycle)
    with pytest.raises(lock.LockError, match="injected"):
        svc.enter_locked("explicit_lock")
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "LOCKING"

    with pytest.raises(lock.LockError, match="could not prove locked"):
        svc.failsafe("request_failed")
    assert lifecycle.calls == ["cancel", "cancel"]
    assert svc.status()["state"] == "LOCKING"


def test_systemd_user_scope_and_attach_use_fixed_dbus_control_group_operations() -> None:
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin"
        assert "shell" not in kwargs
        if "GetUnit" in argv or "GetUnitByPID" in argv:
            return SimpleNamespace(returncode=0, stdout="o /unit/scope\n")
        if "is-active" in argv:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    systemd = lock.SystemdUser("agent", runner=run)
    systemd.start_scope("diary-kimi-turn-" + "a" * 32 + ".scope", 1234)
    created = calls[0]
    assert created[:3] == ["/usr/bin/busctl", "--user", "--machine=agent@"]
    assert "StartTransientUnit" in created
    properties = created[created.index("4") + 1:-1]
    assert properties == [
        "PIDs", "au", "1", "1234",
        "KillMode", "s", "control-group",
        "SendSIGKILL", "b", "1",
        "TimeoutStopUSec", "t", "10000000",
    ]

    systemd.attach("kimi-web.service", 1234)
    attached = next(call for call in calls if "AttachProcessesToUnit" in call)
    assert attached[-5:] == ["ssau", "kimi-web.service", "", "1", "1234"]


def test_turn_socket_uses_kernel_agent_uid_and_preserves_protocol(tmp_path: Path) -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    failures: list[Exception] = []
    group = lock.grp.getgrgid(os.getegid()).gr_name
    path = tmp_path / "turns.sock"
    server = lock.TurnDaemon(str(path), svc, os.geteuid(), group, failures.append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = lock.request(str(path), {"cmd": "start", "runtime": "dsh", "session": "session-one"})
        scope = started["scope"]
        fake.verify_membership(scope, 1100)
        finished = lock.request(str(path), {"cmd": "finish", "runtime": "dsh", "session": "session-one"})
        assert finished["scope"] == scope
        server.agent_uid = os.geteuid() + 1
        with pytest.raises(lock.LockError, match="not the agent|cannot use controller socket"):
            lock.request(str(path), {"cmd": "start", "runtime": "dsh", "session": "other"})
        assert failures == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_locked_turn_started_request_invokes_failsafe(tmp_path: Path) -> None:
    fake = FakeSystemd()
    svc = turns(fake, attended=False)
    failures: list[Exception] = []
    group = lock.grp.getgrgid(os.getegid()).gr_name
    server = lock.TurnDaemon(str(tmp_path / "turns.sock"), svc, os.geteuid(), group, failures.append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(lock.LockError, match="not attended"):
            lock.request(str(tmp_path / "turns.sock"), {
                "cmd": "start", "runtime": "kimi", "session": "task-turn",
            })
        assert len(failures) == 1 and "not attended" in str(failures[0])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_client_finish_failure_never_requests_model_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "request", lambda *_: (_ for _ in ()).throw(ValueError("locked")))
    with patch.object(client.sys, "stdin", SimpleNamespace(buffer=__import__("io").BytesIO(b'{"session_id":"one"}'))):
        assert client.main(["--runtime", "kimi", "finish"]) == 0
    with patch.object(client.sys, "stdin", SimpleNamespace(buffer=__import__("io").BytesIO(b'{"session_id":"one"}'))):
        assert client.main(["--runtime", "kimi", "start"]) == 2


def test_pidfd_signal_is_withheld_when_start_time_or_parent_changes() -> None:
    expected = lock.ProcessIdentity(1234, 100, 999, os.geteuid())
    replacement = lock.ProcessIdentity(1234, 100, 1000, os.geteuid())
    with (
        patch.object(lock.os, "pidfd_open", return_value=42),
        patch.object(lock, "process_identity", return_value=replacement),
        patch.object(lock.signal, "pidfd_send_signal") as send,
        patch.object(lock.os, "close") as close,
    ):
        lock.signal_process(expected, signal.SIGKILL)
    send.assert_not_called()
    close.assert_called_once_with(42)


def pid_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].strip().split()[0] != "Z"


class OutageSystemd:
    def __getattr__(self, _name: str):
        def unavailable(*_args, **_kwargs):
            raise lock.SystemdUnavailable("injected user-manager outage")
        return unavailable


def test_real_pid_fallback_contains_process_tree_during_user_manager_outage(tmp_path: Path) -> None:
    child_file = tmp_path / "child.pid"
    conversation = tmp_path / "conversation.json"
    conversation.write_text('{"turn":"working"}\n')
    helper = r'''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(300)
'''
    parent = subprocess.Popen([sys.executable, "-c", helper, str(child_file)])
    child_pid = None
    try:
        for _attempt in range(100):
            if child_file.exists():
                break
            if parent.poll() is not None:
                raise AssertionError("synthetic parent exited")
            time.sleep(0.02)
        child_pid = int(child_file.read_text())
        allowed = {parent.pid, child_pid}
        processes = lock.AgentProcesses(
            os.geteuid(), eligible=lambda identity: identity.pid in allowed,
        )
        svc = lock.SystemdTurns(
            None, lambda: True, runtimes={"probe": "diary-outage-probe.service"},
            systemd=OutageSystemd(), processes=processes,
        )
        assert pid_running(parent.pid) and pid_running(child_pid)
        assert svc.cancel() == 0
        parent.wait(timeout=5)
        assert not pid_running(parent.pid)
        assert not pid_running(child_pid)
        assert processes.processes() == []
        assert json.loads(conversation.read_text())["turn"] == "working"
    finally:
        for pid in (parent.pid, child_pid):
            if pid is not None and pid_running(pid):
                os.kill(pid, signal.SIGKILL)
        parent.wait(timeout=5)


@pytest.mark.skipif(os.environ.get("DIARY_TEST_SYSTEMD_USER") != "1", reason="opt-in synthetic user units")
def test_real_control_group_kills_setsid_child_preserves_state_and_does_not_resume() -> None:
    tag = uuid.uuid4().hex
    service = f"diary-turn-probe-{tag}.service"
    state_dir = Path.cwd() / ".pytest-systemd" / tag
    state_dir.mkdir(parents=True)
    conversation = state_dir / "conversation.json"
    spawn = state_dir / "spawn"
    child_file = state_dir / "child.pid"
    helper = r'''
import json, os, pathlib, signal, subprocess, sys, time
conversation = pathlib.Path(sys.argv[1])
spawn = pathlib.Path(sys.argv[2])
child_file = pathlib.Path(sys.argv[3])
conversation.write_text(json.dumps({"pid": os.getpid(), "turn": "working"}) + "\n")
while not spawn.exists():
    time.sleep(0.02)
child = subprocess.Popen([
    sys.executable, "-c",
    "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(300)",
    str(child_file),
], start_new_session=True)
signal.pause()
'''
    command = [
        "/usr/bin/systemd-run", "--user", "--unit", service,
        "--property=KillMode=control-group", "--property=TimeoutStopSec=10",
        "--property=Restart=no", sys.executable, "-m", "scripts.diary_agent_supervisor", "--",
        sys.executable, "-c", helper, str(conversation), str(spawn), str(child_file),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    systemd = lock.SystemdUser(None)
    svc = lock.SystemdTurns(
        None, lambda: True, runtimes={"probe": service}, systemd=systemd,
        processes=FakeProcesses(), contain_all_scopes=False,
    )
    old_pid = None
    old_child = None
    try:
        for _attempt in range(200):
            if conversation.exists():
                break
            time.sleep(0.05)
        old_pid = json.loads(conversation.read_text())["pid"]
        old_supervisor = systemd.main_pid(service)
        scope = svc.start("probe", "session-one")["scope"]
        systemd.verify_membership(scope, old_pid)
        spawn.touch()
        for _attempt in range(200):
            if child_file.exists():
                break
            time.sleep(0.05)
        old_child = int(child_file.read_text())
        assert old_child != old_pid and pid_running(old_child)

        assert svc.cancel() == 1
        for _attempt in range(100):
            if not pid_running(old_pid) and not pid_running(old_child):
                break
            time.sleep(0.05)
        assert not pid_running(old_pid)
        assert not pid_running(old_child)
        assert json.loads(conversation.read_text())["turn"] == "working"

        spawn.unlink()
        previous_child_record = child_file.read_text()
        svc.attend()
        for _attempt in range(200):
            current = json.loads(conversation.read_text())
            if current["pid"] != old_pid:
                break
            time.sleep(0.05)
        assert current["pid"] != old_pid
        assert systemd.main_pid(service) != old_supervisor
        time.sleep(0.2)
        assert child_file.read_text() == previous_child_record
        assert not pid_running(old_child)
        assert current["turn"] == "working"
    finally:
        spawn.unlink(missing_ok=True)
        try:
            svc.cancel()
        except lock.LockError:
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", service], check=False)
        shutil.rmtree(state_dir, ignore_errors=True)
