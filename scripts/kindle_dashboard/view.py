"""Build the Jinja context for the dashboard template.

This is the seam between "raw data sources" (calendar, spond, weather)
and "what the template needs". Each data collector returns a small
dict-of-dicts shape that the template iterates over.

Collectors live in scripts.kindle_dashboard.data — this file just glues
them together with sensible empty defaults so the template still renders
when one source is broken or missing.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

log = logging.getLogger("kindle_dashboard.view")

_NB_WEEKDAYS = [
    "Mandag", "Tirsdag", "Onsdag", "Torsdag",
    "Fredag", "Lørdag", "Søndag",
]
_NB_MONTHS = [
    "", "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]


def _nb_date(d: _dt.date) -> str:
    return f"{_NB_WEEKDAYS[d.weekday()]} {d.day}. {_NB_MONTHS[d.month]}"


def build_context(*, now: _dt.datetime | None = None) -> dict[str, Any]:
    now = now or _dt.datetime.now()
    today = now.date()

    ctx: dict[str, Any] = {
        "today_label": _nb_date(today),
        "week": today.isocalendar().week,
        "rendered_at": now.strftime("%H:%M"),
        "build_tag": now.strftime("%Y-%m-%d %H:%M"),
        "days": [],
        "spond": [],
        "weather": None,
    }

    # Calendar — today + tomorrow only in the agenda (the mini-month grid
    # next to it already gives the 30-day overview).
    try:
        from . import data

        ctx["days"] = data.calendar_block(today, days_ahead=1)
    except Exception:
        log.exception("calendar collector failed")
        ctx["days"] = [
            {"label": _nb_date(today + _dt.timedelta(days=i)), "events": []}
            for i in range(2)
        ]

    try:
        from . import data

        ctx["month"] = data.month_grid(today)
    except Exception:
        log.exception("month collector failed")
        ctx["month"] = None

    try:
        from . import data

        ctx["spond"] = data.spond_block()
    except Exception:
        log.exception("spond collector failed")
        ctx["spond"] = []

    try:
        from . import data

        ctx["weather"] = data.weather_block()
    except Exception:
        log.exception("weather collector failed")
        ctx["weather"] = None

    try:
        from . import data

        ctx["sun"] = data.sun_block()
    except Exception:
        log.exception("sun collector failed")
        ctx["sun"] = None

    try:
        from . import data

        ctx["nowcast"] = data.nowcast_block()
    except Exception:
        log.exception("nowcast collector failed")
        ctx["nowcast"] = None

    return ctx
