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

Locale: month abbreviations are taken from an explicit Norwegian table
(`_NO_MONTHS`), **not** `strftime('%b')`. The process locale is typically
C/en, which renders "may"/"oct"/"dec" instead of the Norwegian
"mai"/"okt"/"des" — wrong on a Norwegian page (notes page bug, user
2026-05-31). Hard-coding the table makes rendering locale-independent.
"""
from __future__ import annotations

from datetime import date, datetime

# Norwegian 3-letter month abbreviations, 1-indexed (index 0 unused).
_NO_MONTHS = ["", "jan", "feb", "mar", "apr", "mai", "jun",
              "jul", "aug", "sep", "okt", "nov", "des"]


def short_date(dt: datetime | None, *, today: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = today if today is not None else datetime.now()
    mon = _NO_MONTHS[dt.month]
    if dt.year == now.year:
        return f"{dt.day:02d}. {mon}"
    return f"{dt.day:02d}. {mon} {dt.year}"


def short_datetime(dt: datetime | None, *, today: datetime | None = None) -> str:
    """`short_date` plus a Norwegian-style time.

    Used on the notes page where several notes are often captured the same
    day and the date alone can't tell them apart (user, notes-queue #26).

    >>> short_datetime(datetime(2026, 5, 31, 14, 7), today=datetime(2026, 5, 22))
    '31. mai 14:07'
    >>> short_datetime(datetime(2026, 5, 31, 7, 30), today=datetime(2026, 5, 22))
    '31. mai 07:30'
    >>> short_datetime(None)
    ''
    """
    if dt is None:
        return ""
    return f"{short_date(dt, today=today)} {dt.hour:02d}:{dt.minute:02d}"


_NO_WEEKDAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
_NO_WEEKDAYS_FULL = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


def relative_day(d: date, *, today: date | None = None) -> str:
    """Norwegian relative day name for an agenda card.

    - today              → "i dag"
    - tomorrow           → "i morgen"
    - day after          → full weekday ("torsdag") — kept short to
                           avoid wrapping in narrow agenda/calendar slots
    - within 7 days      → 3-letter weekday ("tor")
    - within 14-ish days → 3-letter weekday + day-of-month ("tor 30")
    - further out        → "5. jun" (short_date style)
    """
    ref = today if today is not None else date.today()
    delta = (d - ref).days
    if delta == 0:
        return "i dag"
    if delta == 1:
        return "i morgen"
    if delta == 2:
        return _NO_WEEKDAYS_FULL[d.weekday()]
    if 0 < delta < 7:
        return _NO_WEEKDAYS[d.weekday()]
    if 0 < delta < 14:
        return f"{_NO_WEEKDAYS[d.weekday()]} {d.day}"
    return f"{d.day}. {_NO_MONTHS[d.month]}"
