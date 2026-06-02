#!/usr/bin/env python3
"""Move expired one-off calendar events from CALENDAR.md to CALENDAR-PAST.md.

Both files share the parser-contract in `CALENDAR-RULES.md`. This script
operates only inside the `## One-off events by month` section:

  - Each line matching `- **<ISO date>[<span/time>...] ** — <title>...` is an
    event. The expiry-check uses the end-date (= start-date when no span).
  - Lines whose end-date is strictly before `--today` are cut from
    CALENDAR.md and inserted into CALENDAR-PAST.md under the corresponding
    `### <Month> <Year>` subheading, creating it (in chronological order)
    if missing.
  - `### Month Year` subheadings that end up empty in CALENDAR.md after the
    cut are dropped.

Everything else — recurring sections, prose, the `## Pågående …` block,
the file header — is left untouched. When nothing is expired, both files
are byte-identical on disk (idempotent).

Usage::

    uv run scripts/retire_calendar.py                    # today = date.today()
    uv run scripts/retire_calendar.py --today 2026-06-01
    uv run scripts/retire_calendar.py --dry-run
    uv run scripts/retire_calendar.py --src foo.md --dst bar.md
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
import sys
from pathlib import Path

from mail_reader.config import workspace_root

ROOT = workspace_root()
SRC_DEFAULT = ROOT / "CALENDAR.md"
DST_DEFAULT = ROOT / "CALENDAR-PAST.md"

ONE_OFF_HEADING = "## One-off events by month"
MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_INDEX = {name: i for i, name in enumerate(MONTHS_EN, start=1)}

EVENT_RE = re.compile(
    r"^-\s\*\*"
    r"(?P<start>\d{4}-\d{2}-\d{2})"
    r"(?:\s*–\s*(?P<end>\d{4}-\d{2}-\d{2}))?"
    r".*?\*\*\s*—\s*"
)
H3_RE = re.compile(r"^###\s+([A-Za-z]+)\s+(\d{4})\s*$")
H2_RE = re.compile(r"^##\s")


def event_dates(line: str) -> tuple[dt.date, dt.date] | None:
    """If `line` is a parseable event line, return (start_date, end_date).

    >>> event_dates("- **2026-05-26 17:30** — Eksempel-IL kamp")
    (datetime.date(2026, 5, 26), datetime.date(2026, 5, 26))
    >>> event_dates("- **2026-06-29 – 2026-07-03 (uke 27)** — Sommerskolen")
    (datetime.date(2026, 6, 29), datetime.date(2026, 7, 3))
    >>> event_dates("- **2026-05-29 (all day)** — Planleggingsdag")
    (datetime.date(2026, 5, 29), datetime.date(2026, 5, 29))
    >>> event_dates("- **Bulder bank-ingest** (siste søndag — neste: 2026-06-28).") is None
    True
    >>> event_dates("Just prose.") is None
    True
    """
    m = EVENT_RE.match(line)
    if not m:
        return None
    start = dt.date.fromisoformat(m.group("start"))
    end = dt.date.fromisoformat(m.group("end")) if m.group("end") else start
    return start, end


@dataclasses.dataclass
class MonthSection:
    """One `### <Month> <Year>` subsection inside `## One-off events by month`.

    `lines` holds every raw line between the heading and the next heading
    (or end of section), *including* leading/trailing blank lines. Event
    lines are identified by `event_dates(...)` — non-event content (blanks,
    occasional prose) is preserved verbatim.
    """

    month: str
    year: int
    lines: list[str]

    @property
    def key(self) -> tuple[int, int]:
        return (self.year, MONTH_INDEX[self.month])

    def event_indices(self) -> list[int]:
        return [i for i, ln in enumerate(self.lines) if event_dates(ln) is not None]

    def is_empty(self) -> bool:
        """True iff the section has no event lines AND no non-blank prose."""
        return not any(ln.strip() for ln in self.lines)

    def insert_event_sorted(self, line: str) -> None:
        """Insert `line` keeping event lines in ascending start-date order."""
        new_start, _ = event_dates(line)  # type: ignore[misc]
        # Find the position: after the last event whose start <= new_start.
        insert_at = None
        for i, ln in enumerate(self.lines):
            d = event_dates(ln)
            if d is None:
                continue
            if d[0] > new_start:
                insert_at = i
                break
        if insert_at is None:
            # Append at end of event block (before any trailing blank lines).
            tail_blanks = 0
            for ln in reversed(self.lines):
                if ln.strip() == "":
                    tail_blanks += 1
                else:
                    break
            insert_at = len(self.lines) - tail_blanks
        self.lines.insert(insert_at, line)


@dataclasses.dataclass
class Document:
    head: list[str]  # lines before `## One-off events by month`
    intro: list[str]  # the heading line + any intro prose up to (not incl.) first ### subsection
    sections: list[MonthSection]
    tail: list[str]  # lines from the next `## ` heading onward (often empty)

    def render(self) -> str:
        out: list[str] = []
        out.extend(self.head)
        out.extend(self.intro)
        for i, s in enumerate(self.sections):
            out.append(f"### {s.month} {s.year}\n")
            out.extend(s.lines)
        out.extend(self.tail)
        return "".join(out)


def parse(text: str) -> Document:
    """Split a calendar markdown file into Document parts.

    >>> doc = parse("# Top\\n\\n## One-off events by month\\n\\nIntro.\\n\\n### May 2026\\n\\n- **2026-05-01** — a\\n")
    >>> [s.month for s in doc.sections]
    ['May']
    >>> doc.sections[0].lines
    ['\\n', '- **2026-05-01** — a\\n']
    """
    lines = text.splitlines(keepends=True)
    head: list[str] = []
    intro: list[str] = []
    sections: list[MonthSection] = []
    tail: list[str] = []

    # Phase 1: scan for ONE_OFF_HEADING.
    i = 0
    n = len(lines)
    while i < n and lines[i].rstrip("\n") != ONE_OFF_HEADING:
        head.append(lines[i])
        i += 1
    if i == n:
        # No One-off section found. Treat everything as head; no sections.
        return Document(head=head, intro=[], sections=[], tail=[])

    intro.append(lines[i])  # the heading itself
    i += 1

    # Phase 2: collect intro lines until the first `### Month Year` or next `##`.
    while i < n:
        ln = lines[i]
        if H3_RE.match(ln) or H2_RE.match(ln):
            break
        intro.append(ln)
        i += 1

    # Phase 3: parse `### Month Year` subsections until next `## ` heading or EOF.
    current: MonthSection | None = None
    while i < n:
        ln = lines[i]
        if H2_RE.match(ln):
            break  # tail starts here
        m = H3_RE.match(ln)
        if m:
            if current is not None:
                sections.append(current)
            month, year = m.group(1), int(m.group(2))
            if month not in MONTH_INDEX:
                # Unknown month name — treat heading line as belonging to current
                # section (preserve verbatim).
                if current is None:
                    intro.append(ln)
                else:
                    current.lines.append(ln)
                i += 1
                continue
            current = MonthSection(month=month, year=year, lines=[])
            i += 1
            continue
        if current is None:
            intro.append(ln)
        else:
            current.lines.append(ln)
        i += 1
    if current is not None:
        sections.append(current)

    # Phase 4: everything from here is tail.
    while i < n:
        tail.append(lines[i])
        i += 1

    return Document(head=head, intro=intro, sections=sections, tail=tail)


def cull_empty_sections(doc: Document) -> None:
    """Drop `### Month Year` subsections whose body is entirely blank.

    Keeps subsections that still hold events or non-event prose.
    """
    doc.sections = [s for s in doc.sections if not s.is_empty()]


def ensure_section(doc: Document, month: str, year: int) -> MonthSection:
    """Return the MonthSection for (month, year), inserting one in chronological
    order if it doesn't exist. New sections start with one blank line so
    rendering produces `### Month Year\\n\\n` followed by events.
    """
    for s in doc.sections:
        if s.month == month and s.year == year:
            return s
    new_section = MonthSection(month=month, year=year, lines=["\n"])
    new_key = (year, MONTH_INDEX[month])
    insert_at = len(doc.sections)
    for i, s in enumerate(doc.sections):
        if s.key > new_key:
            insert_at = i
            break
    doc.sections.insert(insert_at, new_section)
    return new_section


def retire(
    src_text: str,
    dst_text: str,
    today: dt.date,
) -> tuple[str, str, list[str]]:
    """Return `(new_src_text, new_dst_text, moved_lines)`.

    `moved_lines` lists the event lines (with trailing newline) that were
    cut from src and added to dst.
    """
    src_doc = parse(src_text)
    dst_doc = parse(dst_text)

    moved: list[tuple[dt.date, dt.date, str]] = []
    for s in src_doc.sections:
        keep: list[str] = []
        for ln in s.lines:
            d = event_dates(ln)
            if d is not None and d[1] < today:
                moved.append((d[0], d[1], ln))
            else:
                keep.append(ln)
        s.lines = keep

    cull_empty_sections(src_doc)

    # Insert into destination, grouped by end-date's (year, month).
    for start, end, line in moved:
        month = MONTHS_EN[end.month - 1]
        section = ensure_section(dst_doc, month, end.year)
        section.insert_event_sorted(line)

    new_src = src_doc.render()
    new_dst = dst_doc.render()
    return new_src, new_dst, [line for _, _, line in moved]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today(),
                    help="Date to compare against (YYYY-MM-DD); default = today.")
    ap.add_argument("--src", type=Path, default=SRC_DEFAULT, help=f"Source calendar (default: {SRC_DEFAULT.name}).")
    ap.add_argument("--dst", type=Path, default=DST_DEFAULT, help=f"Past calendar (default: {DST_DEFAULT.name}).")
    ap.add_argument("--dry-run", action="store_true", help="Print what would move; do not write files.")
    args = ap.parse_args(argv)

    src_text = args.src.read_text()
    dst_text = args.dst.read_text()
    new_src, new_dst, moved = retire(src_text, dst_text, args.today)

    if not moved:
        print(f"retire_calendar: nothing expired before {args.today.isoformat()}.", file=sys.stderr)
        return 0

    print(f"retire_calendar: {len(moved)} event(s) expired before {args.today.isoformat()}:", file=sys.stderr)
    for line in moved:
        print(f"  - {line.rstrip()}", file=sys.stderr)

    if args.dry_run:
        print("retire_calendar: --dry-run, not writing.", file=sys.stderr)
        return 0

    if new_src != src_text:
        args.src.write_text(new_src)
    if new_dst != dst_text:
        args.dst.write_text(new_dst)
    print(f"retire_calendar: wrote {args.src.name} and {args.dst.name}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
