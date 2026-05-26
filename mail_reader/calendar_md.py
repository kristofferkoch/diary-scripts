"""Parse CALENDAR.md / CALENDAR-PAST.md per the parser-kontrakt
documented in CALENDAR-RULES.md.

A parseable event line looks like:

    - **<ISO-date>[ <time|range>][ (<note>)]** — <title>[. fri tekst.]

Where `<ISO-date>` is `YYYY-MM-DD` (optionally a `YYYY-MM-DD – YYYY-MM-DD`
span with en-dash), `<time|range>` is `HH:MM` or `HH:MM–HH:MM`, and
parenthetical hints (`(onsdag)`, `(uke 27)`, `(all day)`) are ignored.
Anything that doesn't match (prose paragraphs, recurring weekly nested
bullets, "Pågående"-sections) is skipped without error.

The full body text after `— ` is preserved for context display; the
title is sniffed out (leading `**bold**`, or text up to first `—`/sentence-end).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path

import markdown as md_lib


_ISO = r"\d{4}-\d{2}-\d{2}"
_HHMM = r"\d{2}:\d{2}"

_LINE = re.compile(r"^- \*\*([^*]+)\*\*\s+—\s+(.*)$")
_DATE_BLOCK = re.compile(
    rf"^(?P<start>{_ISO})"
    rf"(?:\s+–\s+(?P<end>{_ISO}))?"
    rf"(?:\s+(?P<t1>{_HHMM})(?:–(?P<t2>{_HHMM}))?)?"
    rf"(?:\s+\(.*\))?\s*$"
)

_LEADING_BOLD = re.compile(r"^\*\*([^*]+?)\*\*\s*(.*)$")
# Period + space + capital letter = real sentence break. Avoids breaking
# Norwegian ordinals like "2. trinn" / "2. pinsedag".
_SENTENCE_BREAK = re.compile(r"\. (?=[A-ZÆØÅ])")

_NO_MONTHS = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]
_NO_WEEKDAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]


def no_month_name(d: date) -> str:
    """Norwegian month + year, used as group heading in the calendar view.

    >>> no_month_name(date(2026, 5, 27))
    'mai 2026'
    >>> no_month_name(date(2027, 2, 1))
    'februar 2027'
    """
    return f"{_NO_MONTHS[d.month - 1]} {d.year}"


def no_weekday(d: date) -> str:
    """Three-letter Norwegian weekday.

    >>> no_weekday(date(2026, 5, 27))
    'ons'
    >>> no_weekday(date(2026, 5, 26))
    'tir'
    """
    return _NO_WEEKDAYS[d.weekday()]


def upcoming_by_day(
    events: list["CalEvent"],
    *,
    today: date,
    horizon_days: int = 14,
) -> list[tuple[date, list["CalEvent"]]]:
    """Group events occurring in [today, today+horizon_days) by their date.

    A multi-day event (e.g. Norway Cup spanning Fri–Sat) is attached to
    its `start_date` only — it shows up once per occurrence, not once
    per day in its span. Empty days are *not* included; only days that
    actually have events get a slot.

    >>> from datetime import date
    >>> evs = [
    ...     CalEvent(date(2026, 5, 27), None, None, None, "A", "A", "x"),
    ...     CalEvent(date(2026, 5, 27), None, None, None, "B", "B", "x"),
    ...     CalEvent(date(2026, 5, 29), None, None, None, "C", "C", "x"),
    ...     CalEvent(date(2026, 6, 20), None, None, None, "far-out", "", "x"),
    ... ]
    >>> [(d.isoformat(), [e.title for e in es])
    ...  for d, es in upcoming_by_day(evs, today=date(2026, 5, 27), horizon_days=14)]
    [('2026-05-27', ['A', 'B']), ('2026-05-29', ['C'])]
    """
    end = today + timedelta(days=horizon_days)
    by_day: dict[date, list[CalEvent]] = {}
    for ev in events:
        if today <= ev.start_date < end:
            by_day.setdefault(ev.start_date, []).append(ev)
    return sorted(by_day.items(), key=lambda kv: kv[0])


def render_markdown(path: Path) -> str:
    """Render `path` as HTML, with sensible extensions for the calendar files.

    The file header (title, intro paragraphs, blockquote pointers like
    "see CALENDAR-RULES.md") is stripped — those have dead repo-relative
    links and stale "Source:"-blurbs that aren't useful in the web view.
    Content starts at the first `## ` heading.

    Returns an empty string if the file is missing. Wiki-links like
    `[[memory/2026-05-23.md]]` pass through untouched (rendered as
    literal text) — they're internal refs not meaningful here.
    """
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    idx = text.find("\n## ")
    if idx > 0:
        text = text[idx + 1:]  # +1 skips the leading newline
    return md_lib.markdown(
        text,
        extensions=["extra", "sane_lists", "tables"],
        output_format="html",
    )


def group_by_month(events: list["CalEvent"]) -> list[tuple[str, list["CalEvent"]]]:
    """Group events into [(month-heading, events-in-month), ...] preserving order.

    >>> from datetime import date
    >>> evs = [
    ...     CalEvent(date(2026, 5, 27), None, None, None, "A", "A", "x"),
    ...     CalEvent(date(2026, 5, 29), None, None, None, "B", "B", "x"),
    ...     CalEvent(date(2026, 6, 2), None, None, None, "C", "C", "x"),
    ... ]
    >>> [(h, [e.title for e in es]) for h, es in group_by_month(evs)]
    [('mai 2026', ['A', 'B']), ('juni 2026', ['C'])]
    """
    out: list[tuple[str, list[CalEvent]]] = []
    for ev in events:
        head = no_month_name(ev.start_date)
        if not out or out[-1][0] != head:
            out.append((head, []))
        out[-1][1].append(ev)
    return out


@dataclass(frozen=True)
class CalEvent:
    start_date: date
    end_date: date | None       # None ⇒ single day
    start_time: time | None     # None ⇒ all-day
    end_time: time | None
    title: str
    body: str                   # full text after the em-dash separator
    source: str                 # filename, e.g. "CALENDAR.md"

    @property
    def is_all_day(self) -> bool:
        return self.start_time is None

    @property
    def is_range(self) -> bool:
        return self.end_date is not None


def parse_calendar(path: Path) -> list[CalEvent]:
    """Read `path` and return events sorted by (start_date, start_time).

    Missing files return `[]` — callers don't need to pre-check.
    """
    if not path.exists():
        return []
    out: list[CalEvent] = []
    src = path.name
    for raw in path.read_text(encoding="utf-8").splitlines():
        ev = _parse_line(raw, src)
        if ev is not None:
            out.append(ev)
    out.sort(key=lambda e: (e.start_date, e.start_time or time.min))
    return out


def _parse_line(raw: str, source: str) -> CalEvent | None:
    """Parse one markdown line; return None if it doesn't match the contract.

    Single date + time range:

    >>> ev = _parse_line("- **2026-05-27 10:10–10:40** — Foreldresamtale Bjorn (Eksempeldalen).", "x")
    >>> ev.start_date, ev.start_time, ev.end_time
    (datetime.date(2026, 5, 27), datetime.time(10, 10), datetime.time(10, 40))
    >>> ev.title
    'Foreldresamtale Bjorn'

    Single date + single time, with **bold** title:

    >>> ev = _parse_line("- **2026-05-27 11:00** — **Lunsj på Baltazar** (Kirkeristen).", "x")
    >>> ev.start_time, ev.end_time, ev.title
    (datetime.time(11, 0), None, 'Lunsj på Baltazar')

    All-day with weekday hint in parens (ignored by parser):

    >>> ev = _parse_line("- **2026-05-27 (onsdag)** — Bamsesykehus 2. trinn (Robin).", "x")
    >>> ev.is_all_day, ev.title
    (True, 'Bamsesykehus 2. trinn')

    Explicit `(all day)`:

    >>> ev = _parse_line("- **2026-05-25 (all day)** — 2. pinsedag, skolefri (Robin)", "x")
    >>> ev.is_all_day, ev.title
    (True, '2. pinsedag, skolefri')

    Multi-day range with `(uke N)` annotation:

    >>> ev = _parse_line("- **2026-06-29 – 2026-07-03 (uke 27)** — Sommerskolen.", "x")
    >>> ev.start_date, ev.end_date, ev.is_range, ev.is_all_day
    (datetime.date(2026, 6, 29), datetime.date(2026, 7, 3), True, True)

    Body with a second em-dash (used for subtitle/context):

    >>> _parse_line("- **2026-04-28 08:30** — Whee service — *(online: meet…)*", "x").title
    'Whee service'

    Lines that don't match the contract return None:

    >>> _parse_line("This is just prose.", "x") is None
    True
    >>> _parse_line("- **Neste forfall: 2026-08-22.** Verktøy.", "x") is None
    True
    >>> _parse_line("  - **Mandager 17.15–18.45** — Treningsfelt B2", "x") is None
    True
    >>> _parse_line("### May 2026", "x") is None
    True
    """
    m = _LINE.match(raw.rstrip())
    if not m:
        return None
    bold = m.group(1).strip()
    rest = m.group(2).strip()
    d = _DATE_BLOCK.match(bold)
    if not d:
        return None
    try:
        start_date = date.fromisoformat(d.group("start"))
    except ValueError:
        return None
    end_raw = d.group("end")
    end_date = date.fromisoformat(end_raw) if end_raw else None
    t1_raw, t2_raw = d.group("t1"), d.group("t2")
    start_time = time.fromisoformat(t1_raw) if t1_raw else None
    end_time = time.fromisoformat(t2_raw) if t2_raw else None
    title = _extract_title(rest)
    return CalEvent(
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        title=title,
        body=rest,
        source=source,
    )


def _extract_title(rest: str) -> str:
    """Sniff a short title from the body text after the em-dash separator.

    Leading `**bold**` wins:

    >>> _extract_title("**Norway Cup 2026** (Eksempelhøgdasletta)")
    'Norway Cup 2026'

    Otherwise stop at em-dash subtitle separator or parenthetical:

    >>> _extract_title("Whee service — *(online: meet…)*")
    'Whee service'
    >>> _extract_title("Bamsesykehus 2. trinn (Robin).")
    'Bamsesykehus 2. trinn'

    Sentence-break splitting requires a capital letter so Norwegian
    ordinals like "2. trinn" don't get truncated:

    >>> _extract_title("Sommerskolen. Mer info kommer.")
    'Sommerskolen'
    >>> _extract_title("2. pinsedag, skolefri")
    '2. pinsedag, skolefri'

    Plain titles pass through unchanged:

    >>> _extract_title("Genfors brageveien")
    'Genfors brageveien'
    >>> _extract_title("Uteskole Robin")
    'Uteskole Robin'
    """
    m = _LEADING_BOLD.match(rest)
    if m:
        return m.group(1).strip().rstrip(".")
    candidates: list[int] = []
    for sep in (" — ", " ("):
        i = rest.find(sep)
        if i > 0:
            candidates.append(i)
    sm = _SENTENCE_BREAK.search(rest)
    if sm:
        candidates.append(sm.start())
    if candidates:
        return rest[:min(candidates)].strip().rstrip(".")
    return rest.strip().rstrip(".")
