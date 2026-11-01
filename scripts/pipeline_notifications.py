"""Fixed-template, durable notification rendering for validated triage results.

This module deliberately has no notification transport. PIPE-03 supplies the
strict result mapping and PIPE-05 can transport the returned immutable record.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts.event_spool import EVENT_ID_RE

TRIAGE_FIELDS = frozenset({
    "event_id", "urgency", "summary", "deadline", "review_reference",
})
NOTIFICATION_SCHEMA_VERSION = 1
FIXED_RECIPIENT = "note-to-self"
FIXED_ENVELOPE = "diary-pipeline-v1"
MAX_SUMMARY = 500
MAX_DEADLINE = 80
MAX_REVIEW_REFERENCE = 120
MAX_RAW_SUMMARY = 64 * 1024
MAX_RAW_FIELD = 4096

_ANSI_RE = re.compile(
    r"(?:\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\)|.)"
    r"|\x9b[0-?]*[ -/]*[@-~])",
    re.DOTALL,
)
_DOMAIN_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
_ASCII_TLD = r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})"
_UNICODE_TLD = r"[^\W\d_][\w-]{1,62}"
_ASCII_DOMAIN = rf"(?:{_DOMAIN_LABEL}\.)+{_ASCII_TLD}"
_IDN_DOMAIN = rf"(?:{_DOMAIN_LABEL}\.)+{_UNICODE_TLD}"
_UNICODE_DOT_DOMAIN = rf"(?:{_DOMAIN_LABEL}[。．｡․])+{_UNICODE_TLD}"
_URL_RES = (
    re.compile(
        r"(?i)(?<![\w.+-])[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,63}"
        r"(?::\d{1,5})?(?:/[^\s<>]*)?"
    ),
    re.compile(
        r"(?i)\b(?:(?:https?|ftp|file|data|javascript|mailto|tel|signal)\s*:"
        r"|[a-z][a-z0-9+.-]{1,31}\s*:\s*//)[^\s<>]*"
    ),
    re.compile(r"(?i)(?<![\w.])www\.[^\s<>]+"),
    re.compile(
        r"(?<![\w:/])//(?:\[[^\]\s<>]+\]|[^\s/:<>]+)"
        r"(?::\d{1,5})?(?:/[^\s<>]*)?"
    ),
    re.compile(
        rf"(?i)(?<![\w@.-]){_ASCII_DOMAIN}"
        r"(?::\d{1,5})?(?:/[^\s<>]*)?"
    ),
    re.compile(
        rf"(?i)(?<![\w@.-]){_IDN_DOMAIN}"
        r"(?::\d{1,5})?/[^\s<>]*"
    ),
    re.compile(
        rf"(?i)(?<![\w@.-]){_UNICODE_DOT_DOMAIN}"
        r"(?::\d{1,5})?/[^\s<>]*"
    ),
    re.compile(
        r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s<>]*)?"
    ),
)
_HTML_TAG_RE = re.compile(r"</?[^<>]*>")
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_REFERENCE_RE = re.compile(r"!?\[([^\]]*)\]\[[^]]*\]")
_WHITESPACE_RE = re.compile(r"\s+")


class TriageResultError(ValueError):
    """A value does not match PIPE-03's exact five-field result schema."""


@dataclass(frozen=True)
class Notification:
    notification_id: str
    kind: str
    recipient: str
    envelope: str
    body: str
    event_ids: tuple[str, ...]
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NOTIFICATION_SCHEMA_VERSION,
            "notification_id": self.notification_id,
            "kind": self.kind,
            "recipient": self.recipient,
            "envelope": self.envelope,
            "body": self.body,
            "event_ids": list(self.event_ids),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Decision:
    event_id: str
    status: str
    notification: Notification | None = None


@dataclass(frozen=True)
class ReviewEntry:
    event_id: str
    status: str
    result: dict[str, Any]
    rendered: dict[str, str]
    notification_id: str | None
    created_at: float
    decided_at: float


def validate_triage_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != TRIAGE_FIELDS:
        raise TriageResultError("triage result fields do not match the strict schema")
    if not isinstance(result["event_id"], str) or not EVENT_ID_RE.fullmatch(result["event_id"]):
        raise TriageResultError("invalid event_id")
    if not isinstance(result["urgency"], str) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,31}", result["urgency"],
    ):
        raise TriageResultError("invalid urgency")
    if not isinstance(result["summary"], str) or len(result["summary"]) > MAX_RAW_SUMMARY:
        raise TriageResultError("invalid or oversized summary")
    for field in ("deadline", "review_reference"):
        value = result[field]
        if value is not None and (not isinstance(value, str) or len(value) > MAX_RAW_FIELD):
            raise TriageResultError(f"invalid or oversized {field}")
    return dict(result)


def sanitize_field(value: str, *, limit: int, empty: str = "[no safe text]") -> str:
    """Return one plain-text line with no links, controls, or text formatting."""
    text = html.unescape(unicodedata.normalize("NFKC", value[:MAX_RAW_SUMMARY]))
    text = _ANSI_RE.sub(" ", text)
    text = "".join(
        " " if unicodedata.category(char) in {"Cc", "Zl", "Zp"} and char.isspace()
        else "" if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co"}
        else char
        for char in text
    )
    for pattern in _URL_RES:
        text = pattern.sub(" [link removed] ", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _MARKDOWN_REFERENCE_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.translate({ord(char): " " for char in "`*_~#>\\"})
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return empty
    if len(text) > limit:
        return text[:limit - 1].rstrip() + "…"
    return text


def _notification_id(kind: str, event_ids: tuple[str, ...]) -> str:
    identity = json.dumps([kind, event_ids], separators=(",", ":")).encode("ascii")
    return "ntf_" + hashlib.sha256(identity).hexdigest()


def _render_event(rendered: dict[str, str]) -> str:
    return (
        f"Summary: {rendered['summary']}\n"
        f"Deadline: {rendered['deadline']}\n"
        f"Review: {rendered['review_reference']}"
    )


def _immediate_body(event_id: str, rendered: dict[str, str]) -> str:
    return (
        "Diary notification\n"
        f"Event: {event_id}\n"
        f"Urgency: {rendered['urgency']}\n"
        f"{_render_event(rendered)}"
    )


def _digest_body(entries: list[ReviewEntry]) -> str:
    sections = [f"Diary digest\nLow-priority events: {len(entries)}"]
    sections.extend(f"Event: {entry.event_id}\n{_render_event(entry.rendered)}" for entry in entries)
    return "\n\n".join(sections)


class NotificationRenderer:
    """Persist at-most-once rendering decisions and sliding-window limits."""

    def __init__(
        self,
        state_path: Path | str,
        *,
        aggregate_limit: int = 5,
        aggregate_window: float = 3600.0,
        digest_max_events: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(aggregate_limit) is not int or aggregate_limit < 1:
            raise ValueError("aggregate_limit must be a positive integer")
        if not isinstance(aggregate_window, (int, float)) or isinstance(aggregate_window, bool) \
                or not math.isfinite(aggregate_window) or aggregate_window <= 0:
            raise ValueError("aggregate_window must be positive and finite")
        if type(digest_max_events) is not int or digest_max_events < 1:
            raise ValueError("digest_max_events must be a positive integer")
        self.state_path = Path(state_path)
        self.aggregate_limit = aggregate_limit
        self.aggregate_window = float(aggregate_window)
        self.digest_max_events = digest_max_events
        self.clock = clock
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_path.is_symlink():
            raise ValueError("notification state must not be a symlink")
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    rendered_json TEXT NOT NULL,
                    notification_id TEXT,
                    created_at REAL NOT NULL,
                    decided_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_events (
                    notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
                    event_id TEXT NOT NULL REFERENCES events(event_id),
                    PRIMARY KEY (notification_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS notifications_created
                    ON notifications(created_at);
                """
            )
        os.chmod(self.state_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.state_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _now(self, now: float | None) -> float:
        value = self.clock() if now is None else now
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(value) or value < 0:
            raise ValueError("now must be a finite, non-negative timestamp")
        return float(value)

    @staticmethod
    def _rendered(result: dict[str, Any]) -> dict[str, str]:
        return {
            "urgency": result["urgency"],
            "summary": sanitize_field(result["summary"], limit=MAX_SUMMARY),
            "deadline": sanitize_field(result["deadline"] or "", limit=MAX_DEADLINE, empty="-"),
            "review_reference": sanitize_field(
                result["review_reference"] or "", limit=MAX_REVIEW_REFERENCE, empty="-",
            ),
        }

    @staticmethod
    def _row_entry(row: tuple[Any, ...]) -> ReviewEntry:
        return ReviewEntry(
            event_id=row[0],
            status=row[1],
            result=json.loads(row[2]),
            rendered=json.loads(row[3]),
            notification_id=row[4],
            created_at=row[5],
            decided_at=row[6],
        )

    def get_review(self, event_id: str) -> ReviewEntry | None:
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            raise TriageResultError("invalid event_id")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT event_id, status, result_json, rendered_json, notification_id, "
                "created_at, decided_at FROM events WHERE event_id = ?", (event_id,),
            ).fetchone()
        return self._row_entry(row) if row else None

    def list_review(self) -> list[ReviewEntry]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event_id, status, result_json, rendered_json, notification_id, "
                "created_at, decided_at FROM events ORDER BY created_at, event_id",
            ).fetchall()
        return [self._row_entry(row) for row in rows]

    @staticmethod
    def _insert_notification(
        conn: sqlite3.Connection,
        kind: str,
        entries: list[ReviewEntry],
        body: str,
        now: float,
    ) -> Notification:
        event_ids = tuple(entry.event_id for entry in entries)
        notification = Notification(
            notification_id=_notification_id(kind, event_ids),
            kind=kind,
            recipient=FIXED_RECIPIENT,
            envelope=FIXED_ENVELOPE,
            body=body,
            event_ids=event_ids,
            created_at=now,
        )
        conn.execute(
            "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?)",
            (
                notification.notification_id, kind, FIXED_RECIPIENT, FIXED_ENVELOPE, body, now,
            ),
        )
        conn.executemany(
            "INSERT INTO notification_events VALUES (?, ?)",
            [(notification.notification_id, event_id) for event_id in event_ids],
        )
        return notification

    def _within_limit(self, conn: sqlite3.Connection, now: float) -> bool:
        count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE created_at > ?",
            (now - self.aggregate_window,),
        ).fetchone()[0]
        return count < self.aggregate_limit

    def submit(self, result: dict[str, Any], *, now: float | None = None) -> Decision:
        result = validate_triage_result(result)
        timestamp = self._now(now)
        event_id = result["event_id"]
        rendered = self._rendered(result)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,),
            ).fetchone()
            if existing:
                conn.commit()
                return Decision(event_id, "duplicate")
            conn.execute(
                "INSERT INTO events(event_id, status, result_json, rendered_json, created_at, decided_at) "
                "VALUES (?, 'pending_digest', ?, ?, ?, ?)",
                (
                    event_id,
                    json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(rendered, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    timestamp,
                    timestamp,
                ),
            )
            if result["urgency"] == "low":
                conn.commit()
                return Decision(event_id, "pending_digest")
            if not self._within_limit(conn, timestamp):
                conn.execute(
                    "UPDATE events SET status = 'rate_limited' WHERE event_id = ?", (event_id,),
                )
                conn.commit()
                return Decision(event_id, "rate_limited")
            entry = ReviewEntry(
                event_id, "notified", result, rendered, None, timestamp, timestamp,
            )
            notification = self._insert_notification(
                conn, "immediate", [entry], _immediate_body(event_id, rendered), timestamp,
            )
            conn.execute(
                "UPDATE events SET status = 'notified', notification_id = ? WHERE event_id = ?",
                (notification.notification_id, event_id),
            )
            conn.commit()
            return Decision(event_id, "notified", notification)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def render_digest(self, *, now: float | None = None) -> Notification | None:
        timestamp = self._now(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT event_id, status, result_json, rendered_json, notification_id, "
                "created_at, decided_at FROM events WHERE status = 'pending_digest' "
                "ORDER BY created_at, event_id LIMIT ?", (self.digest_max_events,),
            ).fetchall()
            if not rows or not self._within_limit(conn, timestamp):
                conn.commit()
                return None
            entries = [self._row_entry(row) for row in rows]
            notification = self._insert_notification(
                conn, "digest", entries, _digest_body(entries), timestamp,
            )
            conn.executemany(
                "UPDATE events SET status = 'digested', notification_id = ?, decided_at = ? "
                "WHERE event_id = ?",
                [(notification.notification_id, timestamp, entry.event_id) for entry in entries],
            )
            conn.commit()
            return notification
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
