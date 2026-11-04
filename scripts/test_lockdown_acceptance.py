"""Hermetic catalog, adapter-protocol, evidence, and cleanup tests for ACT-01."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
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


BOOT_BEFORE = "00000000-0000-0000-0000-000000000001"
BOOT_AFTER = "00000000-0000-0000-0000-000000000002"
OID_MAP = {
    "diary": "1111111111111111111111111111111111111111",
    "scripts": "2222222222222222222222222222222222222222",
}


def create_store(tmp_path: Path, catalog: acceptance.Catalog, run_id: str) -> acceptance.EvidenceStore:
    return acceptance.EvidenceStore.create(
        tmp_path, catalog, run_id, require_root_owned=False,
    )


def load_store(store: acceptance.EvidenceStore, catalog: acceptance.Catalog) -> acceptance.EvidenceStore:
    return acceptance.EvidenceStore.load(store.path, catalog, require_root_owned=False)


def resume_suite(
    catalog: acceptance.Catalog, store: acceptance.EvidenceStore, adapter: acceptance.Adapter,
) -> int:
    with patch.object(acceptance, "current_boot_id", return_value=BOOT_AFTER):
        return acceptance.execute_suite(catalog, store, adapter, resume=True)


def verify_run(catalog: acceptance.Catalog, store: acceptance.EvidenceStore) -> dict[str, Any]:
    return acceptance.verify_evidence(catalog, store, running_boot_id=BOOT_AFTER)


def evidence_for(case: acceptance.Case) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in case.required_evidence:
        if key in case.expected:
            evidence[key] = case.expected[key]
        elif key in {"approved_commit_ids", "deployed_commit_ids", "activated_commit_ids"}:
            evidence[key] = dict(OID_MAP)
        elif key == "unexpected_findings":
            evidence[key] = ["none"]
        else:
            evidence[key] = f"proof-{key}"
    return evidence


class FakeAdapter:
    def __init__(self, catalog: acceptance.Catalog):
        self.catalog = catalog
        self.calls: list[dict] = []
        self.failures: dict[str, str] = {}
        self.malformed: set[str] = set()
        self.interrupt: set[str] = set()
        self.overrides: dict[str, dict[str, Any]] = {}
        self.cleanup_status = "pass"

    def call(self, request: dict, timeout_seconds: int) -> dict:
        self.calls.append(request)
        operation = request["operation"]
        if operation in self.interrupt:
            raise KeyboardInterrupt
        if operation in self.malformed:
            return {
                "protocol": 2,
                "status": "pass",
                "summary": "missing evidence",
                "evidence": {},
            }
        if operation == acceptance.CLEANUP_OPERATION:
            evidence = evidence_for(acceptance._cleanup_case())
            evidence["cleanup_complete"] = self.cleanup_status == "pass"
            return {
                "protocol": 2,
                "status": self.cleanup_status,
                "summary": "cleanup finished",
                "evidence": evidence,
            }
        case = next(case for case in self.catalog.cases if case.operation == operation)
        if operation in self.failures:
            return {
                "protocol": 2,
                "status": self.failures[operation],
                "summary": "injected adapter result",
                "evidence": {"probe_id": "failed-probe"},
            }
        evidence = evidence_for(case)
        if case.reboot and request["phase"] == "execute":
            evidence["pre_reboot_boot_id"] = BOOT_BEFORE
            status = "reboot_required"
        else:
            if case.reboot:
                evidence.update({
                    "pre_reboot_boot_id": BOOT_BEFORE,
                    "post_reboot_boot_id": BOOT_AFTER,
                    "boot_changed": True,
                })
            status = "pass"
        evidence.update(self.overrides.get(operation, {}))
        return {
            "protocol": 2,
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


def test_catalog_rejects_missing_coverage_and_any_case_set_change(tmp_path: Path) -> None:
    raw = acceptance.DEFAULT_CATALOG.read_text()
    missing = tmp_path / "missing.toml"
    missing.write_text(raw.replace('"design.network"', '"other.network"'))
    with pytest.raises(acceptance.AcceptanceError, match="missing acceptance coverage"):
        acceptance.load_catalog(missing)
    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(raw.replace("\n[expect]\n", """

[[cases]]
id = "network.ipv4_tcp"
title = "duplicate"
operation = "network.duplicate"
coverage = ["other"]
dependencies = ["CTL-03"]
required_evidence = ["proof"]
timeout_seconds = 10

[expect]
""", 1))
    with pytest.raises(acceptance.AcceptanceError, match="exactly the 47"):
        acceptance.load_catalog(duplicate)
    renamed = tmp_path / "renamed.toml"
    renamed.write_text(raw.replace('id = "network.ipv4_tcp"', 'id = "network.changed"', 1))
    with pytest.raises(acceptance.AcceptanceError, match="identity or order mismatch"):
        acceptance.load_catalog(renamed)
    wrong_suite = tmp_path / "wrong-suite.toml"
    wrong_suite.write_text(raw.replace('suite_id = "act01-lockdown-acceptance"', 'suite_id = "other"', 1))
    with pytest.raises(acceptance.AcceptanceError, match="suite identity mismatch"):
        acceptance.load_catalog(wrong_suite)


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
    with patch.object(acceptance, "load_live_catalog") as load_live:
        assert acceptance.main(["run", "--evidence-root", str(evidence)]) == 2
    load_live.assert_not_called()
    assert not evidence.exists()


def test_complete_run_suspends_for_reboot_resumes_and_verifies(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "full-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert store.metadata["status"] == "reboot_required"
    assert store.metadata["cleanup"] is None
    assert adapter.calls[-1]["case_id"] == "boot.locked"
    assert not any(call["case_id"] == "owner.first_cycle" for call in adapter.calls)

    resumed = load_store(store, catalog)
    assert resume_suite(catalog, resumed, adapter) == 0
    assert resumed.metadata["status"] == "passed"
    assert adapter.calls[-2]["case_id"] == "owner.first_cycle"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION
    boot_calls = [call for call in adapter.calls if call["case_id"] == "boot.locked"]
    assert [call["phase"] for call in boot_calls] == ["execute", "verify"]
    summary = verify_run(catalog, resumed)
    assert summary["status"] == "passed" and summary["cases"] == 47
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.path / "run.json").stat().st_mode) == 0o600
    with (
        patch.object(acceptance.EvidenceStore, "load", return_value=resumed),
        patch.object(acceptance, "current_boot_id", return_value=BOOT_AFTER),
        patch.object(acceptance, "SubprocessAdapter") as live_adapter,
    ):
        assert acceptance.main(["verify", str(store.path)]) == 0
    live_adapter.assert_not_called()


def test_failure_stops_sequence_and_always_attempts_cleanup(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "failure-run")
    adapter = FakeAdapter(catalog)
    adapter.failures["network.locked_ipv4_tcp"] = "fail"
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    assert store.metadata["status"] == "failed"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION
    assert not any(call["case_id"] == "network.ipv4_udp" for call in adapter.calls)
    with pytest.raises(acceptance.AcceptanceError, match="not marked passed"):
        verify_run(catalog, store)


def test_blocked_dependency_is_not_a_pass(catalog: acceptance.Catalog, tmp_path: Path) -> None:
    store = create_store(tmp_path, catalog, "blocked-run")
    adapter = FakeAdapter(catalog)
    adapter.failures["preflight.deployed_identity"] = "blocked"
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    assert store.metadata["records"][-1]["status"] == "blocked"
    assert store.metadata["status"] == "failed"
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_malformed_success_is_failed_and_cleaned_up(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "malformed-run")
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
    store = create_store(tmp_path, catalog, "interrupt-run")
    adapter = FakeAdapter(catalog)
    adapter.interrupt.add("activation.integrated")
    assert acceptance.execute_suite(catalog, store, adapter) == 130
    assert store.metadata["status"] == "failed"
    assert "interrupted" in store.metadata["harness_errors"][0]["error"]
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_unexpected_harness_failure_still_attempts_cleanup(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "error-run")
    adapter = FakeAdapter(catalog)
    with (
        patch.object(adapter, "call", side_effect=RuntimeError("injected harness error")),
        pytest.raises(RuntimeError, match="injected"),
    ):
        acceptance.execute_suite(catalog, store, adapter)
    assert store.metadata["status"] == "failed"
    assert store.metadata["cleanup"]["status"] == "fail"


def test_cleanup_failure_invalidates_otherwise_passing_run(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "cleanup-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    adapter.cleanup_status = "fail"
    resumed = load_store(store, catalog)
    assert resume_suite(catalog, resumed, adapter) == 1
    assert resumed.metadata["status"] == "failed"
    assert resumed.metadata["cleanup"]["status"] == "fail"


def test_evidence_tampering_and_catalog_drift_are_rejected(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "tamper-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert resume_suite(catalog, store, adapter) == 0
    first = store.path / store.metadata["records"][0]["path"]
    first.write_text(first.read_text().replace("fake isolated result", "tampered result"))
    with pytest.raises(acceptance.AcceptanceError, match="digest mismatch"):
        verify_run(catalog, store)
    changed = tmp_path / "changed.toml"
    changed.write_bytes(acceptance.DEFAULT_CATALOG.read_bytes().replace(b"ACT-01", b"ACT-02", 1))
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.EvidenceStore.load(
            store.path, acceptance.load_catalog(changed), require_root_owned=False,
        )


def test_reboot_requires_distinct_ids_bound_to_running_kernel(catalog: acceptance.Catalog) -> None:
    case = next(case for case in catalog.cases if case.reboot)
    evidence = evidence_for(case)
    evidence.update({
        "pre_reboot_boot_id": BOOT_BEFORE,
        "post_reboot_boot_id": BOOT_BEFORE,
        "boot_changed": True,
    })
    response = {
        "protocol": 2,
        "status": "pass",
        "summary": "same boot cannot pass",
        "evidence": evidence,
    }
    with pytest.raises(acceptance.AcceptanceError, match="distinct valid pre/post"):
        acceptance.validate_response(case, response, resume=True, running_boot_id=BOOT_BEFORE)
    evidence["post_reboot_boot_id"] = BOOT_AFTER
    with pytest.raises(acceptance.AcceptanceError, match="running kernel"):
        acceptance.validate_response(case, response, resume=True, running_boot_id=BOOT_BEFORE)
    assert acceptance.validate_response(
        case, response, resume=True, running_boot_id=BOOT_AFTER,
    )["status"] == "pass"


def test_false_and_zero_are_valid_evidence_measurements(catalog: acceptance.Catalog) -> None:
    case = acceptance.Case(
        case_id="example.case",
        title="measurement",
        operation="example.measurement",
        coverage=("other",),
        dependencies=("ACT-01",),
        required_evidence=("replay_observed", "blocked_replay_count"),
        expected={"replay_observed": False, "blocked_replay_count": 0},
        oid_equal=(),
        timeout_seconds=10,
    )
    response = {
        "protocol": 2,
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
        reply = adapter.call({"protocol": 2}, 1)
    assert reply["status"] == "fail" and reply["evidence"]["harness_error"] == "timeout"
    case = acceptance.Case(
        "example.case", "process", "example.process", ("other",), ("ACT-01",),
        ("proof",), {"proof": True}, (), 10,
    )
    with pytest.raises(acceptance.AcceptanceError, match="failed adapter process"):
        acceptance.validate_response(case, {
            "protocol": 2,
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
    "protocol": 2,
    "status": "pass",
    "summary": "isolated executable",
    "evidence": {"probe_id": request["case_id"]},
}))
""")
    executable.chmod(0o700)
    adapter = acceptance.SubprocessAdapter(executable, require_root_owned=False)
    reply = adapter.call({"protocol": 2, "case_id": "isolated"}, 10)
    assert reply["status"] == "pass"
    assert reply["_process"]["returncode"] == 0


def test_live_adapter_rejects_symlink_and_writable_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o777)
    with pytest.raises(acceptance.AcceptanceError, match="fixed installed path"):
        acceptance.SubprocessAdapter(target)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(acceptance.AcceptanceError, match="non-symlink"):
        acceptance.SubprocessAdapter(link, require_root_owned=False)


def test_run_id_and_evidence_symlink_are_rejected(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    with pytest.raises(acceptance.AcceptanceError, match="invalid run ID"):
        create_store(tmp_path, catalog, "../escape")
    store = create_store(tmp_path, catalog, "safe-run")
    (store.path / "run.json").unlink()
    (store.path / "run.json").symlink_to(tmp_path / "outside")
    with pytest.raises(acceptance.AcceptanceError, match="not regular"):
        load_store(store, catalog)


def test_owner_acceptance_and_zero_replay_are_machine_checked(
    catalog: acceptance.Catalog,
) -> None:
    case = next(case for case in catalog.cases if case.case_id == "owner.first_cycle")
    evidence = evidence_for(case)
    response = {
        "protocol": 2,
        "status": "pass",
        "summary": "owner result",
        "evidence": evidence,
    }
    evidence["owner_accepted"] = False
    with pytest.raises(acceptance.AcceptanceError, match="owner_accepted must be exactly True"):
        acceptance.validate_response(case, response)
    evidence["owner_accepted"] = True
    evidence["blocked_replay_count"] = 1
    with pytest.raises(acceptance.AcceptanceError, match="blocked_replay_count must be exactly 0"):
        acceptance.validate_response(case, response)


def test_verify_checks_cleanup_response_not_only_its_index(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "cleanup-proof-run")
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert resume_suite(catalog, store, adapter) == 0
    path = store.path / "cleanup.json"
    record = json.loads(path.read_text())
    record["response"]["evidence"]["cleanup_complete"] = False
    raw = acceptance._json_bytes(record)
    path.write_bytes(raw)
    store.metadata["cleanup"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(acceptance.AcceptanceError, match="cleanup_complete must be exactly True"):
        verify_run(catalog, store)


def test_live_run_uses_only_fixed_catalog_and_adapter(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    with (
        patch.object(
            acceptance, "load_live_catalog",
            side_effect=acceptance.AcceptanceError("fixed trusted catalog unavailable"),
        ) as load_live,
        patch.object(acceptance, "SubprocessAdapter") as adapter,
    ):
        result = acceptance.main([
            "run",
            "--evidence-root", str(evidence),
            "--execute",
            "--acknowledge-live", acceptance.LIVE_ACKNOWLEDGEMENT,
        ])
    assert result == 2
    load_live.assert_called_once_with()
    adapter.assert_not_called()
    assert not evidence.exists()
    with pytest.raises(SystemExit):
        acceptance._parser().parse_args([
            "run", "--adapter", "/tmp/agent-adapter", "--evidence-root", str(evidence),
        ])


def complete_run(
    catalog: acceptance.Catalog, tmp_path: Path, run_id: str,
) -> acceptance.EvidenceStore:
    store = create_store(tmp_path, catalog, run_id)
    adapter = FakeAdapter(catalog)
    assert acceptance.execute_suite(catalog, store, adapter) == 3
    assert resume_suite(catalog, store, adapter) == 0
    return store


def rewrite_evidence(
    store: acceptance.EvidenceStore, case_id: str, changes: dict[str, Any],
) -> None:
    indexes = [item for item in store.metadata["records"] if item["case_id"] == case_id]
    item = indexes[-1]
    path = store.path / item["path"]
    record = json.loads(path.read_text())
    record["response"]["evidence"].update(changes)
    raw = acceptance._json_bytes(record)
    path.write_bytes(raw)
    item["sha256"] = hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(("case_id", "field"), [
    ("network.ipv4_tcp", "denied"),
    ("notification.extra_fields", "all_rejected"),
    ("auth.fresh_ack", "passkey_user_verified"),
])
def test_contradictory_boolean_evidence_cannot_pass(
    catalog: acceptance.Catalog, case_id: str, field: str,
) -> None:
    case = next(case for case in catalog.cases if case.case_id == case_id)
    evidence = evidence_for(case)
    evidence[field] = False
    with pytest.raises(acceptance.AcceptanceError, match=f"{field} must be exactly True"):
        acceptance.validate_response(case, {
            "protocol": 2, "status": "pass", "summary": "contradiction", "evidence": evidence,
        })


def test_boolean_evidence_requires_a_catalog_declaration() -> None:
    case = acceptance.Case(
        "example.case", "undeclared", "example.undeclared", ("other",), ("ACT-01",),
        ("assertion", "blocked_replay_count"), {"blocked_replay_count": 0}, (), 10,
    )
    with pytest.raises(acceptance.AcceptanceError, match="no catalog expectation"):
        acceptance.validate_response(case, {
            "protocol": 2,
            "status": "pass",
            "summary": "undeclared boolean",
            "evidence": {"assertion": True, "blocked_replay_count": 0},
        })


def test_oid_maps_require_exact_equality_and_full_git_ids(catalog: acceptance.Catalog) -> None:
    case = next(case for case in catalog.cases if case.case_id == "preflight.deployed_identity")
    evidence = evidence_for(case)
    evidence["deployed_commit_ids"] = {**OID_MAP, "scripts": "3" * 40}
    with pytest.raises(acceptance.AcceptanceError, match="mismatched commit OID maps"):
        acceptance.validate_response(case, {
            "protocol": 2, "status": "pass", "summary": "OID mismatch", "evidence": evidence,
        })
    evidence["deployed_commit_ids"] = {**OID_MAP, "scripts": "short"}
    with pytest.raises(acceptance.AcceptanceError, match="exact lowercase 40-character"):
        acceptance.validate_response(case, {
            "protocol": 2, "status": "pass", "summary": "OID malformed", "evidence": evidence,
        })


def test_activation_stops_immediately_on_cross_case_oid_drift(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = create_store(tmp_path, catalog, "runtime-oid-run")
    adapter = FakeAdapter(catalog)
    changed = {**OID_MAP, "scripts": "3" * 40}
    adapter.overrides["activation.integrated"] = {
        "approved_commit_ids": changed,
        "deployed_commit_ids": changed,
        "activated_commit_ids": changed,
    }
    assert acceptance.execute_suite(catalog, store, adapter) == 1
    assert store.metadata["status"] == "failed"
    assert store.metadata["commit_oids"] == OID_MAP
    assert store.metadata["records"][-1]["case_id"] == "activation.integrated"
    assert not any(call["case_id"] == "network.ipv4_tcp" for call in adapter.calls)
    assert adapter.calls[-1]["operation"] == acceptance.CLEANUP_OPERATION


def test_cross_case_oid_binding_is_verified(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = complete_run(catalog, tmp_path, "cross-oid-run")
    changed = {**OID_MAP, "scripts": "3" * 40}
    rewrite_evidence(store, "activation.integrated", {
        "approved_commit_ids": changed,
        "deployed_commit_ids": changed,
        "activated_commit_ids": changed,
    })
    with pytest.raises(acceptance.AcceptanceError, match="cross-case OID binding mismatch"):
        verify_run(catalog, store)


def test_cross_record_boot_ids_and_running_kernel_are_verified(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    store = complete_run(catalog, tmp_path, "cross-boot-run")
    rewrite_evidence(store, "boot.locked", {
        "pre_reboot_boot_id": "00000000-0000-0000-0000-000000000003",
    })
    with pytest.raises(acceptance.AcceptanceError, match="inconsistent pre-boot IDs"):
        verify_run(catalog, store)
    store = complete_run(catalog, tmp_path, "stale-boot-run")
    with pytest.raises(acceptance.AcceptanceError, match="running kernel"):
        acceptance.verify_evidence(catalog, store, running_boot_id=BOOT_BEFORE)


@pytest.mark.parametrize("case_id", ["replay.no_replay", "owner.first_cycle"])
def test_both_unlocks_require_zero_suppressed_replays(
    catalog: acceptance.Catalog, case_id: str,
) -> None:
    case = next(case for case in catalog.cases if case.case_id == case_id)
    evidence = evidence_for(case)
    evidence["suppressed_replay_count"] = 1
    with pytest.raises(acceptance.AcceptanceError, match="suppressed_replay_count must be exactly 0"):
        acceptance.validate_response(case, {
            "protocol": 2, "status": "pass", "summary": "replay", "evidence": evidence,
        })


def test_trusted_ancestors_reject_symlinks_and_writable_directories(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(acceptance.AcceptanceError, match="ancestor is unsafe"):
        acceptance._check_trusted_ancestors(link / "adapter")
    tmp_path.chmod(0o777)
    with pytest.raises(acceptance.AcceptanceError, match="ancestor is unsafe"):
        acceptance._check_trusted_ancestors(tmp_path / "adapter")
    tmp_path.chmod(0o700)


def test_open_trusted_file_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("data")
    link = tmp_path / "link"
    link.symlink_to(target)
    with (
        patch.object(acceptance, "_check_trusted_ancestors"),
        pytest.raises(acceptance.AcceptanceError, match="cannot open trusted file"),
    ):
        acceptance._open_trusted_file(link)


def test_trusted_adapter_executes_open_inode_after_path_replacement(tmp_path: Path) -> None:
    executable = tmp_path / "adapter"
    executable.write_text("""#!/usr/bin/python3
import json
import sys
json.load(sys.stdin)
print(json.dumps({
    "protocol": 2,
    "status": "pass",
    "summary": "original trusted inode",
    "evidence": {"probe_id": "original"},
}))
""")
    executable.chmod(0o700)
    real_fstat = os.fstat

    def root_fstat(fd: int):
        info = real_fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode)

    with (
        patch.object(acceptance, "LIVE_ADAPTER", executable),
        patch.object(acceptance, "_check_trusted_ancestors"),
        patch.object(acceptance.os, "fstat", side_effect=root_fstat),
    ):
        adapter = acceptance.SubprocessAdapter(executable)
    try:
        executable.unlink()
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o700)
        reply = adapter.call({"protocol": 2}, 10)
        assert reply["status"] == "pass"
        assert reply["summary"] == "original trusted inode"
    finally:
        adapter.close()


def test_evidence_directories_require_mode_and_root_ownership(
    catalog: acceptance.Catalog, tmp_path: Path,
) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    with pytest.raises(acceptance.AcceptanceError, match="mode 0700"):
        create_store(loose, catalog, "mode-run")
    store = create_store(tmp_path, catalog, "directory-run")
    store.path.chmod(0o755)
    with pytest.raises(acceptance.AcceptanceError, match="mode 0700"):
        load_store(store, catalog)
    store.path.chmod(0o700)
    real_lstat = Path.lstat

    def foreign_root(path: Path):
        info = real_lstat(path)
        if path == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
        return info

    with (
        patch.object(Path, "lstat", foreign_root),
        pytest.raises(acceptance.AcceptanceError, match="root-owned"),
    ):
        acceptance.EvidenceStore.create(tmp_path, catalog, "ownership-run")


def test_atomic_rename_fsyncs_containing_directory(tmp_path: Path) -> None:
    with patch.object(
        acceptance, "_fsync_directory", wraps=acceptance._fsync_directory,
    ) as fsync:
        acceptance._atomic_write(tmp_path / "artifact", b"evidence")
    fsync.assert_called_once_with(tmp_path)


def test_catalog_cannot_drop_expectations_or_oid_predicates(tmp_path: Path) -> None:
    raw = acceptance.DEFAULT_CATALOG.read_text()
    no_expect = tmp_path / "no-expect.toml"
    no_expect.write_text(raw.replace(
        '"network.ipv4_tcp" = { denied = true, locked_verified = true }\n', "", 1,
    ))
    with pytest.raises(acceptance.AcceptanceError, match="expectation.*case set mismatch"):
        acceptance.load_catalog(no_expect)
    no_oid = tmp_path / "no-oid.toml"
    no_oid.write_text(raw.replace(
        '"activation.integrated" = ["approved_commit_ids", "deployed_commit_ids", "activated_commit_ids"]\n',
        "", 1,
    ))
    with pytest.raises(acceptance.AcceptanceError, match="required OID equality predicates"):
        acceptance.load_catalog(no_oid)
