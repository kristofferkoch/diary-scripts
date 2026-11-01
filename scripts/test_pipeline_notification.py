"""Strict eight-field PIPE-04 transport contract tests for PIPE-05."""
from __future__ import annotations

import hashlib
import json

import pytest

from scripts.pipeline_notification import validate_notification


def event_id(number=1):
    return f"evt_{number:064x}"


def notification_id(kind, event_ids):
    identity = json.dumps([kind, event_ids], separators=(",", ":")).encode("ascii")
    return "ntf_" + hashlib.sha256(identity).hexdigest()


def notification(kind="immediate", **changes):
    ids = [event_id(1)] if kind == "immediate" else [event_id(1), event_id(2)]
    if kind == "immediate":
        body = (
            "Diary notification\n"
            f"Event: {ids[0]}\n"
            "Urgency: high\n"
            "Summary: A sanitized summary\n"
            "Deadline: 2026-11-02\n"
            "Review: local-review-1"
        )
    else:
        body = (
            "Diary digest\nLow-priority events: 2\n\n"
            f"Event: {ids[0]}\nSummary: First\nDeadline: -\nReview: first\n\n"
            f"Event: {ids[1]}\nSummary: Second\nDeadline: -\nReview: second"
        )
    value = {
        "schema_version": 1,
        "notification_id": notification_id(kind, ids),
        "kind": kind,
        "recipient": "note-to-self",
        "envelope": "diary-pipeline-v1",
        "body": body,
        "event_ids": ids,
        "created_at": 1000.0,
    }
    value.update(changes)
    return value


def test_immediate_record_is_validated_without_rewriting_body():
    value = notification()
    assert validate_notification(value) == value


def test_multi_event_digest_record_is_validated_without_rewriting_body():
    value = notification("digest")
    assert validate_notification(value) == value


@pytest.mark.parametrize(
    "value",
    [
        {},
        {**notification(), "extra": "field"},
        {key: item for key, item in notification().items() if key != "body"},
        {
            "event_id": event_id(), "urgency": "high", "summary": "raw model text",
            "deadline": None, "review_reference": "local",
        },
        [],
        None,
    ],
)
def test_exact_eight_field_rendered_schema_is_required(value):
    with pytest.raises(ValueError):
        validate_notification(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"notification_id": "ntf_" + "0" * 64},
        {"kind": "arbitrary"},
        {"recipient": "anyone"},
        {"recipient": "noteToSelf"},
        {"envelope": "other-v1"},
        {"created_at": True},
        {"created_at": -1.0},
        {"created_at": float("inf")},
        {"event_ids": []},
        {"event_ids": (event_id(),)},
        {"event_ids": ["not-an-event"]},
        {"event_ids": [event_id(1), event_id(1)]},
        {"event_ids": [event_id(1), event_id(2)]},
        {"body": "arbitrary text"},
    ],
)
def test_fixed_identity_envelope_ids_and_immediate_shape_are_enforced(changes):
    with pytest.raises(ValueError):
        validate_notification(notification(**changes))


def test_deterministic_id_covers_kind_and_every_digest_event():
    value = notification("digest")
    expected = notification_id("digest", [event_id(1), event_id(2)])
    assert value["notification_id"] == expected
    with pytest.raises(ValueError):
        validate_notification({**value, "event_ids": [event_id(2), event_id(1)]})
    with pytest.raises(ValueError):
        validate_notification({**value, "kind": "immediate"})


@pytest.mark.parametrize(
    "body",
    [
        "Diary notification\nEvent: wrong\nUrgency: high\nSummary: safe\nDeadline: -\nReview: -",
        notification()["body"] + "\nExtra: field",
        notification()["body"].replace("Urgency: high", "Urgency: HIGH"),
        notification()["body"].replace("A sanitized summary", "https://example.invalid"),
        notification()["body"].replace("A sanitized summary", "bad\u202etext"),
        notification()["body"].replace("A sanitized summary", "**formatted**"),
        notification()["body"].replace("2026-11-02", "d" * 81),
        notification()["body"].replace("local-review-1", "r" * 121),
        " " + notification()["body"],
    ],
)
def test_immediate_body_must_match_safe_fixed_template_exactly(body):
    with pytest.raises(ValueError):
        validate_notification(notification(body=body))


@pytest.mark.parametrize(
    "body",
    [
        notification("digest")["body"].replace("events: 2", "events: 1"),
        notification("digest")["body"].replace(f"Event: {event_id(2)}", f"Event: {event_id(3)}"),
        notification("digest")["body"].replace("Summary: Second", "Summary: www.example.invalid"),
        notification("digest")["body"] + "\n\nExtra: section",
    ],
)
def test_digest_body_must_cover_every_event_safely(body):
    with pytest.raises(ValueError):
        validate_notification(notification("digest", body=body))


def test_digest_event_count_is_bounded():
    ids = [event_id(number) for number in range(1, 22)]
    sections = [f"Diary digest\nLow-priority events: {len(ids)}"]
    sections.extend(
        f"Event: {item}\nSummary: safe\nDeadline: -\nReview: -" for item in ids
    )
    value = notification(
        "digest", event_ids=ids, body="\n\n".join(sections),
        notification_id=notification_id("digest", ids),
    )
    with pytest.raises(ValueError):
        validate_notification(value)
