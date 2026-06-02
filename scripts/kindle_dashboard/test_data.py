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
