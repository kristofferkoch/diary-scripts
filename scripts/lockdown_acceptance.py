#!/usr/bin/python3
"""Repeatable ACT-01 acceptance orchestration with fail-closed evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

PROTOCOL_VERSION = 1
DEFAULT_CATALOG = Path(__file__).with_name("lockdown_acceptance.toml")
LIVE_ACKNOWLEDGEMENT = "ACT-01 LIVE LOCKDOWN, FAILURE INJECTION, REBOOT, AND SIGNAL"
CLEANUP_OPERATION = "suite.relock_cleanup"
MAX_CATALOG_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
REQUIRED_COVERAGE = frozenset({
    "design.network",
    "design.pipeline_isolation",
    "design.pipeline_e2e",
    "design.broker_rejection",
    "design.immutability",
    "design.failure_boot",
    "design.unlock",
    "design.cancellation",
    "design.notification",
    "design.no_replay",
    "act.boot_locked",
    "act.audit_stops_agent",
    "act.audit_stops_pipeline",
    "act.owner_acceptance",
})


class AcceptanceError(ValueError):
    """The catalog, adapter, or evidence cannot prove a safe result."""


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    operation: str
    coverage: tuple[str, ...]
    dependencies: tuple[str, ...]
    required_evidence: tuple[str, ...]
    timeout_seconds: int
    reboot: bool = False


@dataclass(frozen=True)
class Catalog:
    suite_id: str
    cases: tuple[Case, ...]
    raw: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class Adapter(Protocol):
    def call(self, request: dict[str, Any], timeout_seconds: int) -> dict[str, Any]: ...


def _object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{description} must be an object")
    return cast(dict[str, Any], value)


def _string_list(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AcceptanceError(f"{description} must be a non-empty string array")
    if any(not isinstance(item, str) or not RUN_ID_RE.fullmatch(item) for item in value):
        raise AcceptanceError(f"{description} contains an invalid name")
    return tuple(cast(list[str], value))


def load_catalog(path: str | Path = DEFAULT_CATALOG) -> Catalog:
    catalog_path = Path(path)
    try:
        info = catalog_path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise AcceptanceError("catalog must be a regular non-symlink file")
        raw = catalog_path.read_bytes()
    except OSError as exc:
        raise AcceptanceError(f"cannot read catalog: {exc}") from exc
    if not raw or len(raw) > MAX_CATALOG_BYTES:
        raise AcceptanceError("catalog is empty or oversized")
    try:
        document = _object(tomllib.loads(raw.decode("utf-8")), "catalog")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AcceptanceError("catalog is not valid UTF-8 TOML") from exc
    if set(document) != {"schema_version", "suite_id", "cases"}:
        raise AcceptanceError("catalog has unknown or missing top-level fields")
    if document["schema_version"] != PROTOCOL_VERSION or isinstance(document["schema_version"], bool):
        raise AcceptanceError("unsupported catalog schema")
    suite_id = document["suite_id"]
    if not isinstance(suite_id, str) or not NAME_RE.fullmatch(suite_id):
        raise AcceptanceError("invalid suite ID")
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise AcceptanceError("catalog must contain cases")
    cases: list[Case] = []
    seen_ids: set[str] = set()
    seen_operations: set[str] = set()
    coverage: set[str] = set()
    reboot_cases = 0
    for index, item in enumerate(raw_cases):
        raw_case = _object(item, f"case {index}")
        allowed = {
            "id", "title", "operation", "coverage", "dependencies",
            "required_evidence", "timeout_seconds", "reboot",
        }
        if set(raw_case) - allowed or not {
            "id", "title", "operation", "coverage", "dependencies",
            "required_evidence", "timeout_seconds",
        } <= set(raw_case):
            raise AcceptanceError(f"case {index} has unknown or missing fields")
        case_id = raw_case["id"]
        operation = raw_case["operation"]
        title = raw_case["title"]
        if not isinstance(case_id, str) or not NAME_RE.fullmatch(case_id):
            raise AcceptanceError(f"case {index} has an invalid ID")
        if not isinstance(operation, str) or not NAME_RE.fullmatch(operation) or "." not in operation:
            raise AcceptanceError(f"case {case_id} has an invalid adapter operation")
        if operation == CLEANUP_OPERATION:
            raise AcceptanceError("cleanup operation cannot be an acceptance case")
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise AcceptanceError(f"case {case_id} has an invalid title")
        if case_id in seen_ids or operation in seen_operations:
            raise AcceptanceError("case IDs and adapter operations must be unique")
        timeout = raw_case["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 1800:
            raise AcceptanceError(f"case {case_id} has an invalid timeout")
        reboot = raw_case.get("reboot", False)
        if not isinstance(reboot, bool):
            raise AcceptanceError(f"case {case_id} has an invalid reboot flag")
        reboot_cases += int(reboot)
        case_coverage = _string_list(raw_case["coverage"], f"case {case_id} coverage")
        case = Case(
            case_id=case_id,
            title=title,
            operation=operation,
            coverage=case_coverage,
            dependencies=_string_list(
                raw_case["dependencies"], f"case {case_id} dependencies",
            ),
            required_evidence=_string_list(
                raw_case["required_evidence"], f"case {case_id} required evidence",
            ),
            timeout_seconds=timeout,
            reboot=reboot,
        )
        cases.append(case)
        seen_ids.add(case_id)
        seen_operations.add(operation)
        coverage.update(case_coverage)
    if reboot_cases != 1:
        raise AcceptanceError("catalog must contain exactly one reboot case")
    missing = REQUIRED_COVERAGE - coverage
    if missing:
        raise AcceptanceError(f"catalog is missing acceptance coverage: {', '.join(sorted(missing))}")
    return Catalog(suite_id=suite_id, cases=tuple(cases), raw=raw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _atomic_write(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise AcceptanceError(f"evidence file is not regular: {path.name}")
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceError(f"cannot read evidence file {path.name}: {exc}") from exc
    if len(raw) > maximum:
        raise AcceptanceError(f"evidence file is oversized: {path.name}")
    return raw


def validate_response(case: Case, response: object, resume: bool = False) -> dict[str, Any]:
    reply = _object(response, "adapter response")
    if set(reply) - {"protocol", "status", "summary", "evidence", "_process"}:
        raise AcceptanceError("adapter response has unknown fields")
    if set(reply) < {"protocol", "status", "summary", "evidence"}:
        raise AcceptanceError("adapter response is incomplete")
    if reply["protocol"] != PROTOCOL_VERSION or isinstance(reply["protocol"], bool):
        raise AcceptanceError("adapter protocol version mismatch")
    if reply["status"] not in {"pass", "fail", "blocked", "reboot_required"}:
        raise AcceptanceError("adapter returned an invalid status")
    if not isinstance(reply["summary"], str) or not reply["summary"].strip():
        raise AcceptanceError("adapter response needs a summary")
    evidence = _object(reply["evidence"], "adapter evidence")
    if reply["status"] in {"pass", "reboot_required"}:
        if not evidence:
            raise AcceptanceError("successful adapter response has no evidence")
        for key in case.required_evidence:
            if (
                key not in evidence or evidence[key] is None
                or evidence[key] == "" or evidence[key] == [] or evidence[key] == {}
            ):
                raise AcceptanceError(f"successful adapter response lacks evidence field {key}")
    if reply["status"] == "pass":
        true_proofs = {
            "cleanup_complete", "locked_verified", "owner_accepted",
            "fresh_authorization", "report_acknowledged",
        }
        for key in true_proofs & set(case.required_evidence):
            if evidence.get(key) is not True:
                raise AcceptanceError(f"pass requires affirmative evidence field {key}")
        for key in {"blocked_replay_count", "suppressed_replay_count"} & set(case.required_evidence):
            value = evidence.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                raise AcceptanceError(f"pass requires zero evidence field {key}")
    if reply["status"] == "reboot_required":
        if not case.reboot or resume:
            raise AcceptanceError("unexpected reboot suspension")
        if evidence.get("locked_verified") is not True or not isinstance(
            evidence.get("pre_reboot_boot_id"), str,
        ):
            raise AcceptanceError("reboot suspension lacks locked pre-boot evidence")
    if case.reboot and resume and reply["status"] == "pass":
        if (
            evidence.get("locked_verified") is not True
            or evidence.get("boot_changed") is not True
            or not isinstance(evidence.get("post_reboot_boot_id"), str)
        ):
            raise AcceptanceError("boot verification lacks changed-boot locked evidence")
    if reply["status"] == "pass" and case.reboot and not resume:
        raise AcceptanceError("boot case must suspend before verification")
    process = reply.get("_process")
    if process is not None:
        process = _object(process, "adapter process evidence")
        if process.get("returncode") != 0 and reply["status"] == "pass":
            raise AcceptanceError("a failed adapter process cannot report pass")
    return reply


class SubprocessAdapter:
    def __init__(self, executable: str | Path, require_root_owned: bool = True):
        self.executable = Path(executable)
        try:
            info = self.executable.lstat()
        except OSError as exc:
            raise AcceptanceError(f"cannot stat adapter: {exc}") from exc
        if not info.st_mode & stat.S_IXUSR or not stat.S_ISREG(info.st_mode):
            raise AcceptanceError("adapter must be an executable regular non-symlink file")
        if require_root_owned and (info.st_uid != 0 or info.st_mode & 0o022):
            raise AcceptanceError("live adapter must be root-owned and not group/other writable")

    def call(self, request: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [str(self.executable)],
                input=_json_bytes(request),
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            return {
                "protocol": PROTOCOL_VERSION,
                "status": "fail",
                "summary": "adapter timed out",
                "evidence": {"harness_error": "timeout"},
            }
        except OSError as exc:
            return {
                "protocol": PROTOCOL_VERSION,
                "status": "fail",
                "summary": "adapter could not be executed",
                "evidence": {"harness_error": str(exc)},
            }
        process = {
            "returncode": result.returncode,
            "stderr": result.stderr.decode("utf-8", errors="replace")[-65536:],
        }
        if len(result.stdout) > MAX_RESPONSE_BYTES:
            raise AcceptanceError("adapter response is oversized")
        try:
            reply = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("adapter did not return exactly one JSON response") from exc
        reply = _object(reply, "adapter response")
        reply["_process"] = process
        return reply


class EvidenceStore:
    def __init__(self, path: Path, metadata: dict[str, Any]):
        self.path = path
        self.metadata = metadata

    @classmethod
    def create(
        cls, root: str | Path, catalog: Catalog, run_id: str | None = None,
    ) -> EvidenceStore:
        selected = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        if not RUN_ID_RE.fullmatch(selected) or ".." in selected:
            raise AcceptanceError("invalid run ID")
        root_path = Path(root)
        try:
            root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_info = root_path.lstat()
            if not stat.S_ISDIR(root_info.st_mode):
                raise AcceptanceError("evidence root must be a non-symlink directory")
            path = root_path / selected
            path.mkdir(mode=0o700)
            (path / "cases").mkdir(mode=0o700)
        except OSError as exc:
            raise AcceptanceError(f"cannot create evidence directory: {exc}") from exc
        metadata: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "suite_id": catalog.suite_id,
            "catalog_sha256": catalog.digest,
            "run_id": selected,
            "status": "running",
            "created_at": _now(),
            "completed_at": None,
            "next_case_index": 0,
            "records": [],
            "cleanup": None,
            "harness_errors": [],
        }
        store = cls(path, metadata)
        _atomic_write(path / "catalog.toml", catalog.raw)
        store.save()
        return store

    @classmethod
    def load(cls, path: str | Path, catalog: Catalog) -> EvidenceStore:
        run_path = Path(path)
        try:
            if not stat.S_ISDIR(run_path.lstat().st_mode):
                raise AcceptanceError("evidence run must be a non-symlink directory")
        except OSError as exc:
            raise AcceptanceError(f"cannot stat evidence run: {exc}") from exc
        raw = _read_regular(run_path / "run.json", MAX_RUN_BYTES)
        try:
            metadata = _object(json.loads(raw), "run metadata")
        except json.JSONDecodeError as exc:
            raise AcceptanceError("run metadata is invalid JSON") from exc
        required = {
            "protocol", "suite_id", "catalog_sha256", "run_id", "status",
            "created_at", "completed_at", "next_case_index", "records", "cleanup",
            "harness_errors",
        }
        if set(metadata) != required or metadata["protocol"] != PROTOCOL_VERSION:
            raise AcceptanceError("run metadata schema mismatch")
        if metadata["suite_id"] != catalog.suite_id or metadata["catalog_sha256"] != catalog.digest:
            raise AcceptanceError("catalog changed between run and resume/verify")
        if _read_regular(run_path / "catalog.toml", MAX_CATALOG_BYTES) != catalog.raw:
            raise AcceptanceError("saved catalog does not match run metadata")
        if not isinstance(metadata["records"], list) or not isinstance(metadata["harness_errors"], list):
            raise AcceptanceError("run metadata has invalid records")
        return cls(run_path, metadata)

    def save(self) -> None:
        _atomic_write(self.path / "run.json", _json_bytes(self.metadata))

    def record(self, case: Case, request: dict[str, Any], response: dict[str, Any], attempt: int) -> None:
        record = {
            "case_id": case.case_id,
            "operation": case.operation,
            "attempt": attempt,
            "recorded_at": _now(),
            "request": request,
            "response": response,
        }
        raw = _json_bytes(record)
        relative = f"cases/{self.metadata['next_case_index']:03d}-{case.case_id}-attempt{attempt}.json"
        _atomic_write(self.path / relative, raw)
        self.metadata["records"].append({
            "case_id": case.case_id,
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": response["status"],
        })
        if response["status"] != "reboot_required":
            self.metadata["next_case_index"] += 1
        self.save()

    def suspend_for_reboot(self) -> None:
        self.metadata["status"] = "reboot_required"
        self.save()

    def resume(self) -> None:
        if self.metadata["status"] != "reboot_required":
            raise AcceptanceError("only a reboot-suspended run can be resumed")
        self.metadata["status"] = "running"
        self.save()

    def record_cleanup(self, response: dict[str, Any]) -> None:
        raw = _json_bytes({"recorded_at": _now(), "response": response})
        _atomic_write(self.path / "cleanup.json", raw)
        self.metadata["cleanup"] = {
            "path": "cleanup.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": response["status"],
        }

    def finish(self, status: str) -> None:
        if status not in {"passed", "failed"}:
            raise AcceptanceError("invalid final run status")
        self.metadata["status"] = status
        self.metadata["completed_at"] = _now()
        self.save()

    def note_error(self, error: str) -> None:
        self.metadata["harness_errors"].append({"recorded_at": _now(), "error": error})
        self.save()


def _request(catalog: Catalog, store: EvidenceStore, case: Case, phase: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "suite_id": catalog.suite_id,
        "run_id": store.metadata["run_id"],
        "case_id": case.case_id,
        "operation": case.operation,
        "phase": phase,
    }


def _protocol_failure(error: Exception) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "status": "fail",
        "summary": "adapter response failed validation",
        "evidence": {"harness_error": str(error)},
    }


def _cleanup_case() -> Case:
    return Case(
        case_id="suite.cleanup",
        title="Run-scoped cleanup and verified relock",
        operation=CLEANUP_OPERATION,
        coverage=("act.cleanup",),
        dependencies=("ACT-01",),
        required_evidence=("cleanup_complete", "locked_verified", "blocked_replay_count"),
        timeout_seconds=300,
    )


def _cleanup(catalog: Catalog, store: EvidenceStore, adapter: Adapter) -> dict[str, Any]:
    case = _cleanup_case()
    request = _request(catalog, store, case, "cleanup")
    try:
        response = validate_response(case, adapter.call(request, case.timeout_seconds))
    except Exception as exc:
        response = _protocol_failure(exc)
    store.record_cleanup(response)
    return response


def execute_suite(
    catalog: Catalog, store: EvidenceStore, adapter: Adapter, resume: bool = False,
) -> int:
    if resume:
        store.resume()
    rebooting = False
    try:
        for index in range(store.metadata["next_case_index"], len(catalog.cases)):
            case = catalog.cases[index]
            phase = "verify" if resume and case.reboot else "execute"
            request = _request(catalog, store, case, phase)
            try:
                response = validate_response(
                    case, adapter.call(request, case.timeout_seconds), resume=phase == "verify",
                )
            except (AcceptanceError, OSError) as exc:
                response = _protocol_failure(exc)
            attempt = 1 + sum(
                record["case_id"] == case.case_id for record in store.metadata["records"]
            )
            store.record(case, request, response, attempt)
            if response["status"] == "reboot_required":
                store.suspend_for_reboot()
                rebooting = True
                return 3
            if response["status"] != "pass":
                store.finish("failed")
                return 1
            if case.reboot:
                resume = False
    except KeyboardInterrupt:
        store.note_error("interrupted; cleanup/relock attempted")
        _cleanup(catalog, store, adapter)
        store.finish("failed")
        return 130
    except Exception as exc:
        store.note_error(f"harness failure; cleanup/relock attempted: {exc}")
        _cleanup(catalog, store, adapter)
        store.finish("failed")
        raise
    finally:
        if not rebooting and store.metadata["status"] != "passed" and store.metadata["cleanup"] is None:
            _cleanup(catalog, store, adapter)
    cleanup = store.metadata["cleanup"]
    if cleanup is None:
        store.finish("failed")
        return 1
    store.finish("passed" if cleanup["status"] == "pass" else "failed")
    return 0 if cleanup["status"] == "pass" else 1


def verify_evidence(catalog: Catalog, store: EvidenceStore) -> dict[str, Any]:
    if store.metadata["status"] != "passed":
        raise AcceptanceError("run is not marked passed")
    if store.metadata["next_case_index"] != len(catalog.cases):
        raise AcceptanceError("run does not include every acceptance case")
    cases = {case.case_id: case for case in catalog.cases}
    latest: dict[str, dict[str, Any]] = {}
    boot_suspended = False
    for item in store.metadata["records"]:
        item = _object(item, "case record index")
        if set(item) != {"case_id", "path", "sha256", "status"}:
            raise AcceptanceError("case record index has invalid fields")
        case = cases.get(item["case_id"])
        if case is None:
            raise AcceptanceError("case record is outside the catalog")
        relative = item["path"]
        if not isinstance(relative, str):
            raise AcceptanceError("case record path is unsafe")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 2 or path.parts[0] != "cases":
            raise AcceptanceError("case record path is unsafe")
        raw = _read_regular(store.path / path, MAX_RESPONSE_BYTES)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise AcceptanceError("case evidence digest mismatch")
        try:
            record = _object(json.loads(raw), "case evidence")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("case evidence is invalid JSON") from exc
        if set(record) != {"case_id", "operation", "attempt", "recorded_at", "request", "response"}:
            raise AcceptanceError("case evidence has invalid fields")
        if record["case_id"] != case.case_id or record["operation"] != case.operation:
            raise AcceptanceError("case evidence identity mismatch")
        request = _object(record["request"], "case request")
        if set(request) != {"protocol", "suite_id", "run_id", "case_id", "operation", "phase"}:
            raise AcceptanceError("case request has invalid fields")
        if (
            request["protocol"] != PROTOCOL_VERSION
            or request["suite_id"] != catalog.suite_id
            or request["run_id"] != store.metadata["run_id"]
            or request["case_id"] != case.case_id
            or request["operation"] != case.operation
            or request["phase"] not in {"execute", "verify"}
            or (request["phase"] == "verify" and not case.reboot)
        ):
            raise AcceptanceError("case request identity or phase mismatch")
        response = _object(record["response"], "case response")
        validate_response(case, response, resume=request["phase"] == "verify")
        if item["status"] != response["status"]:
            raise AcceptanceError("case record status mismatch")
        if response["status"] == "reboot_required":
            boot_suspended = True
        latest[case.case_id] = response
    for case in catalog.cases:
        if case.case_id not in latest:
            raise AcceptanceError(f"missing final evidence for {case.case_id}")
        if latest[case.case_id]["status"] != "pass":
            raise AcceptanceError(f"case did not pass: {case.case_id}")
    if not boot_suspended:
        raise AcceptanceError("passed run lacks the pre-reboot suspension evidence")
    cleanup_index = _object(store.metadata["cleanup"], "cleanup index")
    if set(cleanup_index) != {"path", "sha256", "status"} or cleanup_index["path"] != "cleanup.json":
        raise AcceptanceError("cleanup index has invalid fields")
    if cleanup_index["status"] != "pass":
        raise AcceptanceError("cleanup/relock did not pass")
    cleanup_raw = _read_regular(store.path / "cleanup.json", MAX_RESPONSE_BYTES)
    if hashlib.sha256(cleanup_raw).hexdigest() != cleanup_index["sha256"]:
        raise AcceptanceError("cleanup evidence digest mismatch")
    try:
        cleanup_record = _object(json.loads(cleanup_raw), "cleanup evidence")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("cleanup evidence is invalid JSON") from exc
    if set(cleanup_record) != {"recorded_at", "response"}:
        raise AcceptanceError("cleanup evidence has invalid fields")
    cleanup_response = validate_response(_cleanup_case(), cleanup_record["response"])
    if cleanup_response["status"] != "pass":
        raise AcceptanceError("cleanup response is not a pass")
    return {
        "suite_id": catalog.suite_id,
        "run_id": store.metadata["run_id"],
        "status": "passed",
        "cases": len(catalog.cases),
        "evidence": str(store.path),
    }


def _catalog_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))


def _live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-live", default="")


def _require_live(args: argparse.Namespace) -> None:
    if not args.execute or args.acknowledge_live != LIVE_ACKNOWLEDGEMENT:
        raise AcceptanceError(
            "live execution requires --execute and --acknowledge-live with the exact documented acknowledgement",
        )
    try:
        info = Path(args.catalog).lstat()
    except OSError as exc:
        raise AcceptanceError(f"cannot stat live catalog: {exc}") from exc
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise AcceptanceError("live catalog must be root-owned and not group/other writable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate catalog coverage without executing anything")
    _catalog_argument(validate)
    plan = sub.add_parser("plan", help="print ordered cases without executing anything")
    _catalog_argument(plan)
    run = sub.add_parser("run", help="start a new live acceptance run")
    _catalog_argument(run)
    _live_arguments(run)
    run.add_argument("--evidence-root", required=True)
    run.add_argument("--run-id")
    resume = sub.add_parser("resume", help="resume the one reboot-suspended case")
    _catalog_argument(resume)
    _live_arguments(resume)
    resume.add_argument("evidence_run")
    verify = sub.add_parser("verify", help="verify a complete evidence run without executing anything")
    _catalog_argument(verify)
    verify.add_argument("evidence_run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        if args.command == "validate":
            print(f"catalog valid: {catalog.suite_id}, {len(catalog.cases)} cases, sha256={catalog.digest}")
            return 0
        if args.command == "plan":
            for index, case in enumerate(catalog.cases, 1):
                marker = " [reboot suspend/resume]" if case.reboot else ""
                print(f"{index:02d} {case.case_id}: {case.operation}{marker}")
            print(f"always on terminal failure/success: {CLEANUP_OPERATION}")
            return 0
        if args.command == "verify":
            store = EvidenceStore.load(args.evidence_run, catalog)
            print(json.dumps(verify_evidence(catalog, store), sort_keys=True))
            return 0
        _require_live(args)
        adapter = SubprocessAdapter(args.adapter)
        if args.command == "run":
            store = EvidenceStore.create(args.evidence_root, catalog, args.run_id)
            result = execute_suite(catalog, store, adapter)
        else:
            store = EvidenceStore.load(args.evidence_run, catalog)
            result = execute_suite(catalog, store, adapter, resume=True)
        print(f"evidence: {store.path}")
        if result == 3:
            print("reboot required: keep the host locked, reboot locally, then resume this exact run")
        return result
    except AcceptanceError as exc:
        print(f"diary-lockdown-acceptance: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
