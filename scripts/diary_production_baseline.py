#!/usr/bin/python3
"""Build an evidence-bound AUD-05 report from the trusted diary-audit API."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from scripts import diary_audit

SCHEMA_VERSION = 1
MINIMUM_INTERVAL_SECONDS = 24 * 60 * 60
MAX_INPUT_BYTES = 1024 * 1024
MAX_EVIDENCE_RECORDS = 1000
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
ATTENTION_DECISIONS = frozenset({"deny", "would_deny", "error"})
COVERAGE_CHECKS = (
    "normal_destinations",
    "search",
    "fetch",
    "dns",
    "broker_signal",
    "broker_diary_sync",
    "broker_agent_merge",
    "broker_sudo",
    "collector_mail",
    "collector_spond",
    "collector_vigilo",
    "collector_skolemelding",
    "active_turns",
    "restart",
    "boot",
)
FINDING_KINDS = (
    "routine_expected_failures",
    "novel_destinations",
    "repeated_denials",
    "suspicious_broker_activity",
)
MATCH_FIELDS = frozenset(diary_audit.REPORT_FIELDS)


class BaselineError(ValueError):
    """Production baseline input or evidence failed closed validation."""


def _object(
    value: object,
    name: str,
    fields: set[str] | frozenset[str],
    required: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BaselineError(f"{name} must be an object")
    value = dict(value)
    if set(value) - fields or (required is not None and required - set(value)):
        raise BaselineError(f"{name} has missing or unknown fields")
    return value


def _bounded_string(value: object, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise BaselineError(f"{name} must be a bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BaselineError(f"{name} contains control characters")
    return value


def _timestamp(value: object, name: str) -> dt.datetime:
    text = _bounded_string(value, name, 64)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise BaselineError(f"{name} must include a timezone")
    return parsed.astimezone(dt.UTC)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_json(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise BaselineError(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise BaselineError(f"{path} is too large")
    try:
        value = json.loads(raw, object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"{path} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BaselineError(f"{path} must contain a JSON object")
    return {str(key): item for key, item in value.items()}


def _evidence_records(value: object, name: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_RECORDS:
        raise BaselineError(f"{name} must be a bounded evidence list")
    records = []
    for index, item in enumerate(value):
        record = _object(
            item,
            f"{name}[{index}]",
            {"observed_at", "reference", "result"},
            {"observed_at", "reference", "result"},
        )
        if record["result"] != "passed":
            raise BaselineError(f"{name}[{index}] is not passing evidence")
        observed_at = _timestamp(record["observed_at"], f"{name}[{index}].observed_at")
        records.append({
            "observed_at": observed_at.isoformat(),
            "reference": _bounded_string(record["reference"], f"{name}[{index}].reference"),
            "result": "passed",
        })
    return records


def load_coverage(value: object) -> dict[str, Any]:
    evidence = _object(
        value,
        "coverage evidence",
        {"schema_version", "interval", "coverage"},
        {"schema_version", "interval", "coverage"},
    )
    if evidence["schema_version"] != SCHEMA_VERSION or isinstance(evidence["schema_version"], bool):
        raise BaselineError("unsupported coverage evidence schema")
    interval = _object(
        evidence["interval"],
        "interval",
        {"started_at", "ended_at", "evidence"},
        {"started_at", "ended_at", "evidence"},
    )
    started_at = _timestamp(interval["started_at"], "interval.started_at")
    ended_at = _timestamp(interval["ended_at"], "interval.ended_at")
    if started_at >= ended_at:
        raise BaselineError("interval must have positive duration")
    interval_evidence = _evidence_records(interval["evidence"], "interval.evidence")
    coverage = _object(evidence["coverage"], "coverage", set(COVERAGE_CHECKS), set())
    checks = {
        name: _evidence_records(coverage.get(name, []), f"coverage.{name}")
        for name in COVERAGE_CHECKS
    }
    for name, records in (("interval", interval_evidence), *checks.items()):
        for record in records:
            observed_at = _timestamp(record["observed_at"], f"{name} evidence time")
            if not started_at <= observed_at <= ended_at:
                raise BaselineError(f"{name} evidence is outside the production interval")
    boundary_evidence = {
        _timestamp(record["observed_at"], "interval evidence time") for record in interval_evidence
    }
    if not {started_at, ended_at} <= boundary_evidence:
        raise BaselineError("interval requires passing evidence at both boundaries")
    return {
        "schema_version": SCHEMA_VERSION,
        "interval": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "evidence": interval_evidence,
        },
        "coverage": checks,
    }


def _match_value(field: str, value: object) -> str | int:
    if field == "caller_uid":
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
            raise BaselineError("caller_uid match must be a non-negative integer")
        return str(value)
    probe = {
        "event": "network.flow",
        "component": "baseline",
        "operation": "check",
        "decision": "observed",
        "lock_state": "not_applicable",
        field: value,
    }
    try:
        clean, _redacted = diary_audit.normalize_event(probe)
    except diary_audit.AuditError as exc:
        raise BaselineError(f"invalid {field} match: {exc}") from exc
    return clean[field]


def _rules(value: object, name: str, destination: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_RECORDS:
        raise BaselineError(f"{name} must be a bounded rule list")
    rules = []
    for index, item in enumerate(value):
        rule = _object(item, f"{name}[{index}]", {"match", "reference"}, {"match", "reference"})
        match = _object(rule["match"], f"{name}[{index}].match", MATCH_FIELDS, set())
        if not match or (destination and not {"destination_ip", "dns_name"} & set(match)):
            raise BaselineError(f"{name}[{index}] has an empty or non-destination match")
        rules.append({
            "match": {field: _match_value(field, match[field]) for field in sorted(match)},
            "reference": _bounded_string(rule["reference"], f"{name}[{index}].reference"),
        })
    return rules


def load_expected(value: object | None) -> dict[str, Any]:
    if value is None:
        value = {
            "schema_version": SCHEMA_VERSION,
            "expected_destinations": [],
            "routine_expected_failures": [],
        }
    expected = _object(
        value,
        "expected flows",
        {"schema_version", "expected_destinations", "routine_expected_failures"},
        {"schema_version", "expected_destinations", "routine_expected_failures"},
    )
    if expected["schema_version"] != SCHEMA_VERSION or isinstance(expected["schema_version"], bool):
        raise BaselineError("unsupported expected-flow schema")
    return {
        "schema_version": SCHEMA_VERSION,
        "expected_destinations": _rules(expected["expected_destinations"], "expected_destinations", True),
        "routine_expected_failures": _rules(
            expected["routine_expected_failures"], "routine_expected_failures", False,
        ),
    }


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _matches(group: dict[str, Any], rule: dict[str, Any]) -> bool:
    return all(str(group.get(field)) == str(value) for field, value in rule["match"].items())


def _matched_references(group: dict[str, Any], rules: list[dict[str, Any]]) -> list[str]:
    return sorted(rule["reference"] for rule in rules if _matches(group, rule))


def classify_groups(groups: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    findings: dict[str, list[dict[str, Any]]] = {kind: [] for kind in FINDING_KINDS}
    for original in groups:
        if not isinstance(original, dict) or not isinstance(original.get("fingerprint"), str):
            raise BaselineError("baseline contains an invalid group")
        group = copy.deepcopy(original)
        event = str(group.get("event", ""))
        is_destination = event.startswith(("network.", "dns.")) and bool(
            group.get("destination_ip") or group.get("dns_name")
        )
        destination_references = _matched_references(group, expected["expected_destinations"])
        expected_destination = is_destination and bool(destination_references)
        routine_references = _matched_references(group, expected["routine_expected_failures"])
        attention = group.get("decision") in ATTENTION_DECISIONS
        novel = is_destination and not expected_destination
        unknown_broker = event == "broker.decision" and (
            group.get("operation") == "unknown" or group.get("reason") == "unknown_operation"
        )
        routine = attention and not novel and not unknown_broker and bool(
            destination_references or routine_references
        )
        repeated = attention and not routine and group.get("count", 0) > 1
        suspicious_broker = event == "broker.decision" and attention and not routine
        classifications = []
        if routine:
            classifications.append("routine_expected_failures")
        if novel:
            classifications.append("novel_destinations")
        if repeated:
            classifications.append("repeated_denials")
        if suspicious_broker:
            classifications.append("suspicious_broker_activity")
        if not classifications:
            continue
        group["classifications"] = classifications
        group["classification_references"] = sorted(set(destination_references + routine_references))
        for kind in classifications:
            findings[kind].append(copy.deepcopy(group))
    return findings


def _coverage_result(evidence: dict[str, Any]) -> dict[str, Any]:
    checks = {
        name: {"covered": bool(records), "evidence": records}
        for name, records in evidence["coverage"].items()
    }
    covered = sum(check["covered"] for check in checks.values())
    return {
        "checks": checks,
        "covered_count": covered,
        "required_count": len(COVERAGE_CHECKS),
        "satisfied": covered == len(COVERAGE_CHECKS),
    }


def _duration_result(evidence: dict[str, Any]) -> dict[str, Any]:
    interval = evidence["interval"]
    seconds = int((_timestamp(interval["ended_at"], "ended_at") - _timestamp(
        interval["started_at"], "started_at",
    )).total_seconds())
    return {
        **interval,
        "seconds": seconds,
        "minimum_seconds": MINIMUM_INTERVAL_SECONDS,
        "satisfied": seconds >= MINIMUM_INTERVAL_SECONDS,
    }


def _review_result(
    value: object | None,
    report_sha256: str,
    expected_sha256: str,
    required_fingerprints: set[str],
    until: dt.datetime,
    generated_at: dt.datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provided": value is not None,
        "satisfied": False,
        "missing_fingerprint_acknowledgements": sorted(required_fingerprints),
        "unexpected_fingerprint_acknowledgements": [],
    }
    if value is None:
        return result
    review = _object(
        value,
        "owner review",
        {
            "schema_version", "report_sha256", "expected_flows_sha256", "reviewed_at",
            "review_reference", "expected_flow_classifications_reviewed", "acknowledged_fingerprints",
        },
        {
            "schema_version", "report_sha256", "expected_flows_sha256", "reviewed_at",
            "review_reference", "expected_flow_classifications_reviewed", "acknowledged_fingerprints",
        },
    )
    if review["schema_version"] != SCHEMA_VERSION or isinstance(review["schema_version"], bool):
        raise BaselineError("unsupported owner-review schema")
    for field in ("report_sha256", "expected_flows_sha256"):
        if not isinstance(review[field], str) or not FINGERPRINT_RE.fullmatch(review[field]):
            raise BaselineError(f"{field} must be a SHA-256 hex digest")
    reviewed_at = _timestamp(review["reviewed_at"], "owner review.reviewed_at")
    acknowledgements = review["acknowledged_fingerprints"]
    if not isinstance(acknowledgements, list) or any(
        not isinstance(item, str) or not FINGERPRINT_RE.fullmatch(item) for item in acknowledgements
    ) or len(set(acknowledgements)) != len(acknowledgements):
        raise BaselineError("acknowledged_fingerprints must be unique SHA-256 fingerprints")
    acknowledged = set(acknowledgements)
    result.update({
        "reviewed_at": reviewed_at.isoformat(),
        "review_reference": _bounded_string(review["review_reference"], "owner review.review_reference"),
        "report_sha256_match": review["report_sha256"] == report_sha256,
        "expected_flows_sha256_match": review["expected_flows_sha256"] == expected_sha256,
        "expected_flow_classifications_reviewed": review["expected_flow_classifications_reviewed"] is True,
        "reviewed_after_interval": until <= reviewed_at <= generated_at,
        "missing_fingerprint_acknowledgements": sorted(required_fingerprints - acknowledged),
        "unexpected_fingerprint_acknowledgements": sorted(acknowledged - required_fingerprints),
    })
    result["satisfied"] = all((
        result["report_sha256_match"],
        result["expected_flows_sha256_match"],
        result["expected_flow_classifications_reviewed"],
        result["reviewed_after_interval"],
        not result["missing_fingerprint_acknowledgements"],
        not result["unexpected_fingerprint_acknowledgements"],
    ))
    return result


def build_report(
    baseline: dict[str, Any],
    coverage_evidence: object,
    expected_value: object | None = None,
    owner_review: object | None = None,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    evidence = load_coverage(coverage_evidence)
    expected = load_expected(expected_value)
    baseline = copy.deepcopy(_object(baseline, "baseline", set(baseline) if isinstance(baseline, dict) else set()))
    if baseline.get("report") != "baseline" or not isinstance(baseline.get("groups"), list):
        raise BaselineError("trusted API did not return a baseline report")
    interval = evidence["interval"]
    if baseline.get("since") != interval["started_at"] or baseline.get("until") != interval["ended_at"]:
        raise BaselineError("baseline range does not match the production evidence interval")
    generated_at = (generated_at or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    until = _timestamp(interval["ended_at"], "interval.ended_at")
    expected_sha256 = _canonical_hash(expected)
    findings = classify_groups(baseline["groups"], expected)
    required_fingerprints = {
        group["fingerprint"]
        for kind in FINDING_KINDS[1:]
        for group in findings[kind]
    }
    coverage = _coverage_result(evidence)
    duration = _duration_result(evidence)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report": "production_baseline",
        "generated_at": generated_at.isoformat(),
        "baseline": baseline,
        "expected_flows": expected,
        "expected_flows_sha256": expected_sha256,
        "coverage": coverage,
        "duration": duration,
        "findings": findings,
        "finding_counts": {kind: len(groups) for kind, groups in findings.items()},
    }
    reviewable = copy.deepcopy(report)
    reviewable.pop("generated_at")
    reviewable["baseline"].pop("generated_at", None)
    report["report_sha256"] = _canonical_hash(reviewable)
    report["owner_review"] = _review_result(
        owner_review,
        report["report_sha256"],
        expected_sha256,
        required_fingerprints,
        until,
        generated_at,
    )
    truncated = baseline.get("truncated")
    if not isinstance(truncated, bool) or not isinstance(baseline.get("record_count"), int):
        raise BaselineError("baseline lacks completeness metadata")
    gates = {
        "coverage": coverage["satisfied"],
        "minimum_duration": duration["satisfied"],
        "complete_report": not truncated,
        "journal_records_present": baseline["record_count"] > 0,
        "owner_review": report["owner_review"]["satisfied"],
    }
    report["acceptance"] = {
        "gates": gates,
        "evidence_complete": all(gates.values()),
        "live_verification": "not_performed_by_this_tool",
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    duration = report["duration"]
    coverage = report["coverage"]
    acceptance = report["acceptance"]
    lines = [
        f"production-baseline sha256={report['report_sha256']} "
        f"since={duration['started_at']} until={duration['ended_at']} "
        f"duration_seconds={duration['seconds']}",
        f"coverage={coverage['covered_count']}/{coverage['required_count']} "
        f"evidence_complete={str(acceptance['evidence_complete']).lower()} "
        "live_verification=not_performed_by_this_tool",
    ]
    missing = [name for name, check in coverage["checks"].items() if not check["covered"]]
    if missing:
        lines.append("missing_coverage=" + ",".join(missing))
    for kind in FINDING_KINDS:
        lines.append(f"{kind}: groups={len(report['findings'][kind])}")
        for group in report["findings"][kind]:
            dimensions = " ".join(
                f"{field}={group[field]}" for field in diary_audit.REPORT_FIELDS if field in group
            )
            lines.append(f"  count={group['count']} fingerprint={group['fingerprint']} {dimensions}")
    lines.append("gates=" + ",".join(
        f"{name}:{str(satisfied).lower()}" for name, satisfied in acceptance["gates"].items()
    ))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--socket", default=diary_audit.DEFAULT_SOCKET)
    parser.add_argument("--evidence", required=True, help="coverage and production-interval JSON")
    parser.add_argument("--expected", help="owner-supplied expected destinations and routine failures JSON")
    parser.add_argument("--review", help="owner review bound to the report and expected-flow hashes")
    parser.add_argument("--limit", type=int, default=diary_audit.MAX_REPORT_RECORDS)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        evidence = load_coverage(load_json(args.evidence))
        interval = evidence["interval"]
        baseline = diary_audit.request(args.socket, {
            "cmd": "baseline",
            "since": interval["started_at"],
            "until": interval["ended_at"],
            "limit": args.limit,
        })
        report = build_report(
            baseline,
            evidence,
            load_json(args.expected) if args.expected else None,
            load_json(args.review) if args.review else None,
        )
        if args.json_output:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_report(report), end="")
        return 0
    except (BaselineError, diary_audit.AuditError) as exc:
        print(f"diary-production-baseline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
