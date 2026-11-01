"""Isolated rule, read-back, and fail-closed tests for agent network policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import diary_network_policy as policy


UID = 1001


def actual_expression(expression: Any) -> Any:
    if expression is policy.ANY:
        return {"packets": 3, "bytes": 180}
    if isinstance(expression, dict):
        return {key: actual_expression(value) for key, value in expression.items()}
    if isinstance(expression, list):
        return [actual_expression(value) for value in expression]
    return expression


def document(mode: str, uid: int = UID) -> dict[str, Any]:
    chain, rules = policy.policy_spec(mode, uid)
    objects: list[dict[str, Any]] = [
        {"metainfo": {"version": "1.1.6", "release_name": "test", "json_schema_version": 1}},
        {"table": {"family": "inet", "name": policy.TABLE, "handle": 10}},
        {"chain": {**chain, "handle": 11}},
    ]
    for index, expressions in enumerate(rules, start=20):
        objects.append({
            "rule": {
                "family": "inet",
                "table": policy.TABLE,
                "chain": "output",
                "handle": index,
                "expr": [actual_expression(expression) for expression in expressions],
            }
        })
    return {"nftables": objects}


class FakeKernel:
    def __init__(self, tmp_path: Path, armed: bool = True) -> None:
        self.arm = tmp_path / "armed"
        if armed:
            self.arm.write_text("")
        self.mode = "dark"
        self.calls: list[list[str]] = []
        self.stops: list[str] = []
        self.fail_next_apply = False
        self.stale_mode: str | None = None
        self.legacy_present = False
        self.stop_fails = False

    def run(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(argv)
        if argv[0] == "/usr/bin/systemctl":
            self.stops.append(argv[-1])
            return SimpleNamespace(returncode=1 if self.stop_fails else 0, stderr="stop failed", stdout="")
        assert argv[0] == "/usr/bin/nft"
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert kwargs["timeout"] in {15, 20}
        if argv[1:3] == ["--file", "-"]:
            if self.fail_next_apply:
                self.fail_next_apply = False
                return SimpleNamespace(returncode=1, stderr="injected apply failure", stdout="")
            text = kwargs["input"]
            assert f"destroy table inet {policy.LEGACY_TABLE}" in text
            for mode in policy.MODES:
                if f'diary-{mode}-new' in text:
                    self.mode = mode
                    break
            else:
                raise AssertionError("unknown generated policy")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if argv[1:] == ["--json", "list", "table", "inet", policy.TABLE]:
            selected = self.stale_mode or self.mode
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(document(selected)))
        if argv[1:] == ["--json", "list", "tables"]:
            objects: list[dict[str, Any]] = [{"metainfo": {"json_schema_version": 1}}]
            if self.legacy_present:
                objects.append({"table": {"family": "inet", "name": policy.LEGACY_TABLE}})
            objects.append({"table": {"family": "inet", "name": policy.TABLE}})
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"nftables": objects}))
        raise AssertionError(f"unexpected command: {argv}")

    def manager(self) -> policy.NetworkPolicy:
        return policy.NetworkPolicy(UID, str(self.arm), runner=self.run)


@pytest.mark.skipif(
    os.geteuid() != 0 or os.environ.get("DIARY_TEST_NFT") != "1",
    reason="opt-in real nftables parser test; run in a disposable user/network namespace",
)
@pytest.mark.parametrize("mode", sorted(policy.MODES))
def test_generated_rules_validate_against_real_nft_parser(mode: str) -> None:
    result = policy.subprocess.run(
        ["/usr/bin/nft", "--check", "--file", "-"],
        input=policy.ruleset(mode, UID),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", sorted(policy.MODES))
def test_structural_readback_accepts_exact_live_policy(mode: str) -> None:
    policy.verify_document(document(mode), mode, UID)


def test_locked_policy_drops_new_established_and_every_other_tcp_udp_flow() -> None:
    text = policy.ruleset("locked", UID)
    assert text.count(" drop") == 3
    assert 'ct state new counter log prefix "diary-locked-new"' in text
    assert 'ct state established counter limit rate 10/second burst 20 packets log prefix "diary-locked-established"' in text
    assert f"meta skuid {UID} meta l4proto {{ tcp, udp }} counter drop" in text
    assert 'oifname "lo" return' in text
    assert "reject" not in text


def test_attended_policy_logs_only_new_flows_and_never_drops() -> None:
    text = policy.ruleset("attended", UID)
    assert 'log prefix "diary-attended-new"' in text
    assert "established" not in text
    assert " drop" not in text
    assert "reject" not in text


def test_dark_policy_preserves_audit_only_new_and_established_observation() -> None:
    text = policy.ruleset("dark", UID)
    assert 'log prefix "diary-dark-new"' in text
    assert 'log prefix "diary-dark-established"' in text
    assert " drop" not in text
    assert "reject" not in text


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["nftables"][3]["rule"]["expr"].pop(),
        lambda value: value["nftables"][3]["rule"].update(chain="forward"),
        lambda value: value["nftables"].append({"set": {"family": "inet", "table": policy.TABLE, "name": "unexpected"}}),
        lambda value: value["nftables"][2]["chain"].update(policy="drop"),
        lambda value: value["nftables"][3]["rule"]["expr"][0]["match"].update(right=UID + 1),
    ],
)
def test_missing_or_extra_live_state_is_rejected(mutation) -> None:
    value = document("locked")
    mutation(value)
    with pytest.raises(policy.PolicyError):
        policy.verify_document(value, "locked", UID)


def test_unarmed_controller_transitions_keep_accept_only_dark_policy(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path, armed=False)
    manager = kernel.manager()
    manager.activate("locked")
    manager.verify("locked")
    manager.activate("attended")
    manager.verify("attended")
    manager.failsafe()
    assert kernel.mode == "dark"
    assert kernel.stops == []


def test_armed_transitions_apply_and_read_back_each_mode(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    manager = kernel.manager()
    manager.activate("locked")
    manager.verify("locked")
    assert kernel.mode == "locked"
    manager.activate("attended")
    manager.verify("attended")
    assert kernel.mode == "attended"
    assert kernel.stops == []


def test_apply_failure_restores_locked_policy_and_stops_agents(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    kernel.mode = "attended"
    kernel.fail_next_apply = True
    with pytest.raises(policy.PolicyError, match="requested policy failed"):
        kernel.manager().activate("locked")
    assert kernel.mode == "locked"
    assert kernel.stops == [f"user@{UID}.service"]


def test_readback_failure_fails_closed_instead_of_reporting_attended(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    kernel.mode = "locked"
    kernel.stale_mode = "attended"
    with pytest.raises(policy.PolicyError, match="requested policy failed"):
        kernel.manager().verify("locked")
    assert kernel.mode == "locked"
    assert kernel.stops == [f"user@{UID}.service"]


def test_legacy_table_and_agent_stop_failure_are_not_hidden(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    kernel.legacy_present = True
    kernel.stop_fails = True
    with pytest.raises(policy.PolicyError, match="could not stop") as error:
        kernel.manager().activate("locked")
    assert "legacy audit table remains active" in str(error.value)
    assert kernel.mode == "locked"
    assert kernel.stops == [f"user@{UID}.service"]


def test_armed_failsafe_stops_agents_even_after_successful_lock(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    kernel.mode = "attended"
    kernel.manager().failsafe()
    assert kernel.mode == "locked"
    assert kernel.stops == [f"user@{UID}.service"]


def test_dark_requires_disarming_and_restores_observation(tmp_path: Path) -> None:
    kernel = FakeKernel(tmp_path)
    with pytest.raises(policy.PolicyError, match="remove"):
        kernel.manager().dark()
    kernel.arm.unlink()
    kernel.manager().dark()
    assert kernel.mode == "dark"
    assert kernel.stops == []


def test_malformed_json_and_table_inventory_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(policy.PolicyError):
        policy.verify_document({"nftables": "not-a-list"}, "locked", UID)
    with pytest.raises(policy.PolicyError):
        policy.tables_document_has_only_policy({"nftables": [{"chain": {}}]})
    kernel = FakeKernel(tmp_path)
    kernel.legacy_present = True
    with pytest.raises(policy.PolicyError):
        kernel.manager().activate("attended")
    assert kernel.stops == [f"user@{UID}.service"]


@pytest.mark.skipif(
    os.geteuid() != 0 or os.environ.get("DIARY_TEST_NFT") != "1",
    reason="opt-in real nftables test; run in a disposable user/network namespace",
)
def test_real_nftables_readback_and_dark_lifecycle(tmp_path: Path) -> None:
    arm = tmp_path / "armed"
    arm.write_text("")

    def isolated_runner(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "/usr/bin/systemctl":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return policy.subprocess.run(argv, **kwargs)

    manager = policy.NetworkPolicy(os.geteuid(), str(arm), runner=isolated_runner)
    manager.activate("locked")
    manager.verify("locked")
    manager.activate("attended")
    manager.verify("attended")
    arm.unlink()
    manager.dark()
    manager.verify("locked")
