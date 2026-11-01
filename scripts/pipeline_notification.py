"""Strict transport validation for rendered PIPE-04 Notification records."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, cast

NOTIFICATION_SCHEMA_VERSION = 1
FIXED_RECIPIENT = "note-to-self"
FIXED_ENVELOPE = "diary-pipeline-v1"
NOTIFICATION_FIELDS = frozenset({
    "schema_version", "notification_id", "kind", "recipient", "envelope", "body",
    "event_ids", "created_at",
})
EVENT_ID_RE = re.compile(r"evt_[0-9a-f]{64}\Z")
URGENCY_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
MAX_BODY = 4000
MAX_DIGEST_EVENTS = 20
MAX_SUMMARY = 500
MAX_DEADLINE = 80
MAX_REVIEW_REFERENCE = 120

_URL_RE = re.compile(
    r"(?i)(?:[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,63}"
    r"|\b(?:(?:https?|ftp|file|data|javascript|mailto|tel|signal)\s*:"
    r"|[a-z][a-z0-9+.-]{1,31}\s*:\s*//)"
    r"|\bwww\."
    r"|(?<![\w:/])//(?:\[[^\]\s<>]+\]|[^\s/:<>]+)"
    r"|(?<![\w@.-])(?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+[a-z]{2,63}"
    r"|(?<![\d.])(?:\d{1,3}\.){3}\d{1,3})",
)
_HTML_TAG_RE = re.compile(r"</?[^<>]*>")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)|!?\[[^\]]*\]\[[^]]*\]")


def _safe_rendered_text(value: object, field: str, maximum: int) -> str:
    """Check rendered text without normalizing, stripping, or rewriting it."""
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field} is not a valid rendered field")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co"} for char in value):
        raise ValueError(f"{field} contains control or formatting characters")
    if any(char in value for char in "`*_~#>\\"):
        raise ValueError(f"{field} contains text formatting")
    if _URL_RE.search(value) or _HTML_TAG_RE.search(value) or _MARKDOWN_LINK_RE.search(value):
        raise ValueError(f"{field} contains a link or markup")
    return value


def _line_field(line: str, prefix: str, field: str, maximum: int) -> str:
    if not line.startswith(prefix):
        raise ValueError(f"notification body has an invalid {field} line")
    return _safe_rendered_text(line.removeprefix(prefix), field, maximum)


def _validate_immediate_body(body: str, event_ids: list[str]) -> None:
    lines = body.split("\n")
    if len(lines) != 6 or lines[0] != "Diary notification":
        raise ValueError("immediate notification body does not match the fixed template")
    if lines[1] != f"Event: {event_ids[0]}":
        raise ValueError("immediate notification event ID does not match event_ids")
    if not lines[2].startswith("Urgency: ") \
            or not URGENCY_RE.fullmatch(lines[2].removeprefix("Urgency: ")):
        raise ValueError("urgency is not a rendered machine token")
    _line_field(lines[3], "Summary: ", "summary", MAX_SUMMARY)
    _line_field(lines[4], "Deadline: ", "deadline", MAX_DEADLINE)
    _line_field(lines[5], "Review: ", "review_reference", MAX_REVIEW_REFERENCE)


def _validate_digest_body(body: str, event_ids: list[str]) -> None:
    sections = body.split("\n\n")
    if len(sections) != len(event_ids) + 1:
        raise ValueError("digest notification sections do not match event_ids")
    if sections[0] != f"Diary digest\nLow-priority events: {len(event_ids)}":
        raise ValueError("digest notification header does not match the fixed template")
    for section, event_id in zip(sections[1:], event_ids, strict=True):
        lines = section.split("\n")
        if len(lines) != 4 or lines[0] != f"Event: {event_id}":
            raise ValueError("digest notification event IDs do not match event_ids")
        _line_field(lines[1], "Summary: ", "summary", MAX_SUMMARY)
        _line_field(lines[2], "Deadline: ", "deadline", MAX_DEADLINE)
        _line_field(lines[3], "Review: ", "review_reference", MAX_REVIEW_REFERENCE)


def _expected_notification_id(kind: str, event_ids: list[str]) -> str:
    identity = json.dumps([kind, event_ids], separators=(",", ":")).encode("ascii")
    return "ntf_" + hashlib.sha256(identity).hexdigest()


def validate_notification(value: object) -> dict[str, Any]:
    """Validate one exact Notification.as_dict() record and leave body unchanged."""
    if not isinstance(value, dict) or set(value) != NOTIFICATION_FIELDS:
        raise ValueError("notification must contain exactly the eight contract fields")
    value = cast(dict[str, Any], value)
    if type(value["schema_version"]) is not int \
            or value["schema_version"] != NOTIFICATION_SCHEMA_VERSION:
        raise ValueError("unsupported notification schema_version")
    kind = value["kind"]
    if kind not in {"immediate", "digest"}:
        raise ValueError("notification kind must be immediate or digest")
    if value["recipient"] != FIXED_RECIPIENT:
        raise ValueError("notification recipient must be note-to-self")
    if value["envelope"] != FIXED_ENVELOPE:
        raise ValueError("notification envelope must be diary-pipeline-v1")
    event_ids = value["event_ids"]
    if not isinstance(event_ids, list) or not event_ids:
        raise ValueError("event_ids must be a non-empty list")
    if kind == "immediate" and len(event_ids) != 1:
        raise ValueError("immediate notifications require exactly one event ID")
    if kind == "digest" and len(event_ids) > MAX_DIGEST_EVENTS:
        raise ValueError("digest contains too many event IDs")
    if any(not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id)
           for event_id in event_ids):
        raise ValueError("event_ids contains an invalid event ID")
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("event_ids contains duplicates")
    if value["notification_id"] != _expected_notification_id(kind, event_ids):
        raise ValueError("notification_id is not deterministic for kind and event_ids")
    created_at = value["created_at"]
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool) \
            or not math.isfinite(created_at) or created_at < 0:
        raise ValueError("created_at must be a finite, non-negative timestamp")
    body = value["body"]
    if not isinstance(body, str) or not body or len(body) > MAX_BODY:
        raise ValueError("notification body is empty or too large")
    if kind == "immediate":
        _validate_immediate_body(body, event_ids)
    else:
        _validate_digest_body(body, event_ids)
    return dict(value)
