"""Tests for pr_compose.py. Run with `uv run pytest scripts/`.

Covers pure logic — code-fence stripping, triage rules, thread dedup,
body extraction, query construction, classifier payload shape. The
MLX HTTP call and notmuch subprocesses are stubbed, never live."""
from __future__ import annotations

import argparse
import json
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
    """Captures the POST payload for assertions; returns a canned response.

    `content` can be:
      - a str: same response for every URL (legacy single-endpoint tests).
      - a dict {url-substring: str}: route per-URL. Used by tests that need
        to distinguish NuExtract (port 8081) from generator (port 8080).
    """
    def __init__(self, content: str | dict[str, str]) -> None:
        self._content = content
        self.last_payload: dict | None = None
        # Per-call (url, payload) log so multi-endpoint tests can assert
        # what was sent where without having to read last_payload mid-flight.
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.last_payload = json
        self.posts.append((url, json))
        if isinstance(self._content, dict):
            for key, val in self._content.items():
                if key in url:
                    return _StubResponse(val)
            raise KeyError(f"_StubClient: no canned response for url {url!r}")
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


# ---------- writer: _slugify ----------

@pytest.mark.parametrize("raw, expected", [
    ("Eksempel Elektriske AS", "eksempel-elektriske-as"),
    ("Eksempeldalen barnehage", "eksempeldalen-barnehage"),
    ("Tre øl & én ås", "tre-ol-en-as"),
    ("ALREADY-kebab", "already-kebab"),
    ("  spaces  here  ", "spaces-here"),
    ("___underscores", "underscores"),
    ("123 only", "123-only"),
    ("", ""),
    ("---", ""),
    ("a" * 100, "a" * 50),  # capped at 50
])
def test_slugify(raw: str, expected: str) -> None:
    from scripts.pr_compose import _slugify
    assert _slugify(raw) == expected


# ---------- writer: _validate_writer_output ----------

def _writer_dict(**overrides):
    base = {
        "pr_title": "Tilbud fra Eksempel Elektriske",
        "branch_keyword": "eksempel-elektriske-tilbud",
        "memory_heading": "Eksempel Elektriske — tilbudsbrev",
        "memory_body": "Tilbudet er på 95 000 NOK. Frist 15. juni.",
    }
    base.update(overrides)
    return base


def test_validate_writer_output_passes_clean_dict() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict())
    assert out["pr_title"].startswith("Tilbud")
    assert out["branch_keyword"] == "eksempel-elektriske-tilbud"
    assert out["memory_heading"] == "Eksempel Elektriske — tilbudsbrev"
    assert out["memory_body"].endswith("\n")  # body normalised with trailing newline


def test_validate_writer_output_strips_hash_prefix_from_heading() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(memory_heading="## Streik"))
    assert out["memory_heading"] == "Streik"


def test_validate_writer_output_slugifies_branch_keyword() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(branch_keyword="Stort Kjøp Mac Studio"))
    assert out["branch_keyword"] == "stort-kjop-mac-studio"


def test_validate_writer_output_falls_back_when_slug_empty() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(branch_keyword="!!!"))
    assert out["branch_keyword"] == "mail"


def test_validate_writer_output_truncates_long_title() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(pr_title="x" * 200))
    assert len(out["pr_title"]) == 70


def test_validate_writer_output_rejects_missing_key() -> None:
    from scripts.pr_compose import _validate_writer_output
    bad = _writer_dict()
    del bad["pr_title"]
    with pytest.raises(ValueError, match="pr_title"):
        _validate_writer_output(bad)


def test_validate_writer_output_tolerates_null_fields() -> None:
    """NuExtract returns `null` for any field it couldn't fill (e.g.
    memory_body on a 187-char event-invite mail like Spiker'n sommerfest).
    Falling through with a crash would block the pipeline on otherwise-
    valid extractions; the validator instead substitutes pr_title for
    missing heading/branch and a Norwegian placeholder for missing body."""
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(
        memory_heading=None,
        memory_body=None,
        branch_keyword=None,
    ))
    assert out["memory_heading"] == "Tilbud fra Eksempel Elektriske"  # falls back to pr_title
    assert "modell trakk ikke ut" in out["memory_body"]
    assert out["branch_keyword"] == "tilbud-fra-eksempel-elektriske"  # slugified pr_title


def test_validate_writer_output_still_rejects_missing_pr_title() -> None:
    """pr_title is the one field without a sensible fallback — a PR has
    to have a title. If NuExtract returns null/empty for it, that's a
    hard failure rather than silently producing a blank PR."""
    from scripts.pr_compose import _validate_writer_output
    with pytest.raises(ValueError, match="pr_title"):
        _validate_writer_output(_writer_dict(pr_title=None))
    with pytest.raises(ValueError, match="pr_title"):
        _validate_writer_output(_writer_dict(pr_title=""))


def test_validate_writer_output_dedups_calendar_candidates() -> None:
    """Reply-chain quoting causes NuExtract to emit the same (date, title,
    evidence) candidate multiple times — see the Pia/Examplefund thread which
    produced 27. mai × 3. Dedup is keyed on the full triple so different
    events on the same date still come through."""
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(calendar_candidates=[
        {"date": "2026-05-27", "title": "Møte", "evidence": "onsdag 27. mai"},
        {"date": "2026-05-27", "title": "Møte", "evidence": "onsdag 27. mai"},
        {"date": "2026-05-27", "title": "Lunsj", "evidence": "samme dag 12:00"},
        {"date": "2026-05-27", "title": "Møte", "evidence": "onsdag 27. mai"},
    ]))
    assert len(out["calendar_candidates"]) == 2
    titles = {c["title"] for c in out["calendar_candidates"]}
    assert titles == {"Møte", "Lunsj"}


def test_validate_writer_output_drops_dateless_candidates() -> None:
    """A candidate without a date is useless (can't dedup against the
    calendar, can't render meaningfully) — quietly drop rather than
    pollute the PR body."""
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(calendar_candidates=[
        {"date": "2026-05-27", "title": "OK", "evidence": "27. mai"},
        {"date": None, "title": "Mangler dato", "evidence": "snart"},
        {"date": "", "title": "Tom dato", "evidence": "snart"},
    ]))
    assert len(out["calendar_candidates"]) == 1
    assert out["calendar_candidates"][0]["title"] == "OK"


def test_validate_writer_output_flags_no_substance_when_body_null() -> None:
    """The substance flag drives whether file_prs_for_significant_threads
    actually opens a PR. When the model returns null body, the validator
    fills in a placeholder *for preview readability* but marks
    memory_body_from_model=False so the caller can skip — a placeholder
    file diff (heading + filler) is noise in the long-term memory file."""
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(memory_body=None))
    assert out["memory_body_from_model"] is False
    out = _validate_writer_output(_writer_dict(memory_body=""))
    assert out["memory_body_from_model"] is False


def test_validate_writer_output_flags_substance_when_body_present() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(
        memory_body="Tilbudet er på 95 000 NOK. Frist 15. juni.",
    ))
    assert out["memory_body_from_model"] is True


def test_validate_writer_output_tolerates_null_candidate_fields() -> None:
    """Some Pia-style candidates come back with title=null even though the
    date is present. We keep them (date is the actionable bit) and just
    treat null title/evidence as empty strings."""
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(calendar_candidates=[
        {"date": "2026-05-27", "title": None, "evidence": None},
    ]))
    assert len(out["calendar_candidates"]) == 1
    assert out["calendar_candidates"][0]["date"] == "2026-05-27"
    assert out["calendar_candidates"][0]["title"] == ""
    assert out["calendar_candidates"][0]["evidence"] == ""


# ---------- writer: make_branch_name ----------

def test_make_branch_name_format() -> None:
    from datetime import date as _date
    import random as _random
    from scripts.pr_compose import make_branch_name
    name = make_branch_name("eksempel-elektriske", _date(2026, 5, 27),
                            rng=_random.Random(0))
    assert name.startswith("mail/2026-05-27-eksempel-elektriske-")
    suffix = name.rsplit("-", 1)[-1]
    assert len(suffix) == 4
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in suffix)


def test_make_branch_name_random_suffix_varies() -> None:
    """Two calls should (overwhelmingly likely) produce different suffixes."""
    from scripts.pr_compose import make_branch_name
    n1 = make_branch_name("x")
    n2 = make_branch_name("x")
    assert n1.rsplit("-", 1)[-1] != n2.rsplit("-", 1)[-1]


# ---------- writer: compose_pr (stubbed transport) ----------

def test_compose_pr_parses_and_normalises() -> None:
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (
            '{"pr_title": "Streik-varsel Eksempeldalen", '
            '"branch_keyword": "Streik Eksempeldalen", '
            '"memory_heading": "Streik-varsel", '
            '"memory_body": "Barnehagen varsler mulig streik fra 5. juni."}'
        ),
        "8080": "HEADING: Streik-varsel\nBODY:\nBarnehagen varsler streik.\n",
    })
    out = compose_pr(_row(body="Streikevarsel innhold"), client)
    assert out["branch_keyword"] == "streik-eksempeldalen"  # slugified + folded
    assert out["pr_title"] == "Streik-varsel Eksempeldalen"


# ---------- writer: _is_future / _render_memory_section ----------

def test_is_future_single_date() -> None:
    from datetime import date as _d
    from scripts.pr_compose import _is_future
    today = _d(2026, 5, 28)
    assert _is_future("2026-05-28", today) is True  # today counts as future
    assert _is_future("2026-05-29", today) is True
    assert _is_future("2026-05-27", today) is False
    assert _is_future("2025-12-31", today) is False


def test_is_future_date_range_uses_end() -> None:
    """A multi-day event that started yesterday but ends tomorrow is still
    future-relevant — we use the END of the range."""
    from datetime import date as _d
    from scripts.pr_compose import _is_future
    today = _d(2026, 5, 28)
    assert _is_future("2026-05-27 – 2026-05-29", today) is True
    assert _is_future("2026-05-25 – 2026-05-27", today) is False


def test_is_future_unparseable_returns_false() -> None:
    """Unparseable date strings are treated as non-future so they don't
    accidentally rescue the substance gate or land in the diff."""
    from datetime import date as _d
    from scripts.pr_compose import _is_future
    today = _d(2026, 5, 28)
    assert _is_future("", today) is False
    assert _is_future("snart", today) is False
    assert _is_future("2026", today) is False


def test_render_memory_section_no_candidates_passthrough() -> None:
    from scripts.pr_compose import _render_memory_section
    writer = {
        "memory_heading": "Tilbud fra Eksempel",
        "memory_body": "Tilbudet er på 95 000 NOK.\n",
        "calendar_candidates": [],
    }
    heading, body = _render_memory_section(writer)
    assert heading == "Tilbud fra Eksempel"
    assert body == "Tilbudet er på 95 000 NOK.\n"


def test_render_memory_section_prepends_earliest_future_date_to_heading() -> None:
    """The earliest future candidate's date becomes a chronological anchor
    in the heading so the daily file groups by date naturally."""
    from datetime import date as _d
    from scripts.pr_compose import _render_memory_section
    writer = {
        "memory_heading": "Sommerfest Spiker'n",
        "memory_body": "Velkommen-mail fra SchoolApp.\n",
        "calendar_candidates": [
            {"date": "2026-06-15", "title": "Frist RSVP", "evidence": "innen 15.6"},
            {"date": "2026-06-10", "title": "Sommerfest", "evidence": "10. juni"},
        ],
    }
    heading, _ = _render_memory_section(writer, today=_d(2026, 5, 28))
    assert heading == "2026-06-10 — Sommerfest Spiker'n"


def test_render_memory_section_body_passthrough() -> None:
    """Memory body is the writer's narrative untouched. Structured event
    list lives in CALENDAR.md now (via _insert_calendar_events), not
    duplicated in the memory file as a Datoer block."""
    from datetime import date as _d
    from scripts.pr_compose import _render_memory_section
    writer = {
        "memory_heading": "Sommerfest",
        "memory_body": "Velkommen.\n",
        "calendar_candidates": [
            {"date": "2026-06-10", "title": "Sommerfest", "already_in_calendar": True,
             "evidence": "10. juni"},
        ],
    }
    _, body = _render_memory_section(writer, today=_d(2026, 5, 28))
    assert body == "Velkommen.\n"  # unchanged
    assert "Datoer:" not in body


def test_render_memory_section_filters_past_dates_from_heading() -> None:
    """Pia/Examplefund-style mails reference past transaction dates — those
    shouldn't pollute the heading either."""
    from datetime import date as _d
    from scripts.pr_compose import _render_memory_section
    writer = {
        "memory_heading": "User Holding AS - 2025",
        "memory_body": "Skattepapirer.\n",
        "calendar_candidates": [
            {"date": "2025-04-01", "title": "Kjøp", "evidence": "..."},
        ],
    }
    heading, _ = _render_memory_section(writer, today=_d(2026, 5, 28))
    assert heading == "User Holding AS - 2025"  # untouched, no future dates


# ---------- writer: _calendar_event_line / _insert_calendar_events ----------

def test_calendar_event_line_basic() -> None:
    from scripts.pr_compose import _calendar_event_line
    line = _calendar_event_line(
        {"date": "2026-06-03", "title": "Gulvet primes"},
        thread_id="deadbeef",
    )
    assert line == "- **2026-06-03** — Gulvet primes. (Kilde: mail thread:deadbeef.)\n"


def test_calendar_event_line_parses_back_to_original_date() -> None:
    """The emitted line must satisfy retire_calendar's parser contract —
    that's the same parser the daily sjekk-flow + webvisning use."""
    import datetime as _dt
    from scripts.pr_compose import _calendar_event_line
    from scripts.retire_calendar import event_dates
    line = _calendar_event_line(
        {"date": "2026-06-03", "title": "Gulvet primes"}, thread_id="x",
    )
    assert event_dates(line) == (_dt.date(2026, 6, 3), _dt.date(2026, 6, 3))


def test_calendar_event_line_empty_title_uses_placeholder() -> None:
    """Pia-style null titles must still produce a parseable line — the
    parser keys on the date format, not the title text."""
    from scripts.pr_compose import _calendar_event_line
    from scripts.retire_calendar import event_dates
    line = _calendar_event_line(
        {"date": "2026-06-03", "title": ""}, thread_id="x",
    )
    assert "(uten tittel)" in line
    assert event_dates(line) is not None


def test_insert_calendar_events_creates_month_section(tmp_path) -> None:
    """No June section yet → ensure_section creates one chronologically."""
    from datetime import date as _d
    from scripts.pr_compose import _insert_calendar_events
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(
        "# Calendar\n\n"
        "## One-off events by month\n\n"
        "### May 2026\n\n"
        "- **2026-05-20** — Already there.\n"
    )
    n = _insert_calendar_events(
        [{"date": "2026-06-10", "title": "Ny event"}],
        cal, thread_id="abc", today=_d(2026, 5, 28),
    )
    assert n == 1
    text = cal.read_text()
    assert "### June 2026" in text
    assert "- **2026-06-10** — Ny event. (Kilde: mail thread:abc.)" in text
    assert "- **2026-05-20** — Already there." in text  # existing preserved


def test_insert_calendar_events_inserts_chronologically(tmp_path) -> None:
    """Within a month, new events go in date order so the file stays
    readable."""
    from datetime import date as _d
    from scripts.pr_compose import _insert_calendar_events
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(
        "# Calendar\n\n"
        "## One-off events by month\n\n"
        "### June 2026\n\n"
        "- **2026-06-01** — First.\n"
        "- **2026-06-15** — Last.\n"
    )
    _insert_calendar_events(
        [{"date": "2026-06-08", "title": "Middle"}],
        cal, thread_id="x", today=_d(2026, 5, 28),
    )
    lines = cal.read_text().splitlines()
    june_lines = [ln for ln in lines if "2026-06-" in ln]
    dates_in_order = [ln.split("**")[1] for ln in june_lines]
    assert dates_in_order == ["2026-06-01", "2026-06-08", "2026-06-15"]


def test_insert_calendar_events_skips_past_dates(tmp_path) -> None:
    """Past dates (Pia's 2025-04-01 etc.) must not be inserted —
    CALENDAR.md is the active forward calendar, past goes to
    CALENDAR-PAST.md via retire_calendar."""
    from datetime import date as _d
    from scripts.pr_compose import _insert_calendar_events
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(
        "# Calendar\n\n## One-off events by month\n\n### May 2026\n\n- **2026-05-29** — x.\n"
    )
    original = cal.read_text()
    n = _insert_calendar_events(
        [{"date": "2025-04-01", "title": "Past transaction"}],
        cal, thread_id="x", today=_d(2026, 5, 28),
    )
    assert n == 0
    assert cal.read_text() == original  # byte-identical


def test_insert_calendar_events_no_candidates_no_write(tmp_path) -> None:
    """No future candidates → don't touch the file at all (idempotency
    matters; we don't want a write-then-diff that's actually a no-op)."""
    from datetime import date as _d
    from scripts.pr_compose import _insert_calendar_events
    cal = tmp_path / "CALENDAR.md"
    cal.write_text("# Calendar\n\n## One-off events by month\n")
    original_mtime = cal.stat().st_mtime_ns
    n = _insert_calendar_events([], cal, thread_id="x", today=_d(2026, 5, 28))
    assert n == 0
    assert cal.stat().st_mtime_ns == original_mtime  # untouched


def test_insert_calendar_events_handles_overlap_candidates(tmp_path) -> None:
    """already_in_calendar=True candidates still get inserted —
    overlap detection is purely advisory in the PR description. Skipping
    here would silently drop unrelated events that just happen to share
    a date with something else."""
    from datetime import date as _d
    from scripts.pr_compose import _insert_calendar_events
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(
        "# Calendar\n\n## One-off events by month\n\n### June 2026\n\n"
        "- **2026-06-04 18:00** — Fotballkamp Robin.\n"
    )
    n = _insert_calendar_events(
        [{"date": "2026-06-04", "title": "Avretting", "already_in_calendar": True}],
        cal, thread_id="x", today=_d(2026, 5, 28),
    )
    assert n == 1
    assert "- **2026-06-04** — Avretting" in cal.read_text()


def test_compose_pr_falls_back_to_subject_when_title_null() -> None:
    """NuExtract at temp=0.2 occasionally nulls every field on terse mails
    (Spiker'n sommerfest, 187 chars). The validator hard-rejects null
    pr_title, so without this fallback we crash on otherwise-skippable
    threads. Subject is always present and for these mails IS the title.
    (The substance gate now passes because tier-3 fills in heading/body
    — the gate fires on real null-extraction in production when tier-3
    also struggles, not in this controlled-stub test.)"""
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (
            '{"pr_title": null, "branch_keyword": null, '
            '"memory_heading": null, "memory_body": null, '
            '"calendar_candidates": []}'
        ),
        "8080": "HEADING: Sommerfest\nBODY:\nVelkommen-mail fra SchoolApp.\n",
    })
    out = compose_pr(_row(subject="Velkommen til sommerfest", body="x"), client)
    assert out["pr_title"] == "Velkommen til sommerfest"


# ---------- writer: tier-3 generator ----------

def test_parse_generator_output_clean() -> None:
    from scripts.pr_compose import _parse_generator_output
    out = _parse_generator_output(
        "HEADING: Sommerfest Spiker'n\nBODY:\nVelkommen-mail fra SchoolApp.\n"
    )
    assert out["heading"] == "Sommerfest Spiker'n"
    assert out["body"] == "Velkommen-mail fra SchoolApp."


def test_parse_generator_output_multiline_body() -> None:
    """Generator body is markdown; multiple lines + blank lines must
    survive to the file diff."""
    from scripts.pr_compose import _parse_generator_output
    out = _parse_generator_output(
        "HEADING: Plan for gulvarbeid\n"
        "BODY:\n"
        "Gulvet primes 3. juni.\n"
        "\n"
        "- Varmekabler 3.-4. juni\n"
        "- Avretting 4.-5. juni\n"
    )
    assert "Varmekabler 3.-4. juni" in out["body"]
    assert "Avretting" in out["body"]


def test_parse_generator_output_strips_code_fence() -> None:
    """Qwen sometimes wraps output in ```markdown ``` despite being told
    not to. Reuse _strip_code_fence semantics."""
    from scripts.pr_compose import _parse_generator_output
    out = _parse_generator_output(
        "```\nHEADING: x\nBODY:\ny\n```"
    )
    assert out["heading"] == "x"
    assert out["body"] == "y"


def test_parse_generator_output_raises_on_missing_heading_marker() -> None:
    """Offensive programming: the parser is the boundary between "model
    produced expected format" and "something is broken upstream". Silent
    partial results would hide prompt drift or :8080 returning chatty
    prose. Caller (compose_pr → file_prs_for_significant_threads) sees
    the loud error and counts the thread as error."""
    from scripts.pr_compose import _parse_generator_output
    with pytest.raises(ValueError, match="HEADING"):
        _parse_generator_output("Her er en oppsummering: dette er en mail.")


def test_parse_generator_output_raises_on_missing_body_marker() -> None:
    from scripts.pr_compose import _parse_generator_output
    with pytest.raises(ValueError, match="BODY"):
        _parse_generator_output("HEADING: x\nIngen body-markør her.")


def test_parse_generator_output_raises_on_empty_heading() -> None:
    from scripts.pr_compose import _parse_generator_output
    with pytest.raises(ValueError, match="empty HEADING"):
        _parse_generator_output("HEADING:\nBODY:\ny\n")


def test_parse_generator_output_raises_on_empty_body() -> None:
    from scripts.pr_compose import _parse_generator_output
    with pytest.raises(ValueError, match="empty BODY"):
        _parse_generator_output("HEADING: x\nBODY:\n")


def test_compose_pr_uses_generator_output_for_heading_and_body() -> None:
    """Tier-3 generator replaces NuExtract's heading/body when it
    succeeds. NuExtract still owns calendar_candidates + pr_title."""
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (  # NuExtract
            '{"pr_title": "Tilbud Eksempel", '
            '"branch_keyword": "eksempel-tilbud", '
            '"memory_heading": "Tilbud", '
            '"memory_body": "Tilbud", '
            '"calendar_candidates": []}'
        ),
        "8080": (  # Generator
            "HEADING: Eksempel tilbud — uke 23\n"
            "BODY:\n"
            "Tilbudet er på 95 000 NOK. Frist 15. juni.\n"
        ),
    })
    out = compose_pr(_row(body="Tilbudsbrev"), client)
    assert out["memory_heading"] == "Eksempel tilbud — uke 23"
    assert "95 000 NOK" in out["memory_body"]
    assert out["pr_title"] == "Tilbud Eksempel"  # from NuExtract, untouched
    assert out["memory_body_from_model"] is True


def test_compose_pr_propagates_generator_parse_failure() -> None:
    """REGRESSION GUARD. Earlier version silently fell back to NuExtract's
    sentence-plucking output when the generator returned malformed text.
    That hid prompt drift / model changes / :8080 issues. Now: parse
    error from the generator propagates up and the per-thread error
    handler in file_prs_for_significant_threads counts it loudly."""
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (
            '{"pr_title": "x", "branch_keyword": "x", '
            '"memory_heading": "Tilbud Eksempel", '
            '"memory_body": "Tilbudet er på 95 000 NOK.", '
            '"calendar_candidates": []}'
        ),
        "8080": "I am just chatting, no HEADING/BODY here",  # parser raises
    })
    with pytest.raises(ValueError, match="HEADING"):
        compose_pr(_row(body="x"), client)


def test_compose_pr_passes_nuextract_facts_to_generator() -> None:
    """Generator gets the extraction as JSON in the user message so it
    can anchor dates/amounts/names without re-extracting from the mail."""
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (
            '{"pr_title": "x", "branch_keyword": "x", '
            '"memory_heading": "x", "memory_body": "x", '
            '"calendar_candidates": [{"date": "2026-06-10", "title": "Frist", '
            '"evidence": "innen 10/6"}]}'
        ),
        "8080": "HEADING: y\nBODY:\nz\n",
    })
    compose_pr(_row(body="x"), client)
    # Two calls: NuExtract first, generator second.
    assert len(client.posts) == 2
    gen_url, gen_payload = client.posts[1]
    assert "8080" in gen_url
    user_msg = gen_payload["messages"][-1]["content"]
    assert "2026-06-10" in user_msg
    assert "Frist" in user_msg


def test_compose_pr_disables_thinking_and_omits_tools_on_both_tiers() -> None:
    """REGRESSION GUARD. Both the NuExtract extractor (:8081) and the Qwen
    tier-3 generator (:8080) must run with thinking disabled and no
    tools. Qwen3.6 thinking-mode + tool-calling is an upstream-broken
    combination — thinking-on loops on uncertainty, thinking-off skips
    tool calls. NuExtract has no thinking mode but we still pass
    enable_thinking=False to be safe. Calendar dedup is Python-side."""
    from scripts.pr_compose import compose_pr
    client = _StubClient({
        "8081": (
            '{"pr_title": "x", "branch_keyword": "x", '
            '"memory_heading": "x", "memory_body": "x", '
            '"calendar_candidates": []}'
        ),
        "8080": "HEADING: x\nBODY:\ny\n",
    })
    compose_pr(_row(body="x"), client)
    assert len(client.posts) == 2
    for url, payload in client.posts:
        assert payload["chat_template_kwargs"]["enable_thinking"] is False, url
        assert "tools" not in payload, url


# ---------- _render_pr_body ----------

def test_render_pr_body_includes_thread_subject_and_body() -> None:
    from scripts.pr_compose import _render_pr_body
    row = _row(subject="Tilbud 20260510-1", sender="thor@eksempel.no",
               body="Hei,\n\nTilbudet er på 95 000 NOK.\n")
    body = _render_pr_body(row, "deadbeef")
    assert "thread:deadbeef" in body
    assert "Tilbud 20260510-1" in body
    assert "thor@eksempel.no" in body
    assert "95 000 NOK" in body
    assert "~~~" in body  # fenced block uses tildes to survive backtick-containing mail
    # rollback hint must reference thread for the future-me
    assert "tag -pr::filed" in body


def test_render_pr_body_marks_truncated_when_body_long() -> None:
    from scripts.pr_compose import _render_pr_body
    row = _row(subject="x", sender="y@z", body="a" * 5000)
    body = _render_pr_body(row, "t")
    assert "truncated" in body


# ---------- tools: _tool_get_calendar_events ----------

CALENDAR_FIXTURE = """\
# Calendar

## One-off events by month

### May 2026

- **2026-05-27 10:10–10:40** — Foreldresamtale Bjorn
- **2026-05-29 (all day)** — Planleggingsdag barnehagen
- **2026-05-31 14:00** — Fika hos Arna

### June 2026

- **2026-06-02 17:30** — Fotballkamp Robin
- **2026-06-29 – 2026-07-03 (uke 27)** — Sommerskolen Robin
"""


@pytest.fixture
def cal_repo(tmp_path):
    (tmp_path / "CALENDAR.md").write_text(CALENDAR_FIXTURE)
    (tmp_path / "CALENDAR-PAST.md").write_text(
        "# Past\n\n## One-off events by month\n\n### April 2026\n\n- **2026-04-15** — Old event\n"
    )
    return tmp_path


def test_tool_get_calendar_events_in_range(cal_repo) -> None:
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("2026-05-27", "2026-06-02", repo_root=cal_repo))
    lines = [e["line"] for e in out["events"]]
    assert any("Foreldresamtale Bjorn" in l for l in lines)
    assert any("Planleggingsdag" in l for l in lines)
    assert any("Fika hos Arna" in l for l in lines)
    assert any("Fotballkamp Robin" in l for l in lines)
    # Out of range:
    assert not any("Sommerskolen" in l for l in lines)


def test_tool_get_calendar_events_span_overlaps_range(cal_repo) -> None:
    """A span 2026-06-29 → 2026-07-03 must surface when range is 2026-07-01..07-02."""
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("2026-07-01", "2026-07-02", repo_root=cal_repo))
    assert any("Sommerskolen" in e["line"] for e in out["events"])


def test_tool_get_calendar_events_includes_past(cal_repo) -> None:
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("2026-04-01", "2026-04-30", repo_root=cal_repo))
    assert any("Old event" in e["line"] for e in out["events"])
    assert any(e["source"] == "CALENDAR-PAST.md" for e in out["events"])


def test_tool_get_calendar_events_empty_range(cal_repo) -> None:
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("2027-01-01", "2027-01-31", repo_root=cal_repo))
    assert out["events"] == []


def test_tool_get_calendar_events_rejects_bad_date(cal_repo) -> None:
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("not-a-date", "2026-06-01", repo_root=cal_repo))
    assert "error" in out


def test_tool_get_calendar_events_rejects_inverted_range(cal_repo) -> None:
    from scripts.pr_compose import _tool_get_calendar_events
    out = json.loads(_tool_get_calendar_events("2026-06-01", "2026-05-01", repo_root=cal_repo))
    assert "error" in out


# ---------- compose_pr — Python-side calendar verification ----------

def test_compose_pr_calls_verify_candidates(monkeypatch) -> None:
    """compose_pr must hand the model's candidates through _verify_candidates
    so already_in_calendar is set by Python, not the model. Tool-use through
    the OpenAI agent loop is unreliable on MLX + Qwen3.6 (high truncation
    rate, `tool_choice: required` silently ignored)."""
    from scripts import pr_compose as pc
    seen = {"called_with": None}

    def fake_verify(candidates):
        seen["called_with"] = candidates
        return [{**c, "already_in_calendar": True} for c in candidates]

    monkeypatch.setattr(pc, "_verify_candidates", fake_verify)
    client = _StubClient({
        "8081": json.dumps(_writer_dict(calendar_candidates=[
            {"date": "2026-05-29", "title": "Test", "evidence": "e"}
        ])),
        "8080": "HEADING: x\nBODY:\ny\n",
    })
    out = pc.compose_pr(_row(body="x"), client)
    assert seen["called_with"][0]["title"] == "Test"
    assert out["calendar_candidates"][0]["already_in_calendar"] is True


# ---------- _parse_candidate_date ----------

@pytest.mark.parametrize("raw, expected_start, expected_end", [
    ("2026-05-29", "2026-05-29", "2026-05-29"),
    ("2026-06-29 – 2026-07-03", "2026-06-29", "2026-07-03"),  # en-dash
    ("2026-06-29 - 2026-07-03", "2026-06-29", "2026-07-03"),  # hyphen
    ("2026-06-29–2026-07-03", "2026-06-29", "2026-07-03"),    # no spaces
])
def test_parse_candidate_date_valid(raw, expected_start, expected_end) -> None:
    from datetime import date
    from scripts.pr_compose import _parse_candidate_date
    s, e = _parse_candidate_date(raw)
    assert s == date.fromisoformat(expected_start)
    assert e == date.fromisoformat(expected_end)


@pytest.mark.parametrize("raw", [
    "",
    "2026-05",         # incomplete
    "next Friday",     # natural language — model should have resolved this
    "fredag",
    "2026-05-29 17:00",  # time appended (model shouldn't include but defensive)
])
def test_parse_candidate_date_unparseable(raw) -> None:
    from scripts.pr_compose import _parse_candidate_date
    s, e = _parse_candidate_date(raw)
    assert s is None and e is None


# ---------- _verify_candidates ----------

def test_verify_candidates_marks_overlap_as_already(cal_repo, monkeypatch) -> None:
    """Candidate overlapping with an existing event should be flagged
    already_in_calendar=True."""
    from scripts import pr_compose as pc
    real_tool = pc._tool_get_calendar_events
    monkeypatch.setattr(pc, "_tool_get_calendar_events",
                        lambda s, e: real_tool(s, e, repo_root=cal_repo))
    out = pc._verify_candidates([
        {"date": "2026-05-29", "title": "Planleggingsdag", "evidence": "fredag"},
    ])
    assert out[0]["already_in_calendar"] is True


def test_verify_candidates_marks_no_overlap_as_new(cal_repo, monkeypatch) -> None:
    from scripts import pr_compose as pc
    real_tool = pc._tool_get_calendar_events
    monkeypatch.setattr(pc, "_tool_get_calendar_events",
                        lambda s, e: real_tool(s, e, repo_root=cal_repo))
    out = pc._verify_candidates([
        {"date": "2026-12-25", "title": "Julaften", "evidence": "x"},
    ])
    assert out[0]["already_in_calendar"] is False


def test_verify_candidates_preserves_unparseable_dates(monkeypatch) -> None:
    """If date can't be parsed (model produced bad output), still surface
    the candidate to the human reviewer with already_in_calendar=False."""
    from scripts import pr_compose as pc
    monkeypatch.setattr(pc, "_tool_get_calendar_events",
                        lambda s, e: '{"events": []}')
    out = pc._verify_candidates([
        {"date": "next Friday", "title": "Møte", "evidence": "x"},
    ])
    assert len(out) == 1
    assert out[0]["already_in_calendar"] is False
    assert out[0]["title"] == "Møte"


def test_verify_candidates_handles_empty_list() -> None:
    from scripts.pr_compose import _verify_candidates
    assert _verify_candidates([]) == []


# ---------- writer output: calendar_candidates ----------

def test_validate_writer_output_accepts_calendar_candidates() -> None:
    from scripts.pr_compose import _validate_writer_output
    d = _writer_dict(calendar_candidates=[
        {"date": "2026-05-29", "title": "Planleggingsdag",
         "evidence": "fredag", "already_in_calendar": True}
    ])
    out = _validate_writer_output(d)
    assert len(out["calendar_candidates"]) == 1
    assert out["calendar_candidates"][0]["already_in_calendar"] is True


def test_validate_writer_output_defaults_empty_candidates_list() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict())
    assert out["calendar_candidates"] == []


def test_validate_writer_output_drops_malformed_candidate_entries() -> None:
    from scripts.pr_compose import _validate_writer_output
    out = _validate_writer_output(_writer_dict(calendar_candidates=[
        "not a dict",
        {"date": "2026-05-29", "title": "OK", "evidence": "e"},
    ]))
    # Only the dict survives.
    assert len(out["calendar_candidates"]) == 1
    assert out["calendar_candidates"][0]["title"] == "OK"


def test_render_pr_body_renders_calendar_candidates_section() -> None:
    from scripts.pr_compose import _render_pr_body
    row = _row(subject="Bursdag", sender="x@y", body="Olai fyller år 29.05")
    body = _render_pr_body(row, "tid", calendar_candidates=[
        {"date": "2026-05-29", "title": "Bursdag Olai",
         "evidence": "fyller år 29.05", "already_in_calendar": False},
    ])
    assert "Calendar candidates" in body
    assert "Bursdag Olai" in body
    assert "⊕ new" in body


def test_render_pr_body_no_candidates_section_when_empty() -> None:
    from scripts.pr_compose import _render_pr_body
    row = _row(subject="x", sender="y@z", body="body")
    body = _render_pr_body(row, "t", calendar_candidates=[])
    assert "Calendar candidates" not in body
