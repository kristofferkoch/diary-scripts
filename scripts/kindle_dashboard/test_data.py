"""Tests for the dashboard calendar collectors (kindle_dashboard/data.py)."""
from __future__ import annotations

import datetime as _dt

from scripts.kindle_dashboard import data

RECURRING_MD = """\
# Calendar

## Recurring weekly

- **Fotballtrening Hans** (KFUM G2018, Lag 2):
  - **Mandager 17.15–18.45** — Treningsfelt B2
  - **Lørdager 10.30–12.00** — Kunstgresset
  - Kilde: Spond-post 2026-05-03.

- **Svømming Hans** (Lambertseter svømmeklubb):
  - **Tirsdager 17.35–18.05** — Rustad skole, Paal Bergs vei 30

## Recurring — månedlig

- **Bulder bank-ingest** (siste søndag i måneden):
  - **Mandager 09.00** — skal IKKE plukkes opp (feil seksjon)

## One-off events by month

- **2026-06-02 19:00** — **Et engangsmøte** — fri tekst
"""


def test_parse_recurring_weekly_extracts_slots():
    recs = data.parse_recurring_weekly(RECURRING_MD)
    # 3 slots: Mon + Sat football, Tue swimming. The monthly-section bullet
    # must NOT leak in.
    keyed = {(r["weekday"], r["time"]): r["title"] for r in recs}
    assert keyed == {
        (0, "17:15–18:45"): "Fotballtrening Hans",  # Monday
        (5, "10:30–12:00"): "Fotballtrening Hans",  # Saturday
        (1, "17:35–18:05"): "Svømming Hans",         # Tuesday
    }


def test_recurring_excludes_monthly_section():
    """The `Mandager 09.00` bullet lives under `## Recurring — månedlig`."""
    recs = data.parse_recurring_weekly(RECURRING_MD)
    assert all(r["time"] != "09:00" for r in recs)


def test_prose_subbullets_are_skipped():
    recs = data.parse_recurring_weekly(RECURRING_MD)
    assert all("Kilde" not in r["title"] for r in recs)


def test_calendar_block_shows_recurring_on_matching_weekday(tmp_path, monkeypatch):
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(RECURRING_MD)
    monkeypatch.setattr(data, "CALENDAR_MD", cal)

    tuesday = _dt.date(2026, 6, 2)  # a Tuesday
    block = data.calendar_block(tuesday, days_ahead=0)
    texts = {(ev["time"], ev["text"]) for ev in block[0]["events"]}
    assert ("17:35–18:05", "Svømming Hans") in texts   # recurring Tuesday slot
    assert ("19:00", "Et engangsmøte") in texts         # one-off still present


def test_calendar_block_no_recurring_on_other_weekday(tmp_path, monkeypatch):
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(RECURRING_MD)
    monkeypatch.setattr(data, "CALENDAR_MD", cal)

    wednesday = _dt.date(2026, 6, 3)
    block = data.calendar_block(wednesday, days_ahead=0)
    assert all("Svømming" not in ev["text"] for ev in block[0]["events"])


def test_month_grid_counts_events_per_day(tmp_path, monkeypatch):
    """Busy-day dots scale with event count, capped at 3."""
    cal = tmp_path / "CALENDAR.md"
    cal.write_text(
        "## One-off events by month\n\n"
        "- **2026-06-02 09:00** — **A** — x\n"
        "- **2026-06-02 12:00** — **B** — y\n"
        "- **2026-06-02 17:00** — **C** — z\n"
        "- **2026-06-02 19:00** — **D** — w\n"   # 4 events on the 2nd -> capped at 3
        "- **2026-06-05 10:00** — **E** — q\n"   # single event
    )
    monkeypatch.setattr(data, "CALENDAR_MD", cal)
    grid = data.month_grid(_dt.date(2026, 6, 1))
    by_day = {c["day"]: c for week in grid["weeks"] for c in week if c["in_month"]}
    assert by_day[2]["event_count"] == 3        # 4 events, capped at 3
    assert by_day[2]["has_events"] is True
    assert by_day[5]["event_count"] == 1
    assert by_day[3]["event_count"] == 0
    assert by_day[3]["has_events"] is False


def test_every_weather_icon_has_a_norwegian_label():
    """Every yr.no symbol we ship an icon for must have a Norwegian label.

    Regression for notat #307 (2026-06-04): a missing entry made _label_for
    leak the raw code ("heavyrainandthunder") onto the wall display.
    """
    base_codes = {
        p.stem.rsplit("_", 1)[0]
        if p.stem.rsplit("_", 1)[-1] in ("day", "night", "polartwilight")
        else p.stem
        for p in data.YR_ICONS_DIR.glob("*.svg")
    }
    missing = sorted(c for c in base_codes if c not in data._SYMBOL_LABELS)
    assert not missing, f"weather codes without a Norwegian label: {missing}"


def test_label_for_strips_time_suffix_and_never_returns_raw_code():
    assert data._label_for("heavyrainandthunder_day") == "Kraftig regn og torden"
    assert data._label_for("partlycloudy_night") == "Halvskyet"


def test_ordinal_period_does_not_truncate_title_or_drop_badge(monkeypatch):
    """Regression: an ordinal title like "Avslutning 2. trinn (Robin)" was
    truncated at the "2." ordinal to "Avslutning 2", losing both the headline
    and the trailing name badge. An ordinal/decimal period must not count as a
    sentence break."""
    monkeypatch.setattr(data, "FAMILY_MEMBERS", ("Robin", "Bjorn", "Carl"))
    line = (
        "- **2026-06-09 17:00** — Avslutning 2. trinn (Robin). "
        "Sang og høytlesing kl. 17:00."
    )
    (ev,) = data.parse_calendar(line)
    assert ev["title"] == "Avslutning 2. trinn (Robin)"
    assert ev["who"] == "R"


def test_real_sentence_period_still_truncates():
    """A genuine sentence boundary (period after a non-digit) still trims."""
    assert (
        data._clean_title("Vennegruppe hos Kari. Foreldre: Per + Pål.")
        == "Vennegruppe hos Kari"
    )
