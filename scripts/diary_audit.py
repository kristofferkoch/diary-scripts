#!/usr/bin/python3
"""Trusted, metadata-only audit events and reports for the diary-lock journal.

The root daemon accepts newline-delimited JSON from members of the trusted
writer group. It derives writer identity from SO_PEERCRED, validates a fixed
metadata schema, persists duplicate counters, and writes native journal fields
to the diary-lock namespace. The same peer-authenticated socket serves fixed
baseline and review reports; callers cannot supply journalctl expressions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, cast

SCHEMA_VERSION = 1
STATE_VERSION = 2
DEFAULT_SOCKET = "/run/diary-audit/socket"
DEFAULT_JOURNAL_SOCKET = "/run/systemd/journal.diary-lock/socket"
DEFAULT_STATE = "/var/lib/diary-audit/dedup.json"
DEFAULT_GROUP = "diary-audit"
DEFAULT_JOURNAL_GROUP = "diary-audit-journal"
DEFAULT_NAMESPACE = "diary-lock"
DUPLICATE_WINDOW_US = 60_000_000
STATE_RETENTION_US = 31 * 24 * 60 * 60 * 1_000_000
MAX_REQUEST_BYTES = 32 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_STATE_RECORD_BYTES = 8192
MAX_STATE_RECORDS = 10_000
MAX_PROC_STATUS_BYTES = 64 * 1024
MAX_REPORT_RECORDS = 10_000

REQUIRED_FIELDS = ("event", "component", "operation", "decision", "lock_state")
TOKEN_FIELDS = (
    "event", "component", "operation", "decision", "lock_state", "reason",
    "event_id", "session_id", "tool", "unit", "protocol",
)
OPTIONAL_FIELDS = (
    "reason", "event_id", "session_id", "tool", "unit", "destination_ip",
    "destination_port", "protocol", "dns_name", "caller",
)
SENSITIVE_FIELDS = frozenset({
    "message", "body", "text", "content", "url", "uri", "query", "search",
    "search_query", "command", "cmd", "argv", "cwd", "headers", "payload",
})
ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS) | frozenset(OPTIONAL_FIELDS)
CALLER_FIELDS = frozenset({"pid", "uid", "gid", "unit", "session_id", "tool"})
DECISIONS = frozenset({"allow", "deny", "would_allow", "would_deny", "observed", "error"})
LOCK_STATES = frozenset({"locking", "locked", "attended", "unknown", "not_applicable"})
PROTOCOLS = frozenset({"tcp", "udp", "icmp", "ipv6-icmp"})
ATTENTION_DECISIONS = frozenset({"deny", "would_deny", "error"})
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@=-]{0,127}\Z")
EVENT_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
DURATION_RE = re.compile(r"([1-9][0-9]{0,3})([mhd])\Z")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
EMISSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

FIELD_MAP = {
    "event": "DIARY_AUDIT_EVENT",
    "component": "DIARY_AUDIT_COMPONENT",
    "operation": "DIARY_AUDIT_OPERATION",
    "decision": "DIARY_AUDIT_DECISION",
    "lock_state": "DIARY_AUDIT_LOCK_STATE",
    "reason": "DIARY_AUDIT_REASON",
    "event_id": "DIARY_AUDIT_EVENT_ID",
    "session_id": "DIARY_AUDIT_SESSION_ID",
    "tool": "DIARY_AUDIT_TOOL",
    "unit": "DIARY_AUDIT_UNIT",
    "destination_ip": "DIARY_AUDIT_DESTINATION_IP",
    "destination_port": "DIARY_AUDIT_DESTINATION_PORT",
    "protocol": "DIARY_AUDIT_PROTOCOL",
    "dns_name": "DIARY_AUDIT_DNS_NAME",
}
REPORT_FIELDS = (
    "event", "component", "operation", "decision", "lock_state",
    "destination_ip", "destination_port", "protocol", "dns_name", "caller_uid",
)


class AuditError(ValueError):
    """A request or retained audit state failed closed validation."""


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise AuditError(f"{field} must be a bounded ASCII identifier")
    return value


def _integer(value: object, field: str, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise AuditError(f"{field} must be a non-negative integer")
    return value


def _dns_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise AuditError("dns_name must be a DNS name, not free-form text")
    try:
        ascii_name = value.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise AuditError("dns_name is not valid IDNA") from exc
    if not ascii_name or len(ascii_name) > 253:
        raise AuditError("dns_name is empty or too long")
    labels = ascii_name.split(".")
    if any(
        not label or len(label) > 63
        or not re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?", label)
        for label in labels
    ):
        raise AuditError("dns_name contains an invalid label")
    return ascii_name


def normalize_event(value: object) -> tuple[dict, list[str]]:
    """Validate metadata and return it with the names of omitted content fields."""
    if not isinstance(value, dict):
        raise AuditError("event must be a JSON object")
    value = cast(dict[str, Any], value)
    keys = set(value)
    redacted = sorted(keys & SENSITIVE_FIELDS)
    unknown = keys - ALLOWED_FIELDS - SENSITIVE_FIELDS
    if unknown:
        raise AuditError(f"unknown event fields: {', '.join(sorted(unknown))}")
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise AuditError(f"missing event fields: {', '.join(missing)}")
    clean: dict = {}
    for field in REQUIRED_FIELDS:
        clean[field] = _token(value[field], field)
    if not EVENT_RE.fullmatch(clean["event"]):
        raise AuditError("event must be a dotted lower-case event name")
    if clean["decision"] not in DECISIONS:
        raise AuditError("unsupported decision")
    if clean["lock_state"] not in LOCK_STATES:
        raise AuditError("unsupported lock_state")
    for field in OPTIONAL_FIELDS:
        if field not in value or field == "caller":
            continue
        item = value[field]
        if field in TOKEN_FIELDS:
            clean[field] = _token(item, field)
        elif field == "destination_ip":
            if not isinstance(item, str):
                raise AuditError("destination_ip must be an IP address")
            try:
                clean[field] = str(ipaddress.ip_address(item))
            except ValueError as exc:
                raise AuditError("destination_ip must be an IP address") from exc
        elif field == "destination_port":
            clean[field] = _integer(item, field, 65_535)
            if clean[field] == 0:
                raise AuditError("destination_port must be between 1 and 65535")
        elif field == "dns_name":
            clean[field] = _dns_name(item)
    if "protocol" in clean and clean["protocol"] not in PROTOCOLS:
        raise AuditError("unsupported protocol")
    if "caller" in value:
        caller = value["caller"]
        if not isinstance(caller, dict) or set(caller) - CALLER_FIELDS:
            raise AuditError("caller contains unknown fields")
        caller = cast(dict[str, Any], caller)
        if any(field not in caller for field in ("pid", "uid", "gid")):
            raise AuditError("caller requires kernel-derived pid, uid, and gid")
        clean_caller: dict[str, Any] = {
            field: _integer(caller[field], f"caller.{field}")
            for field in ("pid", "uid", "gid")
        }
        for field in ("unit", "session_id", "tool"):
            if field in caller:
                clean_caller[field] = _token(caller[field], f"caller.{field}")
        clean["caller"] = clean_caller
    return clean, redacted


def peer_credentials(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def journal_group_conflicts(group_name: str = DEFAULT_JOURNAL_GROUP, proc_root: str = "/proc") -> list[tuple[int, int]]:
    """Return non-root PIDs that still hold the daemon-only journal group."""
    gid = grp.getgrnam(group_name).gr_gid
    conflicts: list[tuple[int, int]] = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdigit():
            continue
        try:
            fd = os.open(process / "status", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(MAX_PROC_STATUS_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuditError(f"cannot verify process group revocation for PID {process.name}") from exc
        if len(raw) > MAX_PROC_STATUS_BYTES:
            raise AuditError(f"oversized process status for PID {process.name}")
        try:
            lines = raw.decode("ascii").splitlines()
            uid_line = next(line for line in lines if line.startswith("Uid:"))
            gid_line = next(line for line in lines if line.startswith("Gid:"))
            groups_line = next(line for line in lines if line.startswith("Groups:"))
            effective_uid = int(uid_line.split()[2])
            effective_gid = int(gid_line.split()[2])
            groups = {int(value) for value in groups_line.split()[1:]}
        except (StopIteration, ValueError, IndexError, UnicodeDecodeError) as exc:
            raise AuditError(f"unreadable process identity for PID {process.name}") from exc
        if effective_uid != 0 and (effective_gid == gid or gid in groups):
            conflicts.append((int(process.name), effective_uid))
    return sorted(conflicts)


def _user(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid{uid}"


def event_fingerprint(event: dict) -> str:
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class JournalWriter:
    """Write one native journal datagram to the selected namespace."""

    def __init__(self, path: str = DEFAULT_JOURNAL_SOCKET) -> None:
        self.path = path

    def send(self, fields: dict[str, str]) -> None:
        payload = b"".join(
            f"{key}={value}\n".encode("ascii") for key, value in sorted(fields.items())
        )
        if len(payload) > 24_000:
            raise OSError("audit journal record is too large")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.settimeout(2.0)
            sock.sendto(payload, self.path)
        finally:
            sock.close()


class DedupStore:
    """Bounded JSONL counters with durable, idempotent journal emissions."""

    ENTRY_FIELDS = frozenset({
        "count", "emitted_count", "first_seen_us", "last_seen_us", "last_emit_us", "emission",
    })
    EMISSION_FIELDS = frozenset({
        "id", "count", "new_count", "time_us", "first_seen_us", "last_seen_us",
    })

    def __init__(
        self,
        path: str,
        window_us: int = DUPLICATE_WINDOW_US,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path)
        self.window_us = window_us
        self.clock = clock or (lambda: time.time_ns() // 1000)
        self.lock = threading.Lock()
        self._needs_compaction = False
        self.records: dict[str, dict[str, Any]] = self._load()
        if self._needs_compaction:
            self._save_locked()

    def _check_parent(self) -> None:
        info = self.path.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise AuditError("audit state directory must be private to the daemon")

    def _validate_entry(self, entry: object) -> dict[str, Any]:
        if not isinstance(entry, dict) or set(entry) != self.ENTRY_FIELDS:
            raise AuditError("invalid audit state fields")
        entry = cast(dict[str, Any], entry)
        for field in self.ENTRY_FIELDS - {"emission"}:
            _integer(entry[field], f"audit state {field}", 9_223_372_036_854_775_807)
        if not 0 <= entry["emitted_count"] <= entry["count"]:
            raise AuditError("invalid emitted audit counter")
        emission = entry["emission"]
        if emission is None:
            return entry
        if not isinstance(emission, dict) or set(emission) != self.EMISSION_FIELDS:
            raise AuditError("invalid pending emission")
        emission = cast(dict[str, Any], emission)
        if not isinstance(emission["id"], str) or not EMISSION_ID_RE.fullmatch(emission["id"]):
            raise AuditError("invalid pending emission ID")
        for field in self.EMISSION_FIELDS - {"id"}:
            _integer(emission[field], f"pending emission {field}", 9_223_372_036_854_775_807)
        if not entry["emitted_count"] <= emission["count"] <= entry["count"]:
            raise AuditError("invalid pending emission counter")
        if emission["new_count"] != emission["count"] - entry["emitted_count"] or emission["new_count"] == 0:
            raise AuditError("inconsistent pending emission delta")
        return entry

    def _load(self) -> dict[str, dict[str, Any]]:
        self._check_parent()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise AuditError("audit state must be a regular non-symlink file") from exc
        records: dict[str, dict[str, Any]] = {}
        cutoff = self.clock() - STATE_RETENTION_US
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise AuditError("audit state file has unsafe ownership or mode")
                header = handle.readline(MAX_STATE_RECORD_BYTES + 1)
                if len(header) > MAX_STATE_RECORD_BYTES or not header.endswith(b"\n"):
                    raise AuditError("invalid audit state header")
                value = json.loads(header)
                if value != {"version": STATE_VERSION}:
                    raise AuditError("unsupported audit state")
                while True:
                    raw = handle.readline(MAX_STATE_RECORD_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_STATE_RECORD_BYTES or not raw.endswith(b"\n"):
                        raise AuditError("oversized or incomplete audit state record")
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {"fingerprint", "entry"}:
                        raise AuditError("invalid audit state record")
                    fingerprint = value["fingerprint"]
                    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
                        raise AuditError("invalid audit state fingerprint")
                    if fingerprint in records:
                        raise AuditError("duplicate audit state fingerprint")
                    entry = self._validate_entry(value["entry"])
                    if entry["last_seen_us"] < cutoff:
                        self._needs_compaction = True
                        continue
                    records[fingerprint] = entry
                    if len(records) > MAX_STATE_RECORDS:
                        raise AuditError("active audit state exceeds record capacity")
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditError("unreadable audit state") from exc
        return records

    def _save_locked(self) -> None:
        self._check_parent()
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(json.dumps({"version": STATE_VERSION}, separators=(",", ":")) + "\n")
                for fingerprint, entry in sorted(self.records.items()):
                    handle.write(json.dumps(
                        {"fingerprint": fingerprint, "entry": entry},
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def record(
        self,
        event: dict,
        now_us: int,
        send: Callable[[dict[str, int | str]], None],
    ) -> dict:
        fingerprint = event_fingerprint(event)
        with self.lock:
            cutoff = now_us - STATE_RETENTION_US
            self.records = {
                key: entry for key, entry in self.records.items()
                if entry["last_seen_us"] >= cutoff
            }
            entry = self.records.get(fingerprint)
            if entry is None:
                if len(self.records) >= MAX_STATE_RECORDS:
                    raise AuditError("active audit state reached record capacity")
                entry = {
                    "count": 0,
                    "emitted_count": 0,
                    "first_seen_us": now_us,
                    "last_seen_us": now_us,
                    "last_emit_us": 0,
                    "emission": None,
                }
                self.records[fingerprint] = entry
            entry = cast(dict[str, Any], entry)
            entry["count"] += 1
            entry["last_seen_us"] = now_us
            emission: dict[str, Any] | None = entry["emission"]
            if emission is None:
                should_emit = (
                    entry["last_emit_us"] == 0
                    or now_us >= entry["last_emit_us"] + self.window_us
                )
                if should_emit:
                    new_count = entry["count"] - entry["emitted_count"]
                    emission = {
                        "id": uuid.uuid4().hex,
                        "count": entry["count"],
                        "new_count": new_count,
                        "time_us": now_us,
                        "first_seen_us": entry["first_seen_us"],
                        "last_seen_us": entry["last_seen_us"],
                    }
                    entry["emission"] = emission
            self._save_locked()
            if emission is None:
                return {
                    "fingerprint": fingerprint,
                    "count": entry["count"],
                    "emitted": False,
                }
            counters: dict[str, int | str] = {
                "DIARY_AUDIT_FINGERPRINT": fingerprint,
                "DIARY_AUDIT_EMISSION_ID": emission["id"],
                "DIARY_AUDIT_COUNT": emission["count"],
                "DIARY_AUDIT_NEW_COUNT": emission["new_count"],
                "DIARY_AUDIT_SUPPRESSED_COUNT": emission["new_count"] - 1,
                "DIARY_AUDIT_FIRST_SEEN_US": emission["first_seen_us"],
                "DIARY_AUDIT_LAST_SEEN_US": emission["last_seen_us"],
            }
            send(counters)
            previous_emitted = entry["emitted_count"]
            previous_last_emit = entry["last_emit_us"]
            entry["emitted_count"] = emission["count"]
            entry["last_emit_us"] = emission["time_us"]
            entry["emission"] = None
            try:
                self._save_locked()
            except BaseException:
                entry["emitted_count"] = previous_emitted
                entry["last_emit_us"] = previous_last_emit
                entry["emission"] = emission
                raise
            return {
                "fingerprint": fingerprint,
                "emission_id": emission["id"],
                "count": entry["count"],
                "new_count": emission["new_count"],
                "emitted": True,
            }


def _journal_fields(event: dict, peer: dict[str, int], redacted: list[str]) -> dict[str, str]:
    fields = {
        "MESSAGE": f"diary-lock audit {event['event']} {event['decision']}",
        "PRIORITY": "4" if event["decision"] in ATTENTION_DECISIONS else "6",
        "SYSLOG_IDENTIFIER": "diary-audit",
        "DIARY_AUDIT_SCHEMA_VERSION": str(SCHEMA_VERSION),
        "DIARY_AUDIT_WRITER_PID": str(peer["pid"]),
        "DIARY_AUDIT_WRITER_UID": str(peer["uid"]),
        "DIARY_AUDIT_WRITER_GID": str(peer["gid"]),
        "DIARY_AUDIT_WRITER_USER": _user(peer["uid"]),
    }
    for key, value in event.items():
        if key == "caller":
            continue
        fields[FIELD_MAP[key]] = str(value)
    caller = event.get("caller", peer)
    fields.update({
        "DIARY_AUDIT_CALLER_PID": str(caller["pid"]),
        "DIARY_AUDIT_CALLER_UID": str(caller["uid"]),
        "DIARY_AUDIT_CALLER_GID": str(caller["gid"]),
        "DIARY_AUDIT_CALLER_USER": _user(caller["uid"]),
    })
    for key in ("unit", "session_id", "tool"):
        if key in caller:
            fields[f"DIARY_AUDIT_CALLER_{key.upper()}"] = caller[key]
    if redacted:
        fields["DIARY_AUDIT_REDACTED"] = ",".join(redacted)
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in fields):
        raise AuditError("invalid journal field name")
    if any("\n" in value or "\x00" in value for value in fields.values()):
        raise AuditError("invalid journal field value")
    return fields


def parse_time(value: object, now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(dt.UTC)
    if not isinstance(value, str):
        raise AuditError("report time must be a string")
    if value == "now":
        return current
    match = DURATION_RE.fullmatch(value)
    if match:
        amount = int(match.group(1))
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
        if seconds > 366 * 86400:
            raise AuditError("report range is too large")
        return current - dt.timedelta(seconds=seconds)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("report time must be now, a duration such as 24h, or ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _timestamp(microseconds: int) -> str:
    return dt.datetime.fromtimestamp(microseconds / 1_000_000, dt.UTC).isoformat()


def read_journal(namespace: str, since: dt.datetime, until: dt.datetime, limit: int) -> list[dict]:
    argv = [
        "/usr/bin/journalctl",
        f"--namespace={namespace}",
        "--output=json",
        "--no-pager",
        "--reverse",
        "--lines",
        str(limit),
        "--since",
        f"@{int(since.timestamp())}",
        "--until",
        f"@{int(until.timestamp())}",
        f"DIARY_AUDIT_SCHEMA_VERSION={SCHEMA_VERSION}",
    ]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError("journal query failed") from exc
    if proc.returncode != 0:
        raise AuditError("journal query failed")
    entries = []
    for line in proc.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _report_counter(entry: dict, field: str, default: int = 0) -> int:
    value = entry.get(field, default)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def summarize(entries: list[dict], report: str, since: dt.datetime, until: dt.datetime, limit: int) -> dict:
    groups: dict[str, dict] = {}
    seen_emissions: set[str] = set()
    records = 0
    duplicate_records = 0
    for entry in entries:
        if entry.get("DIARY_AUDIT_SCHEMA_VERSION") not in (str(SCHEMA_VERSION), SCHEMA_VERSION):
            continue
        decision = entry.get("DIARY_AUDIT_DECISION")
        if report == "review" and decision not in ATTENTION_DECISIONS:
            continue
        fingerprint = entry.get("DIARY_AUDIT_FINGERPRINT")
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
            continue
        records += 1
        emission_id = entry.get("DIARY_AUDIT_EMISSION_ID")
        duplicate = False
        if isinstance(emission_id, str) and EMISSION_ID_RE.fullmatch(emission_id):
            duplicate = emission_id in seen_emissions
            seen_emissions.add(emission_id)
        if duplicate:
            duplicate_records += 1
        count = 0 if duplicate else _report_counter(entry, "DIARY_AUDIT_NEW_COUNT")
        timestamp = _report_counter(entry, "__REALTIME_TIMESTAMP")
        first_seen = _report_counter(entry, "DIARY_AUDIT_FIRST_SEEN_US", timestamp)
        last_seen = _report_counter(entry, "DIARY_AUDIT_LAST_SEEN_US", timestamp)
        group = groups.get(fingerprint)
        if group is None:
            group = {
                "fingerprint": fingerprint,
                "count": 0,
                "records": 0,
                "first_seen_us": first_seen,
                "last_seen_us": last_seen,
            }
            for field in REPORT_FIELDS:
                journal_key = f"DIARY_AUDIT_{field.upper()}"
                value = entry.get(journal_key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    group[field] = value
            groups[fingerprint] = group
        group["count"] += count
        group["records"] += 1
        group["first_seen_us"] = min(group["first_seen_us"], first_seen)
        group["last_seen_us"] = max(group["last_seen_us"], last_seen)
    ordered = sorted(groups.values(), key=lambda item: (-item["count"], item["fingerprint"]))
    for group in ordered:
        group["first_seen"] = _timestamp(group.pop("first_seen_us"))
        group["last_seen"] = _timestamp(group.pop("last_seen_us"))
        if report == "review":
            group["repeated"] = group["count"] > 1
    return {
        "schema_version": SCHEMA_VERSION,
        "report": report,
        "namespace": DEFAULT_NAMESPACE,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "event_count": sum(group["count"] for group in ordered),
        "record_count": records,
        "duplicate_record_count": duplicate_records,
        "truncated": len(entries) >= limit,
        "groups": ordered,
    }


class AuditService:
    def __init__(
        self,
        store: DedupStore,
        writer: JournalWriter,
        namespace: str = DEFAULT_NAMESPACE,
        journal_reader: Callable[[str, dt.datetime, dt.datetime, int], list[dict]] = read_journal,
        clock: Callable[[], int] = lambda: time.time_ns() // 1000,
    ) -> None:
        self.store = store
        self.writer = writer
        self.namespace = namespace
        self.journal_reader = journal_reader
        self.clock = clock

    def write(self, value: object, peer: dict[str, int]) -> dict:
        event, redacted = normalize_event(value)
        base = _journal_fields(event, peer, redacted)

        def send(counters: dict[str, int | str]) -> None:
            self.writer.send({**base, **{key: str(item) for key, item in counters.items()}})

        result = self.store.record(event, self.clock(), send)
        result["ok"] = True
        result["redacted"] = redacted
        return result

    def report(self, request: dict) -> dict:
        kind = request["cmd"]
        since = parse_time(request.get("since", "24h"))
        until = parse_time(request.get("until", "now"))
        if since > until:
            raise AuditError("since must not be later than until")
        limit = request.get("limit", MAX_REPORT_RECORDS)
        limit = _integer(limit, "limit", MAX_REPORT_RECORDS)
        if limit == 0:
            raise AuditError("limit must be between 1 and 10000")
        entries = self.journal_reader(self.namespace, since, until, limit)
        return summarize(entries, kind, since, until, limit)

    def handle(self, request: object, peer: dict[str, int]) -> dict:
        if not isinstance(request, dict):
            raise AuditError("unknown audit request")
        request = cast(dict[str, Any], request)
        if request.get("cmd") not in {"write", "baseline", "review"}:
            raise AuditError("unknown audit request")
        cmd = request["cmd"]
        allowed = {"cmd", "event"} if cmd == "write" else {"cmd", "since", "until", "limit"}
        if set(request) - allowed or (cmd == "write" and "event" not in request):
            raise AuditError("invalid audit request fields")
        if cmd == "write":
            return self.write(request["event"], peer)
        result = self.report(request)
        result["ok"] = True
        return result


class AuditRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        conn = self.request
        server = cast(AuditDaemon, self.server)
        conn.settimeout(15.0)
        peer = peer_credentials(conn)
        try:
            if not server.authorized(peer):
                raise AuditError("caller is not a trusted audit writer or reader")
            line = conn.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
            if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise AuditError("audit request is missing, oversized, or incomplete")
            try:
                request = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditError("audit request is not valid JSON") from exc
            reply = server.service.handle(request, peer)
        except (AuditError, OSError) as exc:
            reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
        except OSError:
            pass


class AuditDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, service: AuditService, group: str = DEFAULT_GROUP) -> None:
        self.path = path
        self.service = service
        self.group = group
        parent = Path(path).parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise AuditError("audit socket directory must be private to the daemon")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat_is_socket(info.st_mode) or info.st_uid != os.geteuid():
                raise AuditError("refusing to replace a non-socket or foreign socket")
            os.unlink(path)
        super().__init__(path, AuditRequestHandler)
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, -1, gid)
        os.chmod(path, 0o660)

    def authorized(self, peer: dict[str, int]) -> bool:
        if peer["uid"] == 0:
            return True
        try:
            group = grp.getgrnam(self.group)
            name = pwd.getpwuid(peer["uid"]).pw_name
        except KeyError:
            return False
        if name in {"agent", "diary-pipeline"}:
            return False
        return peer["gid"] == group.gr_gid or name in group.gr_mem

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def stat_is_socket(mode: int) -> bool:
    return mode & 0o170000 == 0o140000


def request(socket_path: str, value: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(40.0)
        sock.connect(socket_path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 16 * 1024 * 1024:
                raise AuditError("audit response is too large")
        reply = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot use trusted audit socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise AuditError(reply.get("error", "audit request failed") if isinstance(reply, dict) else "audit request failed")
    return reply


def format_report(report: dict) -> str:
    lines = [
        f"{report['report']} events={report['event_count']} records={report['record_count']} "
        f"since={report['since']} until={report['until']} truncated={str(report['truncated']).lower()}"
    ]
    for group in report["groups"]:
        dimensions = " ".join(
            f"{field}={group[field]}" for field in REPORT_FIELDS if field in group
        )
        lines.append(f"count={group['count']} records={group['records']} {dimensions}")
    return "\n".join(lines) + "\n"


def _client_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="cmd", required=True)
    write = sub.add_parser("write", help="write one validated JSON audit event")
    write.add_argument("--json", required=True, help="event object, or - to read stdin")
    revocation = sub.add_parser("revocation-check", help="fail if non-root processes retain the journal group")
    revocation.add_argument("--group", default=DEFAULT_JOURNAL_GROUP)
    for name in ("baseline", "review"):
        report = sub.add_parser(name, help=f"produce a fixed {name} report")
        report.add_argument("--since", default="24h")
        report.add_argument("--until", default="now")
        report.add_argument("--limit", type=int, default=MAX_REPORT_RECORDS)
        report.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _client_parser().parse_args(argv)
    try:
        if args.cmd == "revocation-check":
            conflicts = journal_group_conflicts(args.group)
            if conflicts:
                processes = ", ".join(f"PID {pid} (UID {uid})" for pid, uid in conflicts)
                raise AuditError(
                    f"{args.group} is retained by {processes}; stop or restart only those "
                    "services in a controlled window, then rerun the installer"
                )
            print(f"ok: no non-root process retains {args.group}")
        elif args.cmd == "write":
            raw = sys.stdin.read(MAX_REQUEST_BYTES + 1) if args.json == "-" else args.json
            if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise AuditError("event is too large")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AuditError("event is not valid JSON") from exc
            reply = request(args.socket, {"cmd": "write", "event": event})
            print(json.dumps(reply, indent=2, sort_keys=True))
        else:
            reply = request(args.socket, {
                "cmd": args.cmd,
                "since": args.since,
                "until": args.until,
                "limit": args.limit,
            })
            if args.json_output:
                print(json.dumps(reply, indent=2, sort_keys=True))
            else:
                print(format_report(reply), end="")
        return 0
    except AuditError as exc:
        print(f"diary-audit: {exc}", file=sys.stderr)
        return 2


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the root-owned diary-lock audit daemon.")
    parser.add_argument("--listen", default=DEFAULT_SOCKET)
    parser.add_argument("--journal-socket", default=DEFAULT_JOURNAL_SOCKET)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-auditd must run as root")
    try:
        service = AuditService(DedupStore(args.state), JournalWriter(args.journal_socket))
        server = AuditDaemon(args.listen, service, args.group)
    except (AuditError, KeyError, OSError) as exc:
        print(f"diary-auditd: {exc}", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "diary-auditd":
        raise SystemExit(daemon_main())
    raise SystemExit(main())
