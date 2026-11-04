#!/usr/bin/env python3
"""Sjekk triage cursor — the sjekk's own progress marker, and nothing else.

Three different cursors used to share one file, and that coupling was the bug:

* the **collector's fetch watermark** (how far a sync got) lives in trusted
  collector state and is private to the collector;
* the **bundle window** (what one collect run showed) is ephemeral and is
  recorded in that run's ``manifest.json``;
* the **triage cursor** (what the sjekk has actually processed) is this file.

Rules that keep the three apart:

* ``sjekk-collect`` reads the cursor to pick a window, and writes nothing here.
* ``sjekk-advance`` writes it, after triage, per track.
* readers (``mailshow``, ``signalshow``, ``spondshow``, ``vigiloshown``,
  ``skolemeldingshow``) never read or write it: they take an explicit
  ``--since/--until`` range.
* collectors (``*-sync``) never touch it.

The stored value is the **upper bound of the last triaged window**, not the
newest record shown. A window is half-open ``(since, until]`` over a record's
collection time; mail is the one track compared by its ``Date:`` header, which
can be far older than arrival, so mail windows are widened by
``MAIL_OVERLAP`` and the ids shown in the overlap are carried in the cursor so
the next window can skip them.

Location: ``<workspace>/memory/sjekk/cursor.json`` — git-ignored, next to the
bundles, backed up with the host, and writable from an agent session. Losing it
costs one replayed window, which triage is expected to tolerate.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mail_reader.config import workspace_root

TRACKS = ("mail", "signal", "spond", "vigilo", "skolemelding")

# Mail is filtered on the Date header, which can predate arrival by days
# (delayed delivery, lists, resends). Every other sink records the time it was
# fetched, so an exact window is enough there.
MAIL_OVERLAP = timedelta(hours=24)

SCHEMA_VERSION = 1


def parse_iso(value: str) -> datetime:
    """Parse ISO-8601 (date or datetime, ``Z`` or offset) into aware UTC.

    >>> parse_iso("2026-11-02T09:00:00Z").isoformat()
    '2026-11-02T09:00:00+00:00'
    >>> parse_iso("2026-11-02").isoformat()
    '2026-11-02T00:00:00+00:00'
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_iso(dt: datetime) -> str:
    """Serialise to the second-resolution UTC form the sinks use.

    >>> format_iso(parse_iso("2026-11-02T09:00:00+01:00"))
    '2026-11-02T08:00:00Z'
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_dir(root: Path | None = None) -> Path:
    """The sjekk runtime directory: bundles, manifest, cursor. Git-ignored."""
    return root or (workspace_root() / "memory" / "sjekk")


def store_path(root: Path | None = None) -> Path:
    return state_dir(root) / "cursor.json"


def load(path: Path | None = None) -> dict[str, Any]:
    """Load the cursor document; a missing or unreadable file is simply empty."""
    path = path or store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(cursor: dict[str, Any], path: Path | None = None) -> None:
    """Atomically replace the cursor document (never a truncate-in-place)."""
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o775)
    cursor = dict(cursor)
    cursor["version"] = SCHEMA_VERSION
    blob = json.dumps(cursor, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _entry(cursor: dict[str, Any], track: str) -> dict[str, Any]:
    entry = cursor.get(track)
    return entry if isinstance(entry, dict) else {}


def window(cursor: dict[str, Any], track: str,
           now: datetime | None = None) -> tuple[datetime | None, datetime]:
    """Return the ``(since, until)`` window for *track*.

    ``since`` is ``None`` on the first run (no cursor yet): a one-time replay
    of everything is better than silently dropping a backlog. Mail widens the
    lower bound by :data:`MAIL_OVERLAP` so a late arrival with an old ``Date:``
    is still inside the window.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    raw = _entry(cursor, track).get("to")
    if not isinstance(raw, str) or not raw:
        return None, now
    try:
        last = parse_iso(raw)
    except ValueError:
        return None, now
    since = last - MAIL_OVERLAP if track == "mail" else last
    return since, now


def shown_ids(cursor: dict[str, Any], track: str) -> list[str]:
    """Ids carried for overlap de-duplication (mail only today)."""
    raw = _entry(cursor, track).get("ids")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def advance(cursor: dict[str, Any], track: str, until: datetime,
            ids: Iterable[str] | None = None) -> bool:
    """Move *track* to *until*, never backwards. Returns True when it moved.

    Monotonic on purpose: two concurrent sjekker may both triage the same
    window, but neither may push the cursor behind the other's progress.
    """
    until = until.astimezone(UTC)
    current = _entry(cursor, track).get("to")
    if isinstance(current, str) and current:
        try:
            if parse_iso(current) >= until:
                return False
        except ValueError:
            pass
    entry: dict[str, Any] = {"to": format_iso(until)}
    ids = sorted({i for i in (ids or []) if i})
    if ids:
        entry["ids"] = ids
    cursor[track] = entry
    return True
