"""Tests for mail_reader.calendar_md — markdown calendar parsing.

The bulk of parser behaviour is covered by doctests in
`mail_reader.calendar_md` itself (those double as usage examples).
What lives here is the end-to-end smoke test that exercises the real
CALENDAR.md / CALENDAR-PAST.md files in the repo, plus a regression
guard for the file-missing case.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from mail_reader.calendar_md import parse_calendar, render_markdown


def test_missing_file_returns_empty():
    assert parse_calendar(Path("/does/not/exist.md")) == []


def test_strikethrough_renders_del(tmp_path):
    """`~~text~~` in CALENDAR.md must render as <del>, not literal tildes.
    Regression for the 'Åpne oppgaver' done-item crossout (note #102)."""
    p = tmp_path / "CAL.md"
    p.write_text("## H\n\n- ~~velg fliser~~ ✅ ferdig\n", encoding="utf-8")
    html = render_markdown(p)
    assert "<del>velg fliser</del>" in html
    assert "~~" not in html


def test_parse_real_repo_files_smoke():
    """Actual repo files parse without error and yield a sensible number
    of events. Ensures we stay in sync with format drift."""
    repo = Path(__file__).resolve().parent.parent
    cal = repo / "CALENDAR.md"
    past = repo / "CALENDAR-PAST.md"
    if cal.exists():
        events = parse_calendar(cal)
        assert len(events) >= 5
        assert all(isinstance(e.start_date, date) for e in events)
        # Sort invariant: monotonic on start_date.
        for a, b in zip(events, events[1:]):
            assert a.start_date <= b.start_date
    if past.exists():
        events = parse_calendar(past)
        assert len(events) >= 1
