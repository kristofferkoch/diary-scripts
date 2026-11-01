"""Isolated schema and fixed-envelope tests for the PIPE-04/05 contract."""
from __future__ import annotations

import pytest

from scripts.pipeline_notification import render_envelope, validate_notification


def notification(**changes):
    value = {
        "event_id": "mail:event-123",
        "urgency": "high",
        "summary": "A sanitized summary",
        "deadline": "2026-11-02T12:00:00+00:00",
        "review_reference": "local/review/event-123",
    }
    value.update(changes)
    return value


def test_valid_notification_has_fixed_envelope():
    fields, message = render_envelope(notification())
    assert fields == notification()
    assert message == (
        "Diary triage notification\n"
        "Urgency: high\n"
        "Event: mail:event-123\n"
        "Summary: A sanitized summary\n"
        "Deadline: 2026-11-02T12:00:00+00:00\n"
        "Review: local/review/event-123"
    )


def test_null_deadline_is_rendered_by_fixed_template():
    fields, message = render_envelope(notification(deadline=None))
    assert fields["deadline"] is None
    assert "Deadline: none" in message


@pytest.mark.parametrize(
    "value",
    [
        {},
        {**notification(), "message": "arbitrary text"},
        {key: item for key, item in notification().items() if key != "summary"},
        [],
        None,
    ],
)
def test_exact_five_field_schema_is_required(value):
    with pytest.raises(ValueError):
        validate_notification(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "not an event id"),
        ("event_id", ""),
        ("urgency", "critical"),
        ("urgency", True),
        ("summary", ""),
        ("summary", "https://example.invalid/private"),
        ("summary", "ftp://example.invalid/private"),
        ("summary", "mailto:private@example.invalid"),
        ("summary", "javascript:alert(1)"),
        ("summary", "www.example.invalid"),
        ("summary", "bad\u202etext"),
        ("summary", "bad\u200btext"),
        ("summary", "bad\ntext"),
        ("summary", "x" * 1001),
        ("deadline", "tomorrow"),
        ("review_reference", "x" * 257),
    ],
)
def test_unsafe_or_invalid_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_notification(notification(**{field: value}))
