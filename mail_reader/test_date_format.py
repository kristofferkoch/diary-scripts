"""Tests for the `short_date` Jinja filter used in tankekart cards.

Bug it pins (TDD): the tankekart card date format used to be a raw
`strftime('%d. %b')`, which drops the year. Mails from 2010 rendered as
just "11. jun" — indistinguishable from a 2026 mail. The fix introduces
a year-aware filter and rewires the template to use it.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from mail_reader.date_format import relative_day, short_date


def test_short_date_current_year_omits_year():
    today = datetime(2026, 5, 22)
    assert short_date(datetime(2026, 4, 15), today=today) == "15. apr"


def test_short_date_other_year_includes_year():
    today = datetime(2026, 5, 22)
    assert short_date(datetime(2010, 6, 11), today=today) == "11. jun 2010"


def test_short_date_returns_empty_for_none():
    assert short_date(None) == ""


def test_short_date_uses_real_today_by_default():
    """Sanity: without an explicit `today`, the filter uses the actual
    calendar — last year's date should include the year."""
    last_year_date = datetime(datetime.now().year - 1, 6, 11)
    out = short_date(last_year_date)
    assert str(last_year_date.year) in out


def test_relative_day_today():
    today = date(2026, 5, 22)
    assert relative_day(today, today=today) == "i dag"


def test_relative_day_tomorrow_and_overmorrow():
    today = date(2026, 5, 22)  # Friday
    assert relative_day(date(2026, 5, 23), today=today) == "i morgen"
    assert relative_day(date(2026, 5, 24), today=today) == "i overmorgen"


def test_relative_day_within_week_returns_weekday():
    """Days 3-6 ahead → weekday name only, e.g. 'tor'."""
    today = date(2026, 5, 22)  # Friday
    # +3 days = Mon
    assert relative_day(date(2026, 5, 25), today=today) == "man"
    # +5 days = Wed
    assert relative_day(date(2026, 5, 27), today=today) == "ons"


def test_relative_day_8_to_13_days_includes_day_of_month():
    """Far enough that the weekday alone is ambiguous; include the day-of-month."""
    today = date(2026, 5, 22)  # Friday
    # +8 days = Sat 30
    assert relative_day(date(2026, 5, 30), today=today) == "lør 30"
    # +13 days = Thu 4
    assert relative_day(date(2026, 6, 4), today=today) == "tor 4"


def test_relative_day_beyond_two_weeks_returns_short_form():
    """Far out → '14. jun' format (no year, no leading zero on day)."""
    today = date(2026, 5, 22)
    assert relative_day(date(2026, 6, 14), today=today) == "14. jun"


def test_tankekart_template_uses_short_date_filter():
    """The template must use the year-aware filter, not raw strftime —
    otherwise the year disappears from the rendered card."""
    tpl = Path(__file__).parent / "templates" / "_tankekart.html"
    text = tpl.read_text()
    assert "r.date.strftime" not in text, (
        "raw r.date.strftime drops the year for non-current-year mails"
    )
    assert "short_date" in text, (
        "_tankekart.html should pipe r.date through the short_date filter"
    )
