"""Tests for pr_compose.py. Run with `uv run pytest scripts/`.

Covers pure logic — code-fence stripping, triage rules, thread dedup,
body extraction, query construction, classifier payload shape. The
MLX HTTP call and notmuch subprocesses are stubbed, never live."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default as email_default

import pytest

from scripts import pr_compose
from scripts.pr_compose import (
    BODY_MAX_CHARS,
    MailRow,
    _strip_code_fence,
    classify,
    dedupe_by_thread,
    extract_body,
    format_line,
    resolve_query,
    triage_skip,
)


# ---------- shared helpers ----------

def _row(subject: str = "s", sender: str = "a@b.com",
         body: str = "", tags: tuple[str, ...] = ()) -> MailRow:
    return MailRow(msg_id="m@x", subject=subject, sender=sender,
                   body=body, tags=list(tags))


# ---------- _strip_code_fence ----------

@pytest.mark.parametrize("raw, expected", [
    ('{"a": 1}', '{"a": 1}'),
    ('  {"a": 1}  ', '{"a": 1}'),
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('```JSON\n{"a": 1}\n```', '{"a": 1}'),
])
def test_strip_code_fence(raw: str, expected: str) -> None:
    assert _strip_code_fence(raw) == expected


# ---------- triage_skip ----------

def test_triage_skip_passes_clean_mail() -> None:
    assert triage_skip(_row(sender="astrid@example.com",
                            tags=("inbox", "unread"))) is None


@pytest.mark.parametrize("tag", sorted(pr_compose.SKIP_TAGS))
def test_triage_skip_catches_each_noise_tag(tag: str) -> None:
    reason = triage_skip(_row(tags=("inbox", tag)))
    assert reason is not None
    assert tag in reason


def test_triage_skip_catches_github_notifications_sender() -> None:
    reason = triage_skip(_row(sender="GitHub <notifications@github.com>"))
    assert reason is not None
    assert "notifications@github.com" in reason


def test_triage_skip_catches_github_noreply_sender() -> None:
    # Real example from the smoke test: GitHub puts the human inviter as
    # display name but the address is noreply@github.com.
    reason = triage_skip(
        _row(sender="Example Ellersgaard User <noreply@github.com>")
    )
    assert reason is not None
    assert "noreply@github.com" in reason


def test_triage_skip_sender_match_is_case_insensitive() -> None:
    assert triage_skip(_row(sender="GitHub <NOREPLY@GITHUB.COM>")) is not None


# ---------- dedupe_by_thread ----------

def test_dedupe_by_thread_keeps_first_per_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """First-encountered wins; notmuch defaults to newest-first, so the
    representative is the latest message in each thread."""
    fake = {"m1": "t1", "m2": "t1", "m3": "t2", "m4": "t1", "m5": "t3"}
    monkeypatch.setattr(pr_compose, "thread_of", fake.get)

    kept, dupes = dedupe_by_thread(["m1", "m2", "m3", "m4", "m5"])
    assert kept == ["m1", "m3", "m5"]
    assert dupes == {"t1": ["m2", "m4"]}


def test_dedupe_by_thread_keeps_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message with no thread_id (notmuch can't resolve it) is kept as-is
    rather than silently dropped."""
    monkeypatch.setattr(
        pr_compose, "thread_of",
        lambda mid: None if mid == "orphan" else "t1",
    )
    kept, dupes = dedupe_by_thread(["m1", "orphan", "m2"])
    assert kept == ["m1", "orphan"]
    assert dupes == {"t1": ["m2"]}


def test_dedupe_by_thread_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_compose, "thread_of", lambda _: "t1")
    kept, dupes = dedupe_by_thread([])
    assert kept == []
    assert dupes == {}


# ---------- extract_body ----------

PLAIN_BODY = (
    b"From: a@b\nTo: c@d\nSubject: hi\n"
    b"Content-Type: text/plain; charset=utf-8\n\n"
    b"Hei p\xc3\xa5 deg\n"
)

HTML_ONLY = (
    b"From: a@b\nTo: c@d\nSubject: hi\n"
    b"Content-Type: text/html; charset=utf-8\n\n"
    b"<html><body><p>Hei <b>p\xc3\xa5 deg</b></p></body></html>\n"
)

MULTIPART_WITH_PDF = (
    b"From: a@b\nTo: c@d\nSubject: hi\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: multipart/mixed; boundary=BOUND\n\n"
    b"--BOUND\n"
    b"Content-Type: text/plain; charset=utf-8\n\n"
    b"Body text here.\n\n"
    b"--BOUND\n"
    b'Content-Type: application/pdf; name="x.pdf"\n'
    b'Content-Disposition: attachment; filename="x.pdf"\n'
    b"Content-Transfer-Encoding: base64\n\n"
    b"JVBERi0xLjQKQVRUQUNITUVOVA==\n"
    b"--BOUND--\n"
)

MULTIPART_ALT = (
    b"From: a@b\nTo: c@d\nSubject: hi\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: multipart/alternative; boundary=BOUND\n\n"
    b"--BOUND\n"
    b"Content-Type: text/plain; charset=utf-8\n\n"
    b"Plain version.\n\n"
    b"--BOUND\n"
    b"Content-Type: text/html; charset=utf-8\n\n"
    b"<html><body><p>HTML version.</p></body></html>\n\n"
    b"--BOUND--\n"
)


def test_extract_body_plain_text() -> None:
    msg = message_from_bytes(PLAIN_BODY, policy=email_default)
    assert "Hei på deg" in extract_body(msg)


def test_extract_body_html_fallback_strips_tags() -> None:
    msg = message_from_bytes(HTML_ONLY, policy=email_default)
    body = extract_body(msg)
    assert "Hei" in body
    assert "<p>" not in body
    assert "<b>" not in body


def test_extract_body_skips_pdf_attachment() -> None:
    msg = message_from_bytes(MULTIPART_WITH_PDF, policy=email_default)
    body = extract_body(msg)
    assert "Body text here." in body
    # Base64 PDF payload (marker bytes) must not leak into the classifier input.
    assert "ATTACHMENT" not in body
    assert "JVBER" not in body


def test_extract_body_prefers_plain_over_html_in_multipart_alt() -> None:
    msg = message_from_bytes(MULTIPART_ALT, policy=email_default)
    body = extract_body(msg)
    assert "Plain version." in body
    # If we picked plain, we should not also have stitched in the HTML.
    assert "HTML version." not in body


# ---------- resolve_query ----------

EXCLUSION_SUFFIX = 'not (tag:pr::triaged or thread:"{tag:pr::significant}")'


def test_resolve_query_default_appends_exclusion() -> None:
    args = argparse.Namespace(since_cursor=False, query=[], limit=None)
    query, limit = resolve_query(args)
    assert "tag:inbox and date:1w.." in query
    assert EXCLUSION_SUFFIX in query
    assert limit == 50


def test_resolve_query_explicit_query_kept_and_wrapped() -> None:
    args = argparse.Namespace(since_cursor=False,
                              query=["from:astrid"], limit=20)
    query, limit = resolve_query(args)
    assert "(from:astrid)" in query
    assert EXCLUSION_SUFFIX in query
    assert limit == 20


def test_resolve_query_since_cursor_uses_cursor_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cursor = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(pr_compose, "load_cursor", lambda: fake_cursor)
    args = argparse.Namespace(since_cursor=True, query=[], limit=None)
    query, _ = resolve_query(args)
    # cursor_query embeds the timestamp +1; just check the marker form.
    assert "date:@" in query
    assert EXCLUSION_SUFFIX in query


# ---------- classify (stubbed transport) ----------

class _StubResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _StubClient:
    """Captures the POST payload for assertions; returns a canned response."""
    def __init__(self, content: str) -> None:
        self._content = content
        self.last_payload: dict | None = None

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.last_payload = json
        return _StubResponse(self._content)


def test_classify_parses_clean_json() -> None:
    client = _StubClient('{"significant": true, "reason": "stort kjøp"}')
    sig, reason = classify(_row(body="x"), client)
    assert sig is True
    assert reason == "stort kjøp"


def test_classify_parses_code_fenced_json() -> None:
    client = _StubClient('```json\n{"significant": false, "reason": "reklame"}\n```')
    sig, reason = classify(_row(body="x"), client)
    assert sig is False
    assert reason == "reklame"


def test_classify_always_disables_thinking() -> None:
    """REGRESSION GUARD. Qwen3.6 with thinking on burns max_tokens on
    <think> blocks before emitting any answer; the smoke-test confirmed
    the classifier fails entirely without this flag. If a refactor
    drops it, the pipeline silently degrades to 200-token reasoning
    traces with no decision."""
    client = _StubClient('{"significant": false, "reason": "x"}')
    classify(_row(body="x"), client)
    assert client.last_payload is not None
    assert client.last_payload["chat_template_kwargs"]["enable_thinking"] is False


def test_classify_truncates_body_to_cap() -> None:
    """Bound the prompt — a 50-page PDF in the body shouldn't blow up token
    budget. We feed the classifier at most BODY_MAX_CHARS."""
    huge_body = "x" * (BODY_MAX_CHARS * 3)
    client = _StubClient('{"significant": false, "reason": "x"}')
    classify(_row(body=huge_body), client)
    assert client.last_payload is not None
    user_msg = client.last_payload["messages"][-1]["content"]
    # The body part comes after the "Subject: …\nFrom: …\n\n" prefix.
    body_part = user_msg.split("\n\n", 1)[1]
    assert len(body_part) == BODY_MAX_CHARS


def test_classify_uses_temperature_zero() -> None:
    """Classifier should be deterministic for the same input."""
    client = _StubClient('{"significant": false, "reason": "x"}')
    classify(_row(body="x"), client)
    assert client.last_payload is not None
    assert client.last_payload["temperature"] == 0


# ---------- format_line ----------

def test_format_line_truncates_long_subject() -> None:
    row = _row(subject="x" * 200, sender="y@z")
    line = format_line(1, 10, "SIG   ", row)
    # subject capped at 80; should never see 81 in a row
    assert "x" * 81 not in line


def test_format_line_includes_reason_when_provided() -> None:
    row = _row(subject="s", sender="y@z")
    line = format_line(1, 10, "SIG   ", row, reason="fordi")
    assert "fordi" in line
    assert "reason:" in line


def test_format_line_omits_reason_when_blank() -> None:
    row = _row(subject="s", sender="y@z")
    line = format_line(1, 10, "SIG   ", row, reason="")
    assert "reason:" not in line
