"""Per-turn scope, cancellation, and no-resume tests for diary-lockd."""
from __future__ import annotations

import json
import os
import shutil
import signal
import struct
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
        self.manager_available = True
        self.manager_recoveries = 0
        self.current_manager_pid = 9001
        self.auto_started_pids: list[tuple[int, int]] = []
        self.socket_active = True
        self.socket_unit = "dbus.socket"
        self.broker_unit = "dbus-broker.service"
        self.bus_quiesced = False
        self.reactivated_after_quiesce = False
        self.ensure_calls: list[str] = []
        self.scheduled: dict[str, bool] = {}

    def _require_manager(self) -> None:
        if not self.manager_available:
            raise lock.SystemdUnavailable("manager unavailable")
        if self.bus_quiesced:
            self.bus_quiesced = False
            self.socket_active = True
            self.services["dbus-broker.service"] = True
            self.reactivated_after_quiesce = True

    def manager_pid(self) -> int:
        return self.current_manager_pid

    def ensure_manager(self) -> None:
        if not self.manager_available:
            self.manager_available = True
            self.manager_recoveries += 1
            self.current_manager_pid += 1
            for service in self.services:
                if service == "dbus-broker.service" or self.services[service]:
                    continue
                self.services[service] = True
                self.supervisor_pids[service] = self.supervisor_pids.get(service, 3000) + 1
                self.pids[service] = self.pids.get(service, 3001) + 1
                self.membership[service] = {self.pids[service]}
                self.auto_started_pids.append((self.supervisor_pids[service], self.pids[service]))
        self.ensure_service(self.socket_unit)
        self.ensure_service(self.broker_unit)

    def identity(self, pid: int, ppid: int = 1) -> lock.ProcessIdentity:
        return lock.ProcessIdentity(pid, ppid, pid * 100, os.geteuid())

    def service_identities(
        self, service: str, wait: bool = False,
    ) -> tuple[lock.ProcessIdentity, lock.ProcessIdentity]:
        self._require_manager()
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
        self._require_manager()
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

    def workload_services(self) -> list[str]:
        self._require_manager()
        return sorted(self.services)

    def scheduled_units(self) -> list[str]:
        self._require_manager()
        return sorted(self.scheduled)

    def all_scopes(self) -> list[str]:
        self._require_manager()
        return sorted(self.scopes_by_name)

    def scopes(self, runtime: str) -> list[str]:
        prefix = f"diary-{runtime}-turn-"
        return sorted(scope for scope in self.scopes_by_name if scope.startswith(prefix))

    def active(self, unit: str) -> bool:
        self._require_manager()
        if unit == "dbus.socket":
            return self.socket_active
        if unit.endswith(".scope"):
            return unit in self.scopes_by_name and unit not in self.inactive_scopes
        if unit.endswith((".timer", ".path")):
            return self.scheduled.get(unit, False)
        return self.services.get(unit, False)

    def stop(self, unit: str, no_block: bool = False) -> None:
        self._require_manager()
        self.calls.append(("stop", unit))
        if unit == "dbus.socket":
            self.socket_active = False
        elif unit.endswith(".scope"):
            self.inactive_scopes.add(unit)
            self.membership[unit] = set()
        elif unit.endswith((".timer", ".path")):
            self.scheduled[unit] = False
        else:
            self.services[unit] = False
            self.membership[unit] = set()

    def wait_inactive(self, unit: str) -> None:
        if self.active(unit):
            raise lock.LockError("still active")

    def start(self, service: str) -> None:
        self.calls.append(("start", service))
        if service == "dbus.socket":
            self.socket_active = True
            return
        if self.services.get(service, False):
            raise lock.LockError("old service was not stopped")
        self.services[service] = True
        self.supervisor_pids[service] = self.supervisor_pids.get(service, 3000) + 1
        self.pids[service] = self.pids.get(service, 3001) + 1
        self.membership[service] = {self.pids[service]}

    def ensure_service(self, service: str) -> None:
        self.ensure_calls.append(service)
        if service in {"dbus.socket", "dbus-broker.service"}:
            self.bus_quiesced = False
            self.reactivated_after_quiesce = False
        if not self.active(service):
            self.start(service)

    def quiesce_user_bus(self) -> None:
        self.services["dbus-broker.service"] = False
        self.socket_active = False
        self.bus_quiesced = True


class FakeProcesses:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pin_manager(self, pid: int | None) -> None:
        self.calls.append(f"pin_manager:{pid}")

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
    assert fake.supervisor_pids["dsh-web.service"] == 1100
    assert fake.supervisor_pids["kimi-web.service"] == 2200
    assert fake.pids["dsh-web.service"] == 1101
    assert fake.pids["kimi-web.service"] == 2201
    assert svc.current == {}


def test_lock_also_stops_tool_created_scopes_outside_controller_prefix() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    fake.scopes_by_name["tool-created-escape.scope"] = {9999}
    fake.membership["tool-created-escape.scope"] = {9999}
    assert svc.cancel() == 1
    assert "tool-created-escape.scope" in fake.inactive_scopes


def test_arbitrary_service_token_and_dbus_are_all_contained() -> None:
    fake = FakeSystemd()
    fake.services.update({
        "dsh-web-token.service": True,
        "kimi-web-token.service": True,
        "escape.service": True,
        "dbus-broker.service": True,
    })
    svc = turns(fake)
    svc.cancel()
    assert not fake.services["dsh-web-token.service"]
    assert not fake.services["kimi-web-token.service"]
    assert not fake.services["escape.service"]
    assert not fake.services["dbus-broker.service"]


def test_timer_and_path_activation_cannot_start_workload_after_lock() -> None:
    fake = FakeSystemd()
    fake.scheduled.update({"agent-escape.timer": True, "agent-escape.path": True})
    svc = turns(fake)
    svc.cancel()
    assert not any(fake.scheduled.values())
    stops = [unit for action, unit in fake.calls if action == "stop"]
    assert stops.index("agent-escape.timer") < stops.index("dsh-web.service")


def test_timer_that_fires_during_cancellation_is_caught_by_the_next_pass() -> None:
    fake = FakeSystemd()
    fake.scheduled["agent-arm.timer"] = True
    svc = turns(fake)
    original = fake.stop
    fired = False

    def stop(unit: str, no_block: bool = False) -> None:
        nonlocal fired
        original(unit, no_block=no_block)
        if unit == "agent-arm.timer" and not fired:
            fired = True
            fake.services["escape.service"] = True
            fake.membership["escape.service"] = {4242}

    cast(Any, fake).stop = stop
    svc.cancel()
    assert fired
    assert not fake.services["escape.service"]


def test_scheduled_unit_created_during_cancellation_is_found_by_next_rescan() -> None:
    fake = FakeSystemd()
    fake.scheduled["agent-first.timer"] = True
    svc = turns(fake)
    original = fake.stop
    injected = False

    def stop(unit: str, no_block: bool = False) -> None:
        nonlocal injected
        original(unit, no_block=no_block)
        if unit == "agent-first.timer" and not injected:
            injected = True
            fake.scheduled["agent-late.path"] = True

    cast(Any, fake).stop = stop
    svc.cancel()
    assert injected
    assert not any(fake.scheduled.values())


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


def test_attend_quiesces_stale_service_and_rejects_reused_fresh_pids() -> None:
    fake = FakeSystemd()
    svc = turns(fake)
    svc.cancel()
    fake.services["dsh-web.service"] = True
    svc.attend()
    assert fake.pids["dsh-web.service"] == 1101
    assert fake.supervisor_pids["dsh-web.service"] == 1100

    svc.cancel()
    original = fake.start

    def reuse(service: str) -> None:
        original(service)
        if service in {"dsh-web.service", "kimi-web.service"}:
            fake.supervisor_pids[service] -= 1
            fake.pids[service] -= 1

    cast(Any, fake).start = reuse
    with pytest.raises(lock.LockError, match="reused a supervisor or runtime PID"):
        svc.attend()


def test_socket_is_stopped_after_last_bus_call_and_restored_before_broker() -> None:
    fake = FakeSystemd()
    fake.services["dbus-broker.service"] = True
    svc = turns(fake)
    svc.cancel()
    assert fake.bus_quiesced
    assert not fake.reactivated_after_quiesce
    assert not fake.socket_active
    assert not fake.services["dbus-broker.service"]

    svc.attend()
    assert fake.socket_active
    assert fake.services["dbus-broker.service"]
    assert fake.ensure_calls[-2:] == ["dbus.socket", "dbus-broker.service"]


def test_outage_lock_unlock_quiesces_default_target_and_starts_fresh_processes() -> None:
    fake = FakeSystemd()
    fake.services["dbus-broker.service"] = True
    fake.manager_available = False
    fake.services = {service: False for service in fake.services}
    svc = turns(fake)
    svc.cancel()
    svc.attend()
    assert fake.manager_available
    assert fake.manager_recoveries == 1
    calls = svc.processes.calls
    pin_index = len(calls) - 1 - calls[::-1].index("pin_manager:9002")
    kill_index = len(calls) - 1 - calls[::-1].index("stop_and_kill")
    assert pin_index < kill_index
    assert fake.auto_started_pids == [(1100, 1101), (2200, 2201)]
    assert all(fake.services.values())
    assert fake.services["dbus-broker.service"]
    assert fake.supervisor_pids["dsh-web.service"] == 1101
    assert fake.supervisor_pids["kimi-web.service"] == 2201
    assert fake.pids["dsh-web.service"] == 1102
    assert fake.pids["kimi-web.service"] == 2202
    start_calls = [unit for operation, unit in fake.calls if operation == "start"]
    assert start_calls.index("dbus-broker.service") < start_calls.index("dsh-web.service")
    assert start_calls.index("dbus-broker.service") < start_calls.index("kimi-web.service")
    for service in ("dsh-web.service", "kimi-web.service"):
        current = (fake.supervisor_pids[service], fake.pids[service])
        assert current not in fake.auto_started_pids


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
    enablement = tmp_path / "ui-actions-enabled"
    enablement.write_text("enabled\n", encoding="utf-8")
    enablement.chmod(0o600)
    svc = lock.Controller(
        lock.StateStore(str(tmp_path / "state.json"), "boot-test"), modes, FakeAudit(),
        ui_actions_file=str(enablement),
    )
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


def test_ui_unlock_prepares_turns_once_and_extension_does_not_restart(tmp_path: Path) -> None:
    lifecycle = FakeTurns()
    svc, order = controller(tmp_path, lifecycle)
    peer = {"pid": 1, "uid": 1100, "gid": 1100}
    svc.startup()
    result = svc.handle({"cmd": "unlock"}, peer, owner_uid=1000, ui_uid=1100)
    assert result["state"] == "ATTENDED"
    assert lifecycle.calls == ["cancel", "attend"]
    assert order == ["locked", "attended"]

    lifecycle.calls.clear()
    extended = svc.handle({"cmd": "extend"}, peer, owner_uid=1000, ui_uid=1100)
    assert extended["state"] == "ATTENDED"
    assert lifecycle.calls == []
    assert order == ["locked", "attended", "attended"]


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


class FakePrivateManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, str]] = []

    def call(self, member: str, signature: str, body: bytes, reply: str) -> str | None:
        self.calls.append((member, signature, body, reply))
        return "/unit/scope" if reply == "o" else None


def test_systemd_user_scope_and_attach_use_private_control_group_operations() -> None:
    calls: list[list[str]] = []
    manager = FakePrivateManager()

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin"
        assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
            "unix:path=/run/user/1234/systemd/private"
        )
        assert not any(arg.startswith("--machine=") for arg in argv)
        assert "shell" not in kwargs
        stdout = "/unit/scope\n" if "--property=ControlGroup" in argv else ""
        return SimpleNamespace(returncode=0, stdout=stdout)

    scope = "diary-kimi-turn-" + "a" * 32 + ".scope"
    with (
        patch.object(lock.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1234)),
        patch.object(lock, "process_cgroup", return_value="/unit/scope/child"),
    ):
        systemd = lock.SystemdUser("agent", runner=run, manager=manager)
        assert systemd._private_manager_path() == "/run/user/1234/systemd/private"
        systemd.start_scope(scope, 1234)
        systemd.attach("kimi-web.service", 1234)

    assert [call[0] for call in manager.calls] == ["StartTransientUnit", "AttachProcessesToUnit"]
    created = manager.calls[0]
    assert created[1] == "ssa(sv)a(sa(sv))"
    for expected in (b"PIDs", b"KillMode", b"control-group", b"SendSIGKILL", b"TimeoutStopUSec"):
        assert expected in created[2]
    assert struct.pack("<I", 1234) in created[2]
    assert struct.pack("<Q", 10000000) in created[2]
    assert manager.calls[1][1] == "ssau"
    assert calls == [
        ["/usr/bin/systemctl", "--user", "is-active", "--quiet", scope],
        ["/usr/bin/systemctl", "--user", "show", scope, "--property=ControlGroup", "--value"],
        ["/usr/bin/systemctl", "--user", "show", "kimi-web.service", "--property=ControlGroup", "--value"],
    ]


def test_production_recovery_uses_private_start_and_status_after_bus_quiescence() -> None:
    active = {"dbus.socket": False, "dbus-broker.service": False}
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:2] == ["--user"]:
            assert not any(arg.startswith("--machine=") for arg in argv)
            assert kwargs["env"]["XDG_RUNTIME_DIR"] == "/run/user/1234"
            assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
                "unix:path=/run/user/1234/systemd/private"
            )
            assert "/run/user/1234/bus" not in kwargs["env"].values()
            if "is-active" in argv:
                return SimpleNamespace(returncode=0 if active[argv[-1]] else 3, stdout="")
            if "start" in argv:
                active[argv[-1]] = True
                return SimpleNamespace(returncode=0, stdout="")
            if "list-units" in argv:
                return SimpleNamespace(returncode=0, stdout="init.scope loaded active running Manager\n")
            raise AssertionError(f"unexpected user operation: {argv}")
        return SimpleNamespace(returncode=0, stdout="4321\n")

    identity = lock.ProcessIdentity(4321, 1, 999, 1234)
    with (
        patch.object(lock.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1234)),
        patch.object(lock, "process_identity", return_value=identity),
    ):
        lock.SystemdUser("agent", runner=run, manager=FakePrivateManager()).ensure_manager()

    starts = [call[-1] for call in calls if call[1:2] == ["--user"] and "start" in call]
    assert starts == ["dbus.socket", "dbus-broker.service"]
    assert not any("user@" + "1234.service" in call and "start" in call for call in calls)


def test_systemd_scope_cancellation_excludes_manager_init_scope() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "init.scope loaded active running System and Service Manager\n"
                "tool-created.scope loaded active running Synthetic tool\n"
            ),
        )

    systemd = lock.SystemdUser("agent", runner=run)
    assert systemd.all_scopes() == ["tool-created.scope"]


def test_systemd_workload_listing_includes_dbus_and_arbitrary_services() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "dbus-broker.service loaded active running D-Bus\n"
                "dsh-web-token.service loaded inactive dead Token notifier\n"
                "escape.service loaded active running Adversarial service\n"
            ),
        )

    systemd = lock.SystemdUser("agent", runner=run)
    assert systemd.workload_services() == [
        "dbus-broker.service", "dsh-web-token.service", "escape.service",
    ]


def test_manager_pid_reports_systemctl_failure_as_unavailable() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="")

    systemd = lock.SystemdUser("agent", runner=run)
    with pytest.raises(lock.SystemdUnavailable):
        systemd.manager_pid()


def test_manager_pid_rejects_malformed_output() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-a-pid\n")

    systemd = lock.SystemdUser("agent", runner=run)
    with pytest.raises(lock.LockError):
        systemd.manager_pid()


def test_manager_pid_reports_inactive_manager_as_none() -> None:
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="0\n")

    systemd = lock.SystemdUser("agent", runner=run)
    assert systemd.manager_pid() is None


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


def fake_process(tmp_path: Path, pid: int, cgroup: str) -> None:
    directory = tmp_path / str(pid)
    directory.mkdir()
    fields = ["0"] * 22
    fields[0] = "S"
    fields[1] = "1"
    fields[19] = str(pid * 10)
    (directory / "stat").write_text(f"{pid} (synthetic) {' '.join(fields)}\n")
    (directory / "cgroup").write_text(f"0::{cgroup}\n")


def test_pid_fallback_preserves_only_manager_pid_and_contains_attached_workload(tmp_path: Path) -> None:
    uid = os.geteuid()
    prefix = f"/user.slice/user-{uid}.slice/user@{uid}.service"
    fake_process(tmp_path, 9001, f"{prefix}/init.scope")
    fake_process(tmp_path, 9002, f"{prefix}/app.slice/dsh-web.service")
    fake_process(tmp_path, 9003, f"{prefix}/app.slice/dsh-web-token.service")
    fake_process(tmp_path, 9004, f"{prefix}/app.slice/tool-created.scope")
    fake_process(tmp_path, 9005, f"{prefix}/session.slice/dbus-broker.service")
    fake_process(tmp_path, 9006, f"{prefix}/app.slice/kimi-web.service/tool-child")
    fake_process(tmp_path, 9007, f"{prefix}/app.slice/escape.service")
    fake_process(tmp_path, 9008, f"{prefix}/session.slice/dbus-broker.service/attached-workload")
    fake_process(tmp_path, 9009, f"{prefix}/init.scope/attached-workload")
    fake_process(tmp_path, 9010, f"{prefix}/session.slice/dbus.socket")
    fake_process(tmp_path, 9011, f"{prefix}/custom.slice/escape.service")
    fake_process(tmp_path, 9012, "/unrelated.slice/unknown-unit")
    processes = lock.AgentProcesses(uid, proc_root=str(tmp_path), manager_pid=9001)
    assert not processes.is_workload(9001)
    assert processes.is_workload(9003)
    assert processes.is_workload(9005)
    assert processes.is_workload(9007)
    assert processes.is_workload(9008)
    assert processes.is_workload(9009)
    assert processes.is_workload(9010)
    assert processes.is_workload(9011)
    assert processes.is_workload(9012)
    assert {identity.pid for identity in processes.processes()} == {
        9002, 9003, 9004, 9005, 9006, 9007, 9008, 9009, 9010, 9011, 9012,
    }


def test_replacement_manager_is_repinned_by_starttime_before_quiescence(tmp_path: Path) -> None:
    uid = os.geteuid()
    prefix = f"/user.slice/user-{uid}.slice/user@{uid}.service"
    fake_process(tmp_path, 9001, f"{prefix}/init.scope")
    fake_process(tmp_path, 9002, f"{prefix}/init.scope")
    processes = lock.AgentProcesses(uid, proc_root=str(tmp_path), manager_pid=9001)
    assert not processes.is_workload(9001)
    assert processes.is_workload(9002)

    processes.pin_manager(9002)
    assert processes.manager_identity == lock.ProcessIdentity(9002, 1, 90020, uid)
    assert processes.is_workload(9001)
    assert not processes.is_workload(9002)


def pid_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].strip().split()[0] != "Z"


class OutageSystemd:
    def manager_pid(self) -> None:
        return None

    def quiesce_user_bus(self) -> None:
        return None

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
    escape_service = f"escape-{tag}.service"
    state_dir = Path.cwd() / ".pytest-systemd" / tag
    state_dir.mkdir(parents=True)
    conversation = state_dir / "conversation.json"
    spawn = state_dir / "spawn"
    child_file = state_dir / "child.pid"
    escape_pid_file = state_dir / "escape.pid"
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
    subprocess.run([
        "/usr/bin/systemd-run", "--user", "--unit", escape_service,
        "--property=KillMode=control-group", "--property=Restart=no",
        sys.executable, "-c",
        "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(300)",
        str(escape_pid_file),
    ], check=True, capture_output=True, text=True, timeout=20)
    systemd = lock.SystemdUser(None)
    cast(Any, systemd).workload_services = lambda: [service, escape_service]
    cast(Any, systemd).quiesce_user_bus = lambda: None
    uid = os.geteuid()
    manager_pid = int(subprocess.check_output([
        "/usr/bin/systemctl", "show", f"user@{uid}.service", "--property=MainPID", "--value",
    ], text=True).strip())
    allowed: set[int] = set()
    processes = lock.AgentProcesses(
        uid, eligible=lambda identity: identity.pid in allowed, manager_pid=manager_pid,
    )
    svc = lock.SystemdTurns(
        None, lambda: True, runtimes={"probe": service}, systemd=systemd,
        processes=processes, contain_all_scopes=False,
    )
    old_pid = None
    old_child = None
    manager_processes = lock.AgentProcesses(
        uid, eligible=lambda identity: identity.pid == manager_pid, manager_pid=manager_pid,
    )
    assert manager_processes.processes() == []
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
        for _attempt in range(200):
            if escape_pid_file.exists():
                break
            time.sleep(0.05)
        escape_pid = int(escape_pid_file.read_text())
        assert pid_running(escape_pid)
        dbus_pid = systemd.main_pid("dbus-broker.service")
        assert "/session.slice/dbus-broker.service" in lock.process_cgroup(dbus_pid)
        assert processes.is_workload(dbus_pid)
        dbus_attached = True
        try:
            systemd.attach("dbus-broker.service", escape_pid)
        except lock.SystemdUnavailable:
            dbus_attached = False
        init_attached = True
        try:
            systemd.attach("init.scope", escape_pid)
        except lock.SystemdUnavailable:
            init_attached = False
        if dbus_attached or init_attached:
            assert processes.is_workload(escape_pid)
        print(f"attachment results: dbus={dbus_attached} init={init_attached}")
        allowed.update({old_supervisor, old_pid, old_child, escape_pid})

        assert svc.cancel() == 1
        for _attempt in range(100):
            if not pid_running(old_pid) and not pid_running(old_child):
                break
            time.sleep(0.05)
        assert not pid_running(old_pid)
        assert not pid_running(old_child)
        assert not pid_running(escape_pid)
        assert not systemd.active(escape_service)
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
        assert pid_running(manager_pid)
        assert manager_processes.processes() == []
        assert not systemd.active(escape_service)
        allowed.update({current["pid"], systemd.main_pid(service)})
        time.sleep(0.2)
        assert child_file.read_text() == previous_child_record
        assert not pid_running(old_child)
        assert current["turn"] == "working"
    finally:
        spawn.unlink(missing_ok=True)
        try:
            svc.cancel()
        except lock.LockError:
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", service, escape_service], check=False)
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("DIARY_TEST_SYSTEMD_USER") != "1", reason="opt-in synthetic user units")
def test_real_user_bus_quiesce_stops_synthetic_broker_and_listener() -> None:
    tag = uuid.uuid4().hex
    service = f"diary-bus-probe-{tag}.service"
    state_dir = Path.cwd() / ".pytest-systemd" / tag
    state_dir.mkdir(parents=True)
    socket_path = Path(f"/tmp/diary-bus-{tag}.sock")
    pid_file = state_dir / "broker.pid"
    helper = r'''
import os, pathlib, socket, sys, time
path = sys.argv[1]
try:
    os.unlink(path)
except FileNotFoundError:
    pass
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
listener.listen()
pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(300)
'''
    subprocess.run([
        "/usr/bin/systemd-run", "--user", "--unit", service,
        "--property=KillMode=control-group", "--property=Restart=no",
        sys.executable, "-c", helper, str(socket_path), str(pid_file),
    ], check=True, capture_output=True, text=True, timeout=20)
    systemd = lock.SystemdUser(
        None, broker_unit=service, socket_unit=None, socket_path=str(socket_path),
    )
    try:
        for _attempt in range(200):
            if pid_file.exists() and socket_path.exists():
                break
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        assert pid_running(pid)
        assert str(socket_path) in Path("/proc/net/unix").read_text()
        systemd.quiesce_user_bus()
        assert not pid_running(pid)
        assert str(socket_path) not in Path("/proc/net/unix").read_text()
    finally:
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", service], check=False)
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("DIARY_TEST_SYSTEMD_USER") != "1", reason="opt-in synthetic user units")
def test_real_ordinary_unlock_restores_socket_and_broker_with_live_manager() -> None:
    tag = uuid.uuid4().hex
    socket_unit = f"diary-bus-recovery-{tag}.socket"
    broker_unit = f"diary-bus-recovery-{tag}.service"
    socket_path = Path(f"/tmp/diary-recovery-{tag}.sock")
    state_dir = Path.cwd() / ".pytest-systemd" / tag
    state_dir.mkdir(parents=True)
    unit_file = state_dir / socket_unit
    unit_file.write_text(f"[Socket]\nListenStream={socket_path}\n")
    broker_file = state_dir / broker_unit
    broker_file.write_text("[Service]\nExecStart=/usr/bin/sleep 300\nRestart=no\n")
    subprocess.run(["/usr/bin/systemctl", "--user", "link", str(unit_file), str(broker_file)], check=True)
    subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["/usr/bin/systemctl", "--user", "start", socket_unit], check=True)
    subprocess.run([
        "/usr/bin/systemctl", "--user", "stop", broker_unit, socket_unit,
    ], check=True, capture_output=True, text=True, timeout=20)
    def private_run(argv, **kwargs):
        if argv[1:2] == ["--user"]:
            assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
                f"unix:path=/run/user/{os.geteuid()}/systemd/private"
            )
        check = kwargs.pop("check", False)
        return subprocess.run(argv, check=check, **kwargs)

    systemd = lock.SystemdUser(
        None, runner=private_run, broker_unit=broker_unit, socket_unit=socket_unit,
        socket_path=str(socket_path),
    )
    manager_before = systemd.manager_pid()
    try:
        assert not systemd.active(socket_unit)
        assert not systemd.active(broker_unit)
        systemd.ensure_manager()
        assert systemd.manager_pid() == manager_before
        assert systemd.active(socket_unit)
        assert systemd.active(broker_unit)
        assert str(socket_path) in Path("/proc/net/unix").read_text()
    finally:
        subprocess.run([
            "/usr/bin/systemctl", "--user", "stop", broker_unit, socket_unit,
        ], check=False, capture_output=True)
        subprocess.run(["/usr/bin/systemctl", "--user", "disable", socket_unit, broker_unit], check=False)
        subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=False)
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(state_dir, ignore_errors=True)
