"""Jinja date helpers.

`short_date` formats a datetime for compact card display. The year is
omitted when the date is in the current calendar year and included
otherwise — Gmail / Apple-Mail style. Without this, a mail from 2010
rendered as just "11. jun", indistinguishable from a recent mail.

Locale: the month abbreviation comes from `datetime.strftime('%b')`,
which honours the process locale. Norwegian and English share the same
3-letter abbreviation for most months (Jan, Feb, Mar, …), so leaving
the locale alone is fine here.
"""
from __future__ import annotations

from datetime import datetime


def short_date(dt: datetime | None, *, today: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = today if today is not None else datetime.now()
    if dt.year == now.year:
        return dt.strftime("%d. %b").lower()
    return dt.strftime("%d. %b %Y").lower()
