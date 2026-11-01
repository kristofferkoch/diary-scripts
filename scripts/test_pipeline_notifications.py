"""Adversarial, frequency, digest, and restart tests for PIPE-04."""
from __future__ import annotations

import concurrent.futures
import unicodedata
from pathlib import Path

import pytest

from scripts.pipeline_notifications import (
    FIXED_ENVELOPE,
    FIXED_RECIPIENT,
    MAX_DEADLINE,
    MAX_REVIEW_REFERENCE,
    MAX_SUMMARY,
    NotificationRenderer,
    TriageResultError,
    sanitize_field,
    validate_triage_result,
)


def event_id(number: int) -> str:
    return f"evt_{number:064x}"


def result(number: int = 1, **overrides) -> dict:
    value = {
        "event_id": event_id(number),
        "urgency": "high",
        "summary": "Ordinary summary",
        "deadline": "2026-11-02",
        "review_reference": "local-review-1",
    }
    value.update(overrides)
    return value


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "pipeline-state" / "notifications.sqlite3"


def renderer(state_path: Path, **kwargs) -> NotificationRenderer:
    return NotificationRenderer(state_path, clock=lambda: 1000.0, **kwargs)


def test_fixed_template_and_envelope_are_not_model_controlled(state_path: Path):
    decision = renderer(state_path).submit(result())
    assert decision.status == "notified"
    notification = decision.notification
    assert notification is not None
    assert notification.recipient == FIXED_RECIPIENT == "note-to-self"
    assert notification.envelope == FIXED_ENVELOPE == "diary-pipeline-v1"
    assert notification.body.startswith("Diary notification\nEvent: " + event_id(1))
    assert set(notification.as_dict()) == {
        "schema_version", "notification_id", "kind", "recipient", "envelope", "body",
        "event_ids", "created_at",
    }
    assert "http" not in notification.body


@pytest.mark.parametrize("field", ["recipient", "envelope", "url", "links", "extra"])
def test_model_cannot_add_envelope_or_link_fields(field: str):
    candidate = result()
    candidate[field] = "attacker-controlled"
    with pytest.raises(TriageResultError, match="strict schema"):
        validate_triage_result(candidate)


@pytest.mark.parametrize(
    "mutation",
    [
        {"event_id": "../other"},
        {"event_id": None},
        {"urgency": "HIGH"},
        {"urgency": "high\nrecipient: other"},
        {"urgency": 1},
        {"summary": None},
        {"deadline": 1},
        {"review_reference": []},
    ],
)
def test_strict_result_types_and_event_id(mutation: dict):
    with pytest.raises(TriageResultError):
        validate_triage_result(result(**mutation))


@pytest.mark.parametrize(
    "hostile",
    [
        "https://example.com/steal?x=1",
        "HTTP : //example.com/steal",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/html,hello",
        "mailto:person@example.com",
        "www.example.com/path",
        "example.com/path?q=1",
        "person@example.com",
        "127.0.0.1:8080/private",
        "[click](https://example.com)",
        "<a href='https://example.com'>click</a>",
        "h\u200bttps://exa\u200bmple.com",
        "&#104;ttps://example.com",
    ],
)
def test_urls_and_obfuscated_urls_are_removed(hostile: str):
    cleaned = sanitize_field(hostile, limit=MAX_SUMMARY)
    assert "example.com" not in cleaned.lower()
    assert "127.0.0.1" not in cleaned
    assert "javascript:" not in cleaned.lower()
    assert "data:" not in cleaned.lower()
    assert "mailto:" not in cleaned.lower()
    if not hostile.startswith("<a"):
        assert "[link removed]" in cleaned


@pytest.mark.parametrize(
    ("hostile", "forbidden"),
    [
        ("//[::1]/admin", "::1"),
        ("//[2001:DB8::1]:8080/admin", "2001"),
        ("https://[::1]/admin", "::1"),
        ("//example.com/admin", "example.com"),
        ("例子.测试/path", "例子"),
        ("例子。测试/path", "例子"),
        ("example。com/path", "example"),
        ("example．com/path", "example"),
        ("example｡com/path", "example"),
        ("bücher.de/path", "bücher"),
        ("xn--fsqu00a.xn--0zwm56d/path", "xn--"),
    ],
)
def test_international_and_protocol_relative_urls_are_removed(hostile: str, forbidden: str):
    cleaned = sanitize_field(hostile, limit=MAX_SUMMARY)
    assert forbidden not in cleaned
    assert "[link removed]" in cleaned


@pytest.mark.parametrize(
    "ordinary",
    [
        "Use // as a separator", "你好。世界", "你好.世界", "a。b", "Version 1。2",
        "1.2", "path/to/file",
    ],
)
def test_url_detection_preserves_ordinary_text(ordinary: str):
    assert sanitize_field(ordinary, limit=MAX_SUMMARY) == ordinary


def test_oversized_markdown_constructs_are_reduced_to_plain_text():
    assert sanitize_field("[safe](" + "d" * 2049 + ")", limit=MAX_SUMMARY) == "safe"
    assert sanitize_field("[label][" + "r" * 201 + "]", limit=MAX_SUMMARY) == "label"
    long_label = sanitize_field("[" + "l" * 1001 + "](destination)", limit=MAX_SUMMARY)
    assert len(long_label) <= MAX_SUMMARY
    assert not any(char in long_label for char in "[]()")


def test_oversized_html_tags_are_removed_without_retaining_delimiters():
    cleaned = sanitize_field(
        "<" + "a" * 1025 + ">safe</" + "b" * 1025 + ">", limit=MAX_SUMMARY,
    )
    assert cleaned == "safe"
    assert "<" not in cleaned and ">" not in cleaned


@pytest.mark.parametrize(
    "hostile",
    [
        "normal\x1b[31mred\x1b[0m",
        "title\x1b]0;fake prompt\x07done",
        "c1\x9b31mred",
        "line1\nline2\r\nline3",
        "nul\x00bell\x07escape\x1b",
    ],
)
def test_terminal_controls_and_multiline_output_are_neutralized(hostile: str):
    cleaned = sanitize_field(hostile, limit=MAX_SUMMARY)
    assert "\n" not in cleaned and "\r" not in cleaned
    assert all(unicodedata.category(char) not in {"Cc", "Cf", "Cs"} for char in cleaned)
    assert "\x1b" not in cleaned and "\x9b" not in cleaned
    assert "[31m" not in cleaned


def test_bidi_zero_width_and_private_use_are_removed():
    cleaned = sanitize_field(
        "safe\u202etxet\u2066hidden\u2069\u200bzero\ufeffwidth\ue000private",
        limit=MAX_SUMMARY,
    )
    assert cleaned == "safetxethiddenzerowidthprivate"
    assert all(unicodedata.category(char) not in {"Cf", "Co"} for char in cleaned)


def test_misleading_markup_becomes_plain_single_line_text():
    cleaned = sanitize_field(
        "<b>**Bold**</b> `code` ~~strike~~ [label][ref]\\_escaped\\_ "
        "#heading >quote ＦＵＬＬ",
        limit=MAX_SUMMARY,
    )
    assert cleaned == "Bold code strike label escaped heading quote FULL"
    assert not any(char in cleaned for char in "<>*_~#>`\\")


def test_all_model_text_fields_are_sanitized_and_capped(state_path: Path):
    decision = renderer(state_path).submit(result(
        summary="**" + "s" * (MAX_SUMMARY + 100) + " https://example.com",
        deadline="d" * (MAX_DEADLINE + 100) + "\nforged: yes",
        review_reference="r" * (MAX_REVIEW_REFERENCE + 100) + "\u202eevil",
    ))
    notification = decision.notification
    assert notification is not None
    body = notification.body
    summary_line = next(line for line in body.splitlines() if line.startswith("Summary: "))
    deadline_line = next(line for line in body.splitlines() if line.startswith("Deadline: "))
    review_line = next(line for line in body.splitlines() if line.startswith("Review: "))
    assert len(summary_line.removeprefix("Summary: ")) <= MAX_SUMMARY
    assert len(deadline_line.removeprefix("Deadline: ")) <= MAX_DEADLINE
    assert len(review_line.removeprefix("Review: ")) <= MAX_REVIEW_REFERENCE
    assert "forged" not in body and "https" not in body and "\u202e" not in body
    assert "**" not in body


def test_empty_sanitized_fields_have_fixed_placeholders(state_path: Path):
    notification = renderer(state_path).submit(result(
        summary="https://example.com", deadline=None, review_reference="\u200b",
    )).notification
    assert notification is not None
    assert "Summary: [link removed]" in notification.body
    assert "Deadline: -" in notification.body
    assert "Review: -" in notification.body


def test_low_priority_stays_local_until_explicit_digest(state_path: Path):
    pipe = renderer(state_path)
    first = pipe.submit(result(1, urgency="low", summary="low one"), now=1.0)
    second = pipe.submit(result(2, urgency="low", summary="low two"), now=2.0)
    assert first.status == second.status == "pending_digest"
    assert first.notification is second.notification is None
    assert [entry.status for entry in pipe.list_review()] == ["pending_digest"] * 2

    digest = pipe.render_digest(now=3.0)
    assert digest is not None
    assert digest.kind == "digest"
    assert digest.event_ids == (event_id(1), event_id(2))
    assert digest.recipient == FIXED_RECIPIENT and digest.envelope == FIXED_ENVELOPE
    assert digest.body.startswith("Diary digest\nLow-priority events: 2")
    assert "low one" in digest.body and "low two" in digest.body
    assert {entry.status for entry in pipe.list_review()} == {"digested"}
    assert pipe.render_digest(now=4.0) is None


def test_digest_batch_limit_and_per_event_deduplication(state_path: Path):
    pipe = renderer(state_path, aggregate_limit=3, digest_max_events=2)
    for number in range(1, 4):
        pipe.submit(result(number, urgency="low"), now=float(number))
    first = pipe.render_digest(now=4.0)
    second = pipe.render_digest(now=5.0)
    assert first is not None and second is not None
    assert first.event_ids == (event_id(1), event_id(2))
    assert second.event_ids == (event_id(3),)
    assert pipe.render_digest(now=6.0) is None
    duplicate = pipe.submit(result(1, urgency="high", summary="changed"), now=7.0)
    assert duplicate.status == "duplicate" and duplicate.notification is None


def test_per_event_limit_survives_restart_and_changed_model_output(state_path: Path):
    first = renderer(state_path).submit(result(summary="original"), now=10.0)
    assert first.status == "notified"
    restarted = renderer(state_path)
    for urgency in ("low", "high"):
        duplicate = restarted.submit(result(urgency=urgency, summary="changed"), now=20.0)
        assert duplicate.status == "duplicate" and duplicate.notification is None
    review = restarted.get_review(event_id(1))
    assert review is not None
    assert review.result["summary"] == "original"
    assert review.status == "notified"


def test_aggregate_limit_suppresses_without_digest_or_replay(state_path: Path):
    pipe = renderer(state_path, aggregate_limit=2, aggregate_window=60)
    assert pipe.submit(result(1), now=1.0).status == "notified"
    assert pipe.submit(result(2), now=2.0).status == "notified"
    suppressed = pipe.submit(result(3, summary="keep me locally"), now=3.0)
    assert suppressed.status == "rate_limited" and suppressed.notification is None
    review = pipe.get_review(event_id(3))
    assert review is not None
    assert review.status == "rate_limited" and review.result["summary"] == "keep me locally"

    restarted = renderer(state_path, aggregate_limit=2, aggregate_window=60)
    duplicate = restarted.submit(result(3), now=1000.0)
    assert duplicate.status == "duplicate" and duplicate.notification is None
    assert restarted.render_digest(now=1000.0) is None
    review = restarted.get_review(event_id(3))
    assert review is not None and review.status == "rate_limited"


def test_aggregate_window_persists_across_restart_and_expires(state_path: Path):
    assert renderer(state_path, aggregate_limit=1, aggregate_window=60).submit(
        result(1), now=10.0,
    ).status == "notified"
    restarted = renderer(state_path, aggregate_limit=1, aggregate_window=60)
    assert restarted.submit(result(2), now=20.0).status == "rate_limited"
    assert restarted.submit(result(3), now=70.1).status == "notified"


def test_rate_limited_digest_keeps_low_events_pending_without_replay(state_path: Path):
    pipe = renderer(state_path, aggregate_limit=1, aggregate_window=60)
    assert pipe.submit(result(1), now=1.0).status == "notified"
    assert pipe.submit(result(2, urgency="low"), now=2.0).status == "pending_digest"
    assert pipe.render_digest(now=3.0) is None
    review = pipe.get_review(event_id(2))
    assert review is not None and review.status == "pending_digest"
    digest = pipe.render_digest(now=62.0)
    assert digest is not None and digest.event_ids == (event_id(2),)
    assert pipe.render_digest(now=63.0) is None


def test_concurrent_frequency_limit_is_exact(state_path: Path):
    pipe = renderer(state_path, aggregate_limit=3, aggregate_window=60)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        decisions = list(pool.map(
            lambda number: pipe.submit(result(number), now=10.0), range(1, 13),
        ))
    assert sum(decision.status == "notified" for decision in decisions) == 3
    assert sum(decision.status == "rate_limited" for decision in decisions) == 9
    assert len(pipe.list_review()) == 12


def test_concurrent_duplicate_event_renders_at_most_once(state_path: Path):
    pipe = renderer(state_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: pipe.submit(result(), now=10.0), range(16)))
    assert sum(decision.status == "notified" for decision in decisions) == 1
    assert sum(decision.status == "duplicate" for decision in decisions) == 15


def test_state_files_are_isolated(tmp_path: Path):
    first = NotificationRenderer(tmp_path / "one" / "state.sqlite3", clock=lambda: 1.0)
    second = NotificationRenderer(tmp_path / "two" / "state.sqlite3", clock=lambda: 1.0)
    assert first.submit(result()).status == "notified"
    assert second.submit(result()).status == "notified"
    first_review = first.get_review(event_id(1))
    second_review = second.get_review(event_id(1))
    assert first_review is not None and first_review.status == "notified"
    assert second_review is not None and second_review.status == "notified"
    assert first.submit(result(2)).status == "notified"
    assert second.get_review(event_id(2)) is None
