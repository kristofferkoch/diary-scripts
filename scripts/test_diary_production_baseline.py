"""Isolated tests for AUD-05 coverage, classification, and review evidence."""
from __future__ import annotations

import copy
import datetime as dt
import grp
import json
import os
import threading
from pathlib import Path

import pytest

from scripts import diary_audit as audit
from scripts import diary_production_baseline as baseline_tool

START = dt.datetime(2026, 11, 1, 9, tzinfo=dt.UTC)
END = START + dt.timedelta(hours=25)
GENERATED = END + dt.timedelta(hours=2)


def group(fingerprint: str, **changes) -> dict:
    value = {
        "fingerprint": fingerprint * 64,
        "count": 1,
        "records": 1,
        "event": "network.flow",
        "component": "nftables",
        "operation": "connect",
        "decision": "would_deny",
        "lock_state": "not_applicable",
        "reason": "audit_only",
        "destination_ip": "192.0.2.10",
        "destination_port": 443,
        "protocol": "tcp",
        "caller_uid": "1001",
    }
    value.update(changes)
    return value


def coverage(hours: int = 25) -> dict:
    end = START + dt.timedelta(hours=hours)
    observed = (START + dt.timedelta(minutes=30)).isoformat()
    record = {"observed_at": observed, "reference": "trusted-test-evidence", "result": "passed"}
    return {
        "schema_version": 1,
        "interval": {
            "started_at": START.isoformat(),
            "ended_at": end.isoformat(),
            "evidence": [
                {**record, "observed_at": START.isoformat()},
                {**record, "observed_at": end.isoformat()},
            ],
        },
        "coverage": {name: [record] for name in baseline_tool.COVERAGE_CHECKS},
    }


def baseline(groups: list[dict], **changes) -> dict:
    value = {
        "schema_version": 1,
        "report": "baseline",
        "namespace": "diary-lock",
        "since": START.isoformat(),
        "until": END.isoformat(),
        "generated_at": GENERATED.isoformat(),
        "event_count": sum(item["count"] for item in groups),
        "record_count": len(groups),
        "duplicate_record_count": 0,
        "truncated": False,
        "groups": groups,
        "ok": True,
    }
    value.update(changes)
    return value


def expected() -> dict:
    return {
        "schema_version": 1,
        "expected_destinations": [
            {
                "match": {
                    "event": "network.flow",
                    "destination_ip": "192.0.2.10",
                    "destination_port": 443,
                    "protocol": "tcp",
                },
                "reference": "owner-approved-test-destination",
            },
        ],
        "routine_expected_failures": [
            {
                "match": {
                    "event": "broker.decision",
                    "component": "diary-sync",
                    "operation": "sync",
                    "caller_uid": "1001",
                },
                "reference": "owner-classified-test-sync",
            },
        ],
    }


def build(groups: list[dict], review: object | None = None, expected_value: object | None = None) -> dict:
    return baseline_tool.build_report(
        baseline(groups),
        coverage(),
        expected() if expected_value is None else expected_value,
        review,
        generated_at=GENERATED,
    )


def review_for(report: dict, **changes) -> dict:
    required = sorted({
        item["fingerprint"]
        for kind in baseline_tool.FINDING_KINDS[1:]
        for item in report["findings"][kind]
    })
    value = {
        "schema_version": 1,
        "report_sha256": report["report_sha256"],
        "expected_flows_sha256": report["expected_flows_sha256"],
        "reviewed_at": (END + dt.timedelta(hours=1)).isoformat(),
        "review_reference": "owner-review-test-record",
        "expected_flow_classifications_reviewed": True,
        "acknowledged_fingerprints": required,
    }
    value.update(changes)
    return value


def test_classification_separates_expected_novel_repeated_and_broker_findings() -> None:
    groups = [
        group("a", count=4),
        group("b", count=2, destination_ip="192.0.2.20"),
        group(
            "c",
            count=3,
            event="dns.alternate",
            destination_ip=None,
            destination_port=None,
            protocol=None,
            dns_name="alternate.example",
        ),
        group(
            "d",
            count=2,
            event="broker.decision",
            component="future-broker",
            operation="unknown",
            reason="unknown_operation",
            destination_ip=None,
            destination_port=None,
            protocol=None,
        ),
        group(
            "e",
            count=2,
            event="broker.decision",
            component="diary-sync",
            operation="sync",
            destination_ip=None,
            destination_port=None,
            protocol=None,
        ),
        group("f", decision="observed"),
    ]
    report = build(groups)
    assert report["finding_counts"] == {
        "routine_expected_failures": 2,
        "novel_destinations": 2,
        "repeated_denials": 3,
        "suspicious_broker_activity": 1,
    }
    assert report["findings"]["routine_expected_failures"][0]["count"] == 4
    assert report["findings"]["novel_destinations"][0]["classifications"] == [
        "novel_destinations", "repeated_denials",
    ]
    assert report["findings"]["suspicious_broker_activity"][0]["operation"] == "unknown"


def test_novel_destination_cannot_be_hidden_by_routine_rule() -> None:
    novel = group("b", destination_ip="192.0.2.20")
    expected_value = expected()
    expected_value["routine_expected_failures"].append({
        "match": {"fingerprint": novel["fingerprint"]},
        "reference": "attempted-routine-override",
    })
    with pytest.raises(baseline_tool.BaselineError):
        build([novel], expected_value=expected_value)


def test_novel_destination_remains_visible_with_matching_routine_metadata() -> None:
    novel = group("b", destination_ip="192.0.2.20")
    expected_value = expected()
    expected_value["routine_expected_failures"].append({
        "match": {"destination_ip": "192.0.2.20", "decision": "would_deny"},
        "reference": "too-broad-routine-rule",
    })
    report = build([novel], expected_value=expected_value)
    assert len(report["findings"]["novel_destinations"]) == 1
    assert report["findings"]["routine_expected_failures"] == []


def test_complete_real_shaped_evidence_passes_all_machine_checkable_gates() -> None:
    groups = [
        group("a", count=4),
        group("b", count=2, destination_ip="192.0.2.20"),
        group(
            "d",
            event="broker.decision",
            component="future-broker",
            operation="unknown",
            destination_ip=None,
            destination_port=None,
            protocol=None,
        ),
    ]
    initial = build(groups)
    report = build(groups, review_for(initial))
    assert report["acceptance"] == {
        "gates": {
            "coverage": True,
            "minimum_duration": True,
            "complete_report": True,
            "journal_records_present": True,
            "owner_review": True,
        },
        "evidence_complete": True,
        "live_verification": "not_performed_by_this_tool",
    }
    assert report["owner_review"]["satisfied"] is True


def test_report_hash_is_stable_across_generation_timestamps() -> None:
    first = build([group("a")])
    second_baseline = baseline([group("a")], generated_at="2030-01-01T00:00:00+00:00")
    second = baseline_tool.build_report(
        second_baseline,
        coverage(),
        expected(),
        generated_at=GENERATED + dt.timedelta(days=1),
    )
    assert first["report_sha256"] == second["report_sha256"]
    assert first["generated_at"] != second["generated_at"]


@pytest.mark.parametrize("gate", ["coverage", "minimum_duration", "complete_report", "journal_records_present"])
def test_each_production_gate_independently_blocks_evidence_completion(gate: str) -> None:
    evidence = coverage()
    report_baseline = baseline([group("a")])
    if gate == "coverage":
        del evidence["coverage"]["dns"]
    elif gate == "minimum_duration":
        evidence = coverage(hours=23)
        report_baseline["until"] = evidence["interval"]["ended_at"]
    elif gate == "complete_report":
        report_baseline["truncated"] = True
    else:
        report_baseline["groups"] = []
        report_baseline["event_count"] = 0
        report_baseline["record_count"] = 0
    initial = baseline_tool.build_report(
        report_baseline, evidence, expected(), generated_at=GENERATED,
    )
    review = review_for(initial)
    if gate == "minimum_duration":
        review["reviewed_at"] = (START + dt.timedelta(hours=24)).isoformat()
    report = baseline_tool.build_report(
        report_baseline, evidence, expected(), review, generated_at=GENERATED,
    )
    assert report["acceptance"]["gates"][gate] is False
    assert report["acceptance"]["evidence_complete"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"report_sha256": "0" * 64},
        {"expected_flows_sha256": "0" * 64},
        {"expected_flow_classifications_reviewed": False},
        {"acknowledged_fingerprints": []},
        {"acknowledged_fingerprints": ["0" * 64]},
        {"reviewed_at": (START + dt.timedelta(hours=1)).isoformat()},
    ],
)
def test_stale_incomplete_or_early_owner_review_fails(change: dict) -> None:
    groups = [group("b", destination_ip="192.0.2.20")]
    initial = build(groups)
    report = build(groups, review_for(initial, **change))
    assert report["owner_review"]["satisfied"] is False
    assert report["acceptance"]["gates"]["owner_review"] is False


def test_missing_owner_review_lists_required_acknowledgements() -> None:
    report = build([group("b", destination_ip="192.0.2.20")])
    assert report["owner_review"]["provided"] is False
    assert report["owner_review"]["missing_fingerprint_acknowledgements"] == ["b" * 64]
    assert report["acceptance"]["evidence_complete"] is False


def test_out_of_interval_coverage_evidence_is_rejected() -> None:
    evidence = coverage()
    evidence["coverage"]["dns"][0]["observed_at"] = (START - dt.timedelta(seconds=1)).isoformat()
    with pytest.raises(baseline_tool.BaselineError, match="outside the production interval"):
        baseline_tool.load_coverage(evidence)


def test_interval_requires_passing_evidence_at_both_boundaries() -> None:
    evidence = coverage()
    del evidence["interval"]["evidence"][0]
    with pytest.raises(baseline_tool.BaselineError, match="both boundaries"):
        baseline_tool.load_coverage(evidence)


def test_unknown_coverage_and_non_passing_evidence_are_rejected() -> None:
    unknown = coverage()
    unknown["coverage"]["unexpected"] = []
    with pytest.raises(baseline_tool.BaselineError):
        baseline_tool.load_coverage(unknown)
    failed = coverage()
    failed["coverage"]["dns"][0]["result"] = "failed"
    with pytest.raises(baseline_tool.BaselineError, match="not passing"):
        baseline_tool.load_coverage(failed)


def test_duplicate_json_fields_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(baseline_tool.BaselineError, match="duplicate JSON"):
        baseline_tool.load_json(str(path))


def test_invalid_expected_match_is_rejected() -> None:
    value = expected()
    value["expected_destinations"][0]["match"]["destination_ip"] = "not-an-ip"
    with pytest.raises(baseline_tool.BaselineError, match="invalid destination_ip"):
        baseline_tool.load_expected(value)


def test_text_report_names_missing_coverage_and_findings() -> None:
    evidence = coverage()
    del evidence["coverage"]["boot"]
    report = baseline_tool.build_report(
        baseline([group("b", destination_ip="192.0.2.20")]),
        evidence,
        expected(),
        generated_at=GENERATED,
    )
    text = baseline_tool.format_report(report)
    assert "missing_coverage=boot" in text
    assert "novel_destinations: groups=1" in text
    assert "live_verification=not_performed_by_this_tool" in text


def test_cli_uses_real_isolated_audit_socket_and_exact_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timestamp = int((START + dt.timedelta(hours=1)).timestamp() * 1_000_000)
    journal_entry = {
        "DIARY_AUDIT_SCHEMA_VERSION": "1",
        "DIARY_AUDIT_FINGERPRINT": "a" * 64,
        "DIARY_AUDIT_EVENT": "network.flow",
        "DIARY_AUDIT_COMPONENT": "nftables",
        "DIARY_AUDIT_OPERATION": "connect",
        "DIARY_AUDIT_DECISION": "would_deny",
        "DIARY_AUDIT_LOCK_STATE": "not_applicable",
        "DIARY_AUDIT_REASON": "audit_only",
        "DIARY_AUDIT_DESTINATION_IP": "192.0.2.10",
        "DIARY_AUDIT_DESTINATION_PORT": "443",
        "DIARY_AUDIT_PROTOCOL": "tcp",
        "DIARY_AUDIT_CALLER_UID": "1001",
        "DIARY_AUDIT_NEW_COUNT": "1",
        "DIARY_AUDIT_FIRST_SEEN_US": str(timestamp),
        "DIARY_AUDIT_LAST_SEEN_US": str(timestamp),
        "__REALTIME_TIMESTAMP": str(timestamp),
        "MESSAGE": "private content must not reach production report",
    }
    store = audit.DedupStore(str(tmp_path / "dedup.json"))
    service = audit.AuditService(
        store,
        audit.JournalWriter(str(tmp_path / "unused-journal.sock")),
        journal_reader=lambda _namespace, since, until, _limit: (
            [journal_entry] if since == START and until == END else []
        ),
    )
    server = audit.AuditDaemon(
        str(tmp_path / "audit.sock"), service, grp.getgrgid(os.getegid()).gr_name,
    )
    monkeypatch.setattr(server, "authorized", lambda _peer: True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(coverage()), encoding="utf-8")
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(expected()), encoding="utf-8")
    try:
        result = baseline_tool.main([
            "--socket", str(tmp_path / "audit.sock"),
            "--evidence", str(evidence_path),
            "--expected", str(expected_path),
            "--json",
        ])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline"]["event_count"] == 1
    assert report["duration"]["seconds"] == 25 * 3600
    assert report["coverage"]["satisfied"] is True
    assert report["finding_counts"]["routine_expected_failures"] == 1
    assert report["acceptance"]["gates"]["owner_review"] is False
    assert report["acceptance"]["evidence_complete"] is False
    assert "private content" not in json.dumps(report)
