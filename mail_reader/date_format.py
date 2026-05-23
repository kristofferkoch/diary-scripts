"""Jinja date helpers.

`short_date` formats a datetime for compact card display. The year is
omitted when the date is in the current calendar year and included
otherwise — Gmail / Apple-Mail style. Without this, a mail from 2010
rendered as just "11. jun", indistinguishable from a recent mail.

`relative_day` formats a *date* in Norwegian relative form for the
agenda strip: "i dag", "i morgen", "tor" (within current week), or
"tor 30" (further out). Distinct from `short_date` (datetimes, paper
style); kept in the same module so all date-rendering rules live in
one place.

Locale: month abbreviations come from `strftime('%b')`, which honours
the process locale. Norwegian and English share the same 3-letter
abbreviation for most months, so leaving the locale alone is fine.
"""
from __future__ import annotations

from datetime import date, datetime


def short_date(dt: datetime | None, *, today: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = today if today is not None else datetime.now()
    if dt.year == now.year:
        return dt.strftime("%d. %b").lower()
    return dt.strftime("%d. %b %Y").lower()


_NO_WEEKDAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]


def relative_day(d: date, *, today: date | None = None) -> str:
    """Norwegian relative day name for an agenda card.

    - today              → "i dag"
    - tomorrow           → "i morgen"
    - day after          → "i overmorgen"
    - within 7 days      → weekday name ("tor")
    - within 14-ish days → weekday + day-of-month ("tor 30")
    - further out        → "5. jun" (short_date style)
    """
    ref = today if today is not None else date.today()
    delta = (d - ref).days
    if delta == 0:
        return "i dag"
    if delta == 1:
        return "i morgen"
    if delta == 2:
        return "i overmorgen"
    if 0 < delta < 7:
        return _NO_WEEKDAYS[d.weekday()]
    if 0 < delta < 14:
        return f"{_NO_WEEKDAYS[d.weekday()]} {d.day}"
    return d.strftime("%d. %b").lower().lstrip("0")
