"""Strict PIPE-04 notification contract and fixed PIPE-05 text envelope."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, cast

NOTIFICATION_FIELDS = (
    "event_id", "urgency", "summary", "deadline", "review_reference",
)
URGENCY_VALUES = frozenset({"low", "normal", "high"})
EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@=-]{0,127}\Z")
URL_RE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]{1,31}://|\bmailto:|\bjavascript:|\bdata:|\bwww\.)",
    re.IGNORECASE,
)

MAX_SUMMARY_CHARS = 1000
MAX_DEADLINE_CHARS = 64
MAX_REVIEW_REFERENCE_CHARS = 256


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError(f"{field} contains control or formatting characters")
    if URL_RE.search(value):
        raise ValueError(f"{field} contains a URL")
    return value.strip()


def validate_notification(value: object) -> dict[str, Any]:
    """Validate the exact sanitized schema emitted by the PIPE-04 renderer."""
    if not isinstance(value, dict) or set(value) != set(NOTIFICATION_FIELDS):
        raise ValueError("notification must contain exactly the five schema fields")
    value = cast(dict[str, Any], value)
    event_id = _text(value["event_id"], "event_id", 128)
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event_id has an invalid format")
    urgency = _text(value["urgency"], "urgency", 16)
    if urgency not in URGENCY_VALUES:
        raise ValueError("urgency is not supported")
    summary = _text(value["summary"], "summary", MAX_SUMMARY_CHARS)
    deadline_value = value["deadline"]
    if deadline_value is None:
        deadline = None
    else:
        deadline = _text(deadline_value, "deadline", MAX_DEADLINE_CHARS)
        try:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("deadline must be an ISO 8601 timestamp or date") from exc
    review_reference = _text(
        value["review_reference"], "review_reference", MAX_REVIEW_REFERENCE_CHARS,
    )
    return {
        "event_id": event_id,
        "urgency": urgency,
        "summary": summary,
        "deadline": deadline,
        "review_reference": review_reference,
    }


def render_envelope(value: object) -> tuple[dict[str, Any], str]:
    """Return validated fields and the only message text PIPE-05 may send."""
    notification = validate_notification(value)
    lines = [
        "Diary triage notification",
        f"Urgency: {notification['urgency']}",
        f"Event: {notification['event_id']}",
        f"Summary: {notification['summary']}",
        f"Deadline: {notification['deadline'] or 'none'}",
        f"Review: {notification['review_reference']}",
    ]
    return notification, "\n".join(lines)
