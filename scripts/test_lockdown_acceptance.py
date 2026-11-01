"""Hermetic catalog, adapter-protocol, evidence, and cleanup tests for ACT-01."""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import lockdown_acceptance as acceptance


EXPECTED_CASES = {
    "preflight.dark_safe",
    "preflight.deployed_identity",
    "preflight.recovery",
    "activation.integrated",
    "network.ipv4_tcp",
    "network.ipv4_udp",
    "network.ipv6_tcp",
    "network.ipv6_udp",
    "network.dns_direct",
    "network.dns_dot",
    "network.dns_doh",
    "network.dns_mdns",
    "network.dns_llmnr",
    "network.lan",
    "network.tailscale",
    "network.deepseek_fetch",
    "network.established_stream",
    "network.loopback",
    "pipeline.allowance_isolation",
    "pipeline.mail_to_signal",
    "broker.signal_arbitrary",
    "broker.merge_push",
    "broker.sync_mutating",
    "broker.sync_safe",
    "broker.sudo",
    "broker.unknown",
    "immutability.lock_state",
    "immutability.pipeline_config",
    "immutability.endpoints",
    "immutability.audit",
    "failure.controller",
    "failure.audit_agent",
    "failure.audit_pipeline",
    "failure.policy_verification",
    "auth.stolen_session",
    "auth.fresh_ack",
    "auth.failed_unlock",
    "auth.agent_activity_expiry",
    "turn.idle_cancel",
    "notification.extra_fields",
    "notification.urls",
    "notification.unicode",
    "notification.length",
    "notification.frequency",
    "replay.no_replay",
    "boot.locked",
    "owner.first_cycle",
}


class FakeAdapter:
    def __init__(self, catalog: acceptance.Catalog):
        self.catalog = catalog
        self.calls: list[dict] = []
        self.failures: dict[str, str] = {}
        self.malformed: set[str] = set()
        self.interrupt: set[str] = set()
        self.cleanup_status = "pass"

    def call(self, request: dict, timeout_seconds: int) -> dict:
        self.calls.append(request)
        operation = request["operation"]
        if operation in self.interrupt:
            raise KeyboardInterrupt
        if operation in self.malformed:
            return {
                "protocol": 1,
                "status": "pass",
                "summary": "missing evidence",
                "evidence": {},
            }
        if operation == acceptance.CLEANUP_OPERATION:
            return {
                "protocol": 1,
                "status": self.cleanup_status,
                "summary": "cleanup finished",
                "evidence": {
                    "cleanup_complete": self.cleanup_status == "pass",
                    "locked_verified": True,
                    "blocked_replay_count": 0,
                },
            }
        case = next(case for case in self.catalog.cases if case.operation == operation)
        if operation in self.failures:
            return {
                "protocol": 1,
                "status": self.failures[operation],
                "summary": "injected adapter result",
                "evidence": {"probe_id": "failed-probe"},
            }
        evidence: dict[str, Any] = {
            key: 0 if key.endswith("count") else ["none"] if key == "unexpected_findings" else True
            for key in case.required_evidence
        }
        if case.reboot and request["phase"] == "execute":
            evidence.update({
                "pre_reboot_boot_id": "boot-before",
                "locked_verified": True,
            })
            status = "reboot_required"
        else:
            if case.reboot:
                evidence.update({
                    "post_reboot_boot_id": "boot-after",
                    "boot_changed": True,
                    "locked_verified": True,
                })
            status = "pass"
        return {
            "protocol": 1,
            "status": status,
            "summary": "fake isolated result",
            "evidence": evidence,
        }


@pytest.fixture
def catalog() -> acceptance.Catalog:
    return acceptance.load_catalog()


def test_shipped_catalog_has_every_source_and_act_case(catalog: acceptance.Catalog) -> None:
    assert {case.case_id for case in catalog.cases} == EXPECTED_CASES
    assert len(catalog.cases) == 47
    assert sum(case.reboot for case in catalog.cases) == 1
    assert acceptance.REQUIRED_COVERAGE <= {
        coverage for case in catalog.cases for coverage in case.coverage
    }


def test_catalog_rejects_missing_coverage_and_duplicate_ids(tmp_path: Path) -> None:
    raw = acceptance.DEFAULT_CATALOG.read_text()
    missing = tmp_path / "missing.toml"
    missing.write_text(raw.replace('"design.network"', '"other.network"'))
    with pytest.raises(acceptance.AcceptanceError, match="missing acceptance coverage"):
        acceptance.load_catalog(missing)
    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(raw + """

[[cases]]
id = "network.ipv4_tcp"
title = "duplicate"
operation = "network.duplicate"
coverage = ["other"]
dependencies = ["CTL-03"]
required_evidence = ["proof"]
timeout_seconds = 10
""")
    with pytest.raises(acceptance.AcceptanceError, match="must be unique"):
        acceptance.load_catalog(duplicate)


def test_catalog_must_be_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "target.toml"
    target.write_bytes(acceptance.DEFAULT_CATALOG.read_bytes())
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    with pytest.raises(acceptance.AcceptanceError, match="regular non-symlink"):
        acceptance.load_catalog(link)


def test_offline_plan_and_live_guard_do_not_create_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert acceptance.main(["plan"]) == 0
    output = capsys.readouterr().out
    assert "boot.locked" in output and acceptance.CLEANUP_OPERATION in output
    evidence = tmp_path / "evidence"
    assert acceptance.main([
        "run", "--adapter", "/nonexistent-adapter", "--evidence-root", str(evidence),
    ]) == 2
    assert not evidence.exists()


def test_complete_run_suspends_for_reboot_resumes_and_verifies(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "full-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert store.metadata["status"] == "reboot_required"
    assert store.metadata["cleanup"] is None
    assert adapter.calls[-1]["case_id"] == "boot.locked"
    assert not any(call["case_id"] == "owner.first_cycle" for call in adapter.calls)

    resumed = acceptance.EvidenceStore.load(store.path, catalog)
    assert acceptance.execute_suite(catalog, resumed, adapter, resume=True) == 0
    assert resumed.metadata["status"] == "passed"
    assert adapter.calls[-2]["case_id"] == "owner.first_cycle"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION
    boot_calls = [call for call in adapter.calls if call["case_id"] == "boot.locked"]
    assert [call["phase"] for call in boot_calls] == ["execute", "verify"]
    summary = acceptance.verify_evidence(catalog, resumed)
    assert summary["status"] == "passed" and summary["cases"] == 47
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.path / "run.json").stat().st_mode) == 0o600
    assert acceptance.main(["verify", str(store.path)]) == 0


def test_failure_stops_sequence_and_always_attempts_cleanup(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "failure-run")
    adapter = FakeAdapter(catalog)
    adapter.failures["network.locked_ipv4_tcp"] = "fail"
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    assert store.metadata["status"] == "failed"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION
    assert not any(call["case_id"] == "network.ipv4_udp" for call in adapter.calls)
    with pytest.raises(acceptance.AcceptanceError, match="not marked passed"):
        acceptance.verify_evidence(catalog, store)


def test_blocked_dependency_is_not_a_pass(catalog: acceptance.Catalog, tmp_path: Path) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "blocked-run")
    adapter = FakeAdapter(catalog)
    adapter.failures["preflight.deployed_identity"] = "blocked"
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    assert store.metadata["records"][-1]["status"] == "blocked"
    assert store.metadata["status"] == "failed"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_malformed_success_is_failed_and_cleaned_up(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "malformed-run")
    adapter = FakeAdapter(catalog)
    adapter.malformed.add("preflight.dark_safe")
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    record = json.loads((store.path / store.metadata["records"][0]["path"]).read_text())
    assert record["response"]["status"] == "fail"
    assert "no evidence" in record["response"]["evidence"]["harness_error"]
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_interruption_is_recorded_and_cleaned_up(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "interrupt-run")
    adapter = FakeAdapter(catalog)
    adapter.interrupt.add("activation.integrated")
    assert acceptance.execute_suite(catalog, store, adapter) == 130
    assert store.metadata["status"] == "failed"
    assert "interrupted" in store.metadata["harness_errors"][0]["error"]
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_unexpected_harness_failure_still_attempts_cleanup(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "error-run")
    adapter = FakeAdapter(catalog)
    with patch.object(adapter, "call", side_effect=RuntimeError("injected harness error")):
        with pytest.raises(RuntimeError, match="injected"):
            acceptance.execute_suite(catalog, store, adapter)
    assert store.metadata["status"] == "failed"
    assert store.metadata["cleanup"]["status"] == "fail"


def test_cleanup_failure_invalidates_otherwise_passing_run(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "cleanup-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    adapter.cleanup_status = "fail"
    resumed = acceptance.EvidenceStore.load(store.path, catalog)
    assert acceptance.execute_suite(catalog, resumed, adapter, resume=True) == 1
    assert resumed.metadata["status"] == "failed"
    assert resumed.metadata["cleanup"]["status"] == "fail"


def test_evidence_tampering_and_catalog_drift_are_rejected(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "tamper-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert acceptance.execute_suite(catalog, store, adapter, resume=True) == 0
    first = store.path / store.metadata["records"][0]["path"]
    first.write_text(first.read_text().replace("fake isolated result", "tampered result"))
    with pytest.raises(acceptance.AcceptanceError, match="digest mismatch"):
        acceptance.verify_evidence(catalog, store)
    changed = tmp_path / "changed.toml"
    changed.write_bytes(acceptance.DEFAULT_CATALOG.read_bytes().replace(b"ACT-01", b"ACT-02", 1))
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.EvidenceStore.load(store.path, acceptance.load_catalog(changed))


def test_reboot_requires_locked_changed_boot_proof(catalog: acceptance.Catalog) -> None:
    case = next(case for case in catalog.cases if case.reboot)
    response = {
        "protocol": 1,
        "status": "pass",
        "summary": "same boot cannot pass",
        "evidence": {
            **{key: True for key in case.required_evidence},
            "boot_changed": False,
            "post_reboot_boot_id": "same-boot",
        },
    }
    with pytest.raises(acceptance.AcceptanceError, match="changed-boot"):
        acceptance.validate_response(case, response, resume=True)


def test_false_and_zero_are_valid_evidence_measurements(catalog: acceptance.Catalog) -> None:
    case = acceptance.Case(
        case_id="example.case",
        title="measurement",
        operation="example.measurement",
        coverage=("other",),
        dependencies=("ACT-01",),
        required_evidence=("replay_observed", "blocked_replay_count"),
        timeout_seconds=10,
    )
    response = {
        "protocol": 1,
        "status": "pass",
        "summary": "no replay is a valid proof",
        "evidence": {"replay_observed": False, "blocked_replay_count": 0},
    }
    assert acceptance.validate_response(case, response)["status"] == "pass"


def test_adapter_timeout_and_process_failure_cannot_pass(tmp_path: Path) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    adapter = acceptance.SubprocessAdapter(executable, require_root_owned=False)
    with patch.object(
        acceptance.subprocess, "run", side_effect=subprocess.TimeoutExpired(["adapter"], 1),
    ):
        reply = adapter.call({"protocol": 1}, 1)
    assert reply["status"] == "fail" and reply["evidence"]["harness_error"] == "timeout"
    case = acceptance.Case(
        "example.case", "process", "example.process", ("other",), ("ACT-01",),
        ("proof",), 10,
    )
    with pytest.raises(acceptance.AcceptanceError, match="failed adapter process"):
        acceptance.validate_response(case, {
            "protocol": 1,
            "status": "pass",
            "summary": "false pass",
            "evidence": {"proof": True},
            "_process": {"returncode": 1, "stderr": ""},
        })


def test_subprocess_adapter_uses_fixed_environment_and_json_protocol(tmp_path: Path) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("""#!/usr/bin/python3
import json
import os
import sys
request = json.load(sys.stdin)
assert os.environ == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
print(json.dumps({
    "protocol": 1,
    "status": "pass",
    "summary": "isolated executable",
    "evidence": {"probe_id": request["case_id"]},
}))
""")
    executable.chmod(0o700)
    adapter = acceptance.SubprocessAdapter(executable, require_root_owned=False)
    reply = adapter.call({"protocol": 1, "case_id": "isolated"}, 10)
    assert reply["status"] == "pass"
    assert reply["_process"]["returncode"] == 0


def test_live_adapter_rejects_symlink_and_writable_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o777)
    with pytest.raises(acceptance.AcceptanceError, match="root-owned|writable"):
        acceptance.SubprocessAdapter(target)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(acceptance.AcceptanceError, match="non-symlink"):
        acceptance.SubprocessAdapter(link, require_root_owned=False)


def test_run_id_and_evidence_symlink_are_rejected(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    with pytest.raises(acceptance.AcceptanceError, match="invalid run ID"):
        acceptance.EvidenceStore.create(tmp_path, catalog, "../escape")
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "safe-run")
    (store.path / "run.json").unlink()
    (store.path / "run.json").symlink_to(tmp_path / "outside")
    with pytest.raises(acceptance.AcceptanceError, match="not regular"):
        acceptance.EvidenceStore.load(store.path, catalog)


def test_owner_acceptance_and_zero_replay_are_machine_checked(
    catalog: acceptance.Catalog,
) -> None:
    case = next(case for case in catalog.cases if case.case_id == "owner.first_cycle")
    evidence: dict[str, Any] = {
        key: 0 if key.endswith("count") else ["none"] if key == "unexpected_findings" else True
        for key in case.required_evidence
    }
    response = {
        "protocol": 1,
        "status": "pass",
        "summary": "owner result",
        "evidence": evidence,
    }
    evidence["owner_accepted"] = False
    with pytest.raises(acceptance.AcceptanceError, match="affirmative.*owner_accepted"):
        acceptance.validate_response(case, response)
    evidence["owner_accepted"] = True
    evidence["blocked_replay_count"] = 1
    with pytest.raises(acceptance.AcceptanceError, match="zero.*blocked_replay_count"):
        acceptance.validate_response(case, response)


def test_verify_checks_cleanup_response_not_only_its_index(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = acceptance.EvidenceStore.create(tmp_path, catalog, "cleanup-proof-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert acceptance.execute_suite(catalog, store, adapter, resume=True) == 0
    path = store.path / "cleanup.json"
    record = json.loads(path.read_text())
    record["response"]["evidence"]["cleanup_complete"] = False
    raw = acceptance._json_bytes(record)
    path.write_bytes(raw)
    store.metadata["cleanup"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(acceptance.AcceptanceError, match="affirmative.*cleanup_complete"):
        acceptance.verify_evidence(catalog, store)


def test_live_catalog_must_be_root_owned_and_immutable_to_agents(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    result = acceptance.main([
        "run",
        "--adapter", "/nonexistent-adapter",
        "--evidence-root", str(evidence),
        "--execute",
        "--acknowledge-live", acceptance.LIVE_ACKNOWLEDGEMENT,
    ])
    assert result == 2
    assert not evidence.exists()
