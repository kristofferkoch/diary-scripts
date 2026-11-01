"""Isolated nftables cgroup identity and controller fail-closed tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import diary_lock as lock


class IdentityCommands:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cgroups = {
            "agent.slice": "/agent.slice",
            "dsh-web.service": "/agent.slice/dsh-web.service",
            "kimi-web.service": "/agent.slice/kimi-web.service",
            "dsh-turn-test.scope": "/agent.slice/dsh-turn-test.scope",
        }
        self.active = {unit: "active" for unit in self.cgroups}
        self.scopes = [
            "dsh-turn-test.scope LOAD ACTIVE SUB running",
            "session-1.scope LOAD ACTIVE SUB running",
        ]
        self.members: dict[tuple[str, str, str], set[int]] = {}
        self.set_type = "cgroupv2"
        self.calls: list[list[str]] = []
        for control_group in self.cgroups.values():
            path = root / control_group.removeprefix("/")
            path.mkdir(parents=True)
        self.slice_id = (root / "agent.slice").stat().st_ino

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(argv)
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert "shell" not in kwargs
        if argv[:3] == ["/usr/bin/systemctl", "show", argv[2]]:
            unit = argv[2]
            if unit not in self.cgroups:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(
                returncode=0,
                stdout=f"ActiveState={self.active[unit]}\nControlGroup={self.cgroups[unit]}\n",
            )
        if argv[:2] == ["/usr/bin/systemctl", "list-units"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.scopes) + "\n")
        if argv[:4] == ["/usr/bin/nft", "--json", "list", "set"]:
            selector = (argv[4], argv[5], argv[6])
            if selector not in self.members:
                return SimpleNamespace(returncode=1, stdout="")
            document = {"nftables": [{"set": {
                "family": selector[0],
                "table": selector[1],
                "name": selector[2],
                "type": self.set_type,
                "elem": sorted(self.members[selector]),
            }}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(document))
        raise AssertionError(f"unexpected command: {argv}")


def identity(root: Path, sets=lock.DEFAULT_CGROUP_SETS):
    commands = IdentityCommands(root)
    for selector in sets:
        commands.members[selector] = {commands.slice_id}
    return lock.NftCgroupIdentity(sets=sets, cgroup_root=str(root), runner=commands), commands


def test_slice_and_services_use_verified_nftables_cgroup_identity(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    checker.verify()
    checker.verify_scope("dsh-turn-test.scope")
    assert all(call[0] in {"/usr/bin/systemctl", "/usr/bin/nft"} for call in commands.calls)


def test_every_required_policy_set_must_contain_the_slice(tmp_path: Path) -> None:
    policy = ("inet", "diary_agent_policy", "agent_egress")
    checker, commands = identity(tmp_path, lock.DEFAULT_CGROUP_SETS + (policy,))
    checker.verify()
    commands.members[policy].clear()
    with pytest.raises(lock.LockError, match="missing from inet:diary_agent_policy:agent_egress"):
        checker.verify()


def test_missing_slice_membership_fails_closed(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    commands.members[lock.DEFAULT_CGROUP_SETS[0]] = {commands.slice_id + 1}
    with pytest.raises(lock.LockError, match="missing from"):
        checker.verify()


def test_non_cgroup_set_is_not_accepted(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    commands.set_type = "ipv4_addr"
    with pytest.raises(lock.LockError, match="not a cgroupv2 set"):
        checker.verify()


@pytest.mark.parametrize("unit", ["dsh-web.service", "kimi-web.service"])
def test_inactive_or_escaped_web_service_fails_closed(tmp_path: Path, unit: str) -> None:
    checker, commands = identity(tmp_path)
    commands.active[unit] = "inactive"
    with pytest.raises(lock.LockError, match="not active"):
        checker.verify()
    commands.active[unit] = "active"
    commands.cgroups[unit] = "/system.slice/" + unit
    with pytest.raises(lock.LockError, match="escaped agent.slice"):
        checker.verify()


def test_turn_scope_cannot_escape_agent_slice(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    commands.cgroups["dsh-turn-test.scope"] = "/system.slice/dsh-turn-test.scope"
    with pytest.raises(lock.LockError, match="escaped agent.slice"):
        checker.verify_scope("dsh-turn-test.scope")
    with pytest.raises(lock.LockError, match="invalid agent turn scope"):
        checker.verify_scope("other.scope")


def test_supervision_discovers_scope_escape_after_successful_transition(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    checker.verify()
    commands.cgroups["dsh-turn-test.scope"] = "/system.slice/dsh-turn-test.scope"
    with pytest.raises(lock.LockError, match="escaped agent.slice"):
        checker.verify()


def test_scope_that_exits_after_listing_does_not_cause_false_failure(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    commands.active["dsh-turn-test.scope"] = "inactive"
    commands.cgroups["dsh-turn-test.scope"] = ""
    checker.verify()
    with pytest.raises(lock.LockError, match="not active"):
        checker.verify_scope("dsh-turn-test.scope")


@pytest.mark.parametrize("unit", ["dsh-web.service", "dsh-turn-test.scope"])
@pytest.mark.parametrize("control_group", [
    "/agent.slice/../system.slice/escaped",
    "/agent.slice//nested",
    "/agent.slice/./nested",
    "agent.slice/relative",
])
def test_descendant_control_group_must_be_canonical(
    tmp_path: Path, unit: str, control_group: str,
) -> None:
    checker, commands = identity(tmp_path)
    commands.cgroups[unit] = control_group
    with pytest.raises(lock.LockError, match="unsafe cgroup path"):
        checker.verify()


def test_descendant_control_group_must_exist(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    (tmp_path / "agent.slice/dsh-web.service").rmdir()
    with pytest.raises(lock.LockError, match="cannot stat cgroup"):
        checker.verify()


def test_missing_or_malformed_kernel_state_is_uncertainty(tmp_path: Path) -> None:
    checker, commands = identity(tmp_path)
    (tmp_path / "agent.slice").rename(tmp_path / "missing.slice")
    with pytest.raises(lock.LockError, match="cannot stat cgroup"):
        checker.verify()


def test_controller_does_not_declare_mode_when_identity_is_missing(tmp_path: Path) -> None:
    class Modes:
        def activate(self, mode: str) -> None:
            pass

    class Audit:
        def emit(
            self, event: str, operation: str, decision: str, state: str,
            reason: str, caller: dict[str, int] | None = None,
        ) -> None:
            pass

        def ping(self) -> None:
            pass

    class MissingIdentity:
        def verify(self) -> None:
            raise lock.LockError("missing cgroup membership")

        def verify_scope(self, unit: str) -> None:
            raise lock.LockError("missing cgroup membership")

    controller = lock.Controller(
        lock.StateStore(str(tmp_path / "state.json"), "boot-test"),
        Modes(), Audit(), identity=MissingIdentity(),
    )
    with pytest.raises(lock.LockError, match="missing cgroup"):
        controller.startup()
    assert controller.status()["state"] == "LOCKING"
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "LOCKING"
    with pytest.raises(lock.LockError, match="could not prove locked"):
        controller.failsafe("identity_test")
    assert controller.status()["state"] == "LOCKING"


@pytest.mark.parametrize("attended", [False, True])
def test_maintenance_rechecks_identity_after_transition(
    tmp_path: Path, attended: bool,
) -> None:
    class Modes:
        def activate(self, mode: str) -> None:
            pass

    class Audit:
        def emit(
            self, event: str, operation: str, decision: str, state: str,
            reason: str, caller: dict[str, int] | None = None,
        ) -> None:
            pass

        def ping(self) -> None:
            pass

    class SupervisedIdentity:
        def __init__(self) -> None:
            self.error: str | None = None
            self.calls = 0

        def verify(self) -> None:
            self.calls += 1
            if self.error is not None:
                raise lock.LockError(self.error)

        def verify_scope(self, unit: str) -> None:
            self.verify()

    identity = SupervisedIdentity()
    controller = lock.Controller(
        lock.StateStore(str(tmp_path / "state.json"), "boot-test"),
        Modes(), Audit(), identity=identity,
    )
    controller.startup()
    if attended:
        controller.break_glass({"pid": 1, "uid": 1000, "gid": 1000})
    assert controller.status()["state"] == ("ATTENDED" if attended else "LOCKED")
    identity.error = "post-transition membership lost"
    with pytest.raises(lock.LockError, match="post-transition membership lost"):
        controller.maintenance()
    assert identity.calls >= 2
    identity.error = None
    controller.failsafe("supervision_failed")
    assert controller.status()["state"] == "LOCKED"
