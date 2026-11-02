"""Tests for the sjekk triage cursor: window maths and monotonic advance."""
from __future__ import annotations

import datetime as dt

from scripts import sjekk_cursor as cur

NOW = dt.datetime(2026, 11, 2, 9, 0, 0, tzinfo=dt.UTC)


def test_parse_format_roundtrip():
    assert cur.format_iso(cur.parse_iso("2026-11-02T09:00:00+01:00")) == "2026-11-02T08:00:00Z"


def test_first_run_has_no_lower_bound():
    since, until = cur.window({}, "signal", NOW)
    assert since is None and until == NOW


def test_next_run_starts_at_the_previous_upper_bound():
    cursor = {"signal": {"to": "2026-11-01T19:41:56Z"}}
    since, until = cur.window(cursor, "signal", NOW)
    assert since is not None
    assert cur.format_iso(since) == "2026-11-01T19:41:56Z"
    assert until == NOW


def test_mail_widens_the_lower_bound_by_the_overlap():
    cursor = {"mail": {"to": "2026-11-01T06:52:57Z"}}
    since, until = cur.window(cursor, "mail", NOW)
    assert since is not None
    assert cur.format_iso(since) == "2026-10-31T06:52:57Z"
    assert until == NOW


def test_advance_is_monotonic():
    cursor: dict = {}
    assert cur.advance(cursor, "mail", NOW) is True
    assert cur.advance(cursor, "mail", NOW - dt.timedelta(hours=1)) is False
    assert cur.format_iso(cur.parse_iso(cursor["mail"]["to"])) == "2026-11-02T09:00:00Z"


def test_advance_stores_overlap_ids_sorted_and_deduped():
    cursor: dict = {}
    cur.advance(cursor, "mail", NOW, ["b", "a", "a", ""])
    assert cursor["mail"]["ids"] == ["a", "b"]


def test_advance_without_ids_drops_old_ids():
    cursor = {"mail": {"to": "2026-11-01T00:00:00Z", "ids": ["old"]}}
    assert cur.advance(cursor, "mail", NOW) is True
    assert "ids" not in cursor["mail"]


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "cursor.json"
    cur.save({"mail": {"to": "2026-11-02T09:00:00Z"}}, path)
    loaded = cur.load(path)
    assert loaded["mail"]["to"] == "2026-11-02T09:00:00Z"
    assert loaded["version"] == cur.SCHEMA_VERSION


def test_missing_or_broken_file_is_empty(tmp_path):
    assert cur.load(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cur.load(bad) == {}
