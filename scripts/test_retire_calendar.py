"""Tests for retire_calendar.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

import datetime as dt
from textwrap import dedent

import pytest

from scripts.retire_calendar import (
    EVENT_RE,
    MonthSection,
    Document,
    ensure_section,
    event_dates,
    parse,
    retire,
)


# ---------- event_dates: pin the parser-contract for event lines ----------


@pytest.mark.parametrize(
    "line, expected",
    [
        # Basic time-only event
        ("- **2026-05-26 17:30** — Fotballkamp\n", (dt.date(2026, 5, 26), dt.date(2026, 5, 26))),
        # Time range
        ("- **2026-05-26 17:00–19:00** — Korps\n", (dt.date(2026, 5, 26), dt.date(2026, 5, 26))),
        # All-day with (parens note)
        ("- **2026-05-29 (all day)** — Planleggingsdag\n", (dt.date(2026, 5, 29), dt.date(2026, 5, 29))),
        # Weekday note in parens
        ("- **2026-05-26 (tirsdag)** — Ring DNB\n", (dt.date(2026, 5, 26), dt.date(2026, 5, 26))),
        # Date span (en-dash) — end-date wins
        ("- **2026-06-29 – 2026-07-03 (uke 27)** — Sommerskolen\n", (dt.date(2026, 6, 29), dt.date(2026, 7, 3))),
        # Title contains bold markers — must not confuse the closing **
        ("- **2026-05-26 17:30** — **Fotballkamp Robin** — Eksempel-IL\n", (dt.date(2026, 5, 26), dt.date(2026, 5, 26))),
    ],
)
def test_event_dates_parses_parser_contract(line: str, expected: tuple[dt.date, dt.date]) -> None:
    assert event_dates(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Just prose.\n",
        "\n",
        "## Recurring weekly\n",
        "### May 2026\n",
        # Bulder-style prose bullet (no ISO date at the front)
        "- **Bulder bank-ingest** (siste søndag i måneden — neste: 2026-06-28).\n",
        # Heading-style bullets used elsewhere
        "- **Byggemelder:** Mesterhus Oslo og Akershus A/L\n",
    ],
)
def test_event_dates_rejects_non_events(line: str) -> None:
    assert event_dates(line) is None


# ---------- parse / render: structural invariants ----------


def test_parse_roundtrip_no_changes() -> None:
    """parse() → render() is identity for a well-formed file."""
    text = dedent(
        """\
        # Top

        ## Recurring weekly

        - Mondays — training

        ## One-off events by month

        Intro line.

        ### May 2026

        - **2026-05-26 17:30** — Kamp

        ### June 2026

        - **2026-06-01** — Skolesvømming
        """
    )
    doc = parse(text)
    assert doc.render() == text


def test_parse_handles_no_oneoff_section() -> None:
    text = "# Only top\n\n## Recurring\n\n- foo\n"
    doc = parse(text)
    assert doc.sections == []
    assert doc.render() == text


# ---------- retire: end-to-end ----------


SRC_MIN = dedent(
    """\
    # Family calendar

    ## Recurring weekly

    - Mondays — training

    ## One-off events by month

    Intro.

    ### May 2026

    - **2026-05-25 (all day)** — pinsedag
    - **2026-05-26 17:30** — Kamp
    - **2026-05-27 11:00** — Lunsj

    ### June 2026

    - **2026-06-01** — Skolesvømming
    """
)

DST_MIN = dedent(
    """\
    # Past

    ## One-off events by month

    ### April 2026

    - **2026-04-29** — gammelt event
    """
)


def test_retire_cuts_lines_strictly_before_today() -> None:
    new_src, new_dst, moved = retire(SRC_MIN, DST_MIN, dt.date(2026, 5, 27))
    # 25, 26 → expired. 27 (today) stays. 06-01 stays.
    assert len(moved) == 2
    assert "2026-05-25" in moved[0]
    assert "2026-05-26" in moved[1]
    # Source no longer contains the moved lines.
    assert "2026-05-25 (all day)" not in new_src
    assert "2026-05-26 17:30" not in new_src
    # Today's event stays.
    assert "2026-05-27 11:00" in new_src
    # Destination got them under May 2026.
    assert "### May 2026" in new_dst
    assert new_dst.index("2026-05-25") < new_dst.index("2026-05-26")
    # April section preserved + still has its event.
    assert "### April 2026" in new_dst
    assert "2026-04-29" in new_dst


def test_retire_idempotent_when_nothing_expired() -> None:
    new_src, new_dst, moved = retire(SRC_MIN, DST_MIN, dt.date(2026, 5, 1))
    assert moved == []
    assert new_src == SRC_MIN
    assert new_dst == DST_MIN


def test_retire_culls_empty_month_section() -> None:
    src = dedent(
        """\
        ## One-off events by month

        ### May 2026

        - **2026-05-26 17:30** — Kamp

        ### June 2026

        - **2026-06-01** — fortsatt fremtid
        """
    )
    new_src, _, moved = retire(src, "## One-off events by month\n", dt.date(2026, 5, 27))
    assert len(moved) == 1
    # May section is now empty → should be removed entirely.
    assert "### May 2026" not in new_src
    # June section untouched.
    assert "### June 2026" in new_src
    assert "2026-06-01" in new_src


def test_retire_creates_missing_destination_month() -> None:
    src = dedent(
        """\
        ## One-off events by month

        ### May 2026

        - **2026-05-26 17:30** — Kamp
        """
    )
    dst = dedent(
        """\
        ## One-off events by month

        ### April 2026

        - **2026-04-29** — old
        """
    )
    _, new_dst, moved = retire(src, dst, dt.date(2026, 5, 27))
    assert len(moved) == 1
    # New May 2026 section is created and slotted after April.
    assert "### April 2026" in new_dst
    assert "### May 2026" in new_dst
    assert new_dst.index("### April 2026") < new_dst.index("### May 2026")


def test_retire_inserts_chronologically_into_existing_month() -> None:
    src = dedent(
        """\
        ## One-off events by month

        ### May 2026

        - **2026-05-10** — middle
        """
    )
    dst = dedent(
        """\
        ## One-off events by month

        ### May 2026

        - **2026-05-05** — first
        - **2026-05-20** — last
        """
    )
    _, new_dst, _ = retire(src, dst, dt.date(2026, 6, 1))
    # Now May has three events, in chronological order.
    idx_05 = new_dst.index("2026-05-05")
    idx_10 = new_dst.index("2026-05-10")
    idx_20 = new_dst.index("2026-05-20")
    assert idx_05 < idx_10 < idx_20


def test_retire_span_event_filed_under_end_month() -> None:
    """A span 2026-06-29 → 2026-07-03 retires under July (end-month)."""
    src = dedent(
        """\
        ## One-off events by month

        ### July 2026

        - **2026-06-29 – 2026-07-03 (uke 27)** — Sommerskolen
        """
    )
    dst = "## One-off events by month\n"
    new_src, new_dst, moved = retire(src, dst, dt.date(2026, 7, 4))
    assert len(moved) == 1
    # End-date (2026-07-03) determines filing → July 2026 in dst.
    assert "### July 2026" in new_dst
    # And it's also before --today (07-04), so it must be cut.
    assert "2026-06-29" not in new_src


def test_retire_span_kept_while_end_in_future() -> None:
    """If end-date is today or later, the line stays."""
    src = dedent(
        """\
        ## One-off events by month

        ### July 2026

        - **2026-06-29 – 2026-07-03** — Sommerskolen
        """
    )
    new_src, new_dst, moved = retire(src, "## One-off events by month\n", dt.date(2026, 7, 3))
    # end == today → not strictly before → stays.
    assert moved == []
    assert new_src == src


def test_retire_leaves_prose_inside_oneoff_section_alone() -> None:
    """Prose between subheadings (e.g. an intro paragraph) must survive."""
    src = dedent(
        """\
        ## One-off events by month

        Intro that should survive.

        ### May 2026

        - **2026-05-26 17:30** — Kamp
        """
    )
    new_src, _, _ = retire(src, "## One-off events by month\n", dt.date(2026, 5, 27))
    assert "Intro that should survive." in new_src


def test_retire_recurring_section_untouched() -> None:
    """Events inside `## Recurring …` must not be touched, even if dated."""
    src = dedent(
        """\
        ## Recurring weekly

        - **2025-01-01 17:30** — should NOT be parsed as one-off

        ## One-off events by month

        ### May 2026

        - **2026-05-26 17:30** — Kamp
        """
    )
    new_src, _, moved = retire(src, "## One-off events by month\n", dt.date(2030, 1, 1))
    # Only the One-off event moves; the Recurring "event" stays put.
    assert len(moved) == 1
    assert "should NOT be parsed" in new_src


# ---------- ensure_section ordering ----------


def test_ensure_section_chronological_insertion() -> None:
    doc = parse(
        dedent(
            """\
            ## One-off events by month

            ### January 2026

            - **2026-01-15** — jan

            ### March 2026

            - **2026-03-10** — mar
            """
        )
    )
    section = ensure_section(doc, "February", 2026)
    months = [s.month for s in doc.sections]
    assert months == ["January", "February", "March"]
    assert section.month == "February"


def test_ensure_section_returns_existing() -> None:
    doc = parse(
        dedent(
            """\
            ## One-off events by month

            ### May 2026

            - **2026-05-01** — existing
            """
        )
    )
    s = ensure_section(doc, "May", 2026)
    assert "existing" in "".join(s.lines)
