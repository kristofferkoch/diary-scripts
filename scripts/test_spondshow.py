"""Tests for spondshow.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mail_reader import config as config_module
from scripts import spondshow
from scripts.spondshow import (
    iter_records,
    render_full,
    render_header,
    rsvp_status,
)


# ---------- render_header: pin Spond field names ----------
# These pin the actual field paths in Spond API responses (as of 2026-05).
# If Spond ever renames `data.message.text` or `data.heading`, etc., these
# tests will fail and we'll know to update render_header rather than silently
# render empty strings (the original bug, where I'd guessed `data.lastMessage`
# and `data.text` and headers came out blank).


def test_render_header_chat_includes_name_and_message_text():
    rec = {
        "kind": "chat",
        "fetched_at": "2026-05-26T11:52:50Z",
        "data": {
            "id": "CHAT1",
            "name": "G-lag Treningsgruppe",
            "newestTimestamp": "2026-05-26T10:00:00Z",
            "unread": True,
            "message": {"type": "TEXT", "text": "Husk å ta med vannflaske"},
        },
    }
    out = render_header(rec)
    assert "G-lag Treningsgruppe" in out
    assert "Husk å ta med vannflaske" in out


def test_render_header_chat_rename_shows_new_name():
    """RENAME-type messages don't have .text; render_header should fall through to .newName."""
    rec = {
        "kind": "chat",
        "fetched_at": "2026-05-26T11:52:50Z",
        "data": {
            "id": "CHAT2",
            "name": "Lunde-dag",
            "newestTimestamp": "2026-04-16T19:05:37.325Z",
            "message": {"type": "RENAME", "newName": "Lunde-dag for 2018-årgangen"},
        },
    }
    out = render_header(rec)
    assert "Lunde-dag for 2018-årgangen" in out


def test_render_header_event_uses_heading_and_starttimestamp_and_location_feature():
    rec = {
        "kind": "event",
        "fetched_at": "2026-05-26T11:52:51Z",
        "data": {
            "id": "EV1",
            "heading": "Eksempel-IL G8 Eksempel-IL 2 – Nordstrand G8 Lilla",
            "startTimestamp": "2026-05-26T15:30:00Z",
            "location": {"feature": "eksempelhøgda bane 12 5er"},
        },
    }
    out = render_header(rec)
    assert "2026-05-26T15:30:00Z" in out
    assert "Eksempel-IL G8 Eksempel-IL 2 – Nordstrand G8 Lilla" in out
    assert "eksempelhøgda bane 12 5er" in out


def test_render_header_post_uses_title_and_timestamp():
    rec = {
        "kind": "post",
        "fetched_at": "2026-05-26T11:52:51Z",
        "data": {
            "id": "P1",
            "title": "Felles treninger på gresset - G-lag",
            "timestamp": "2026-05-03T10:23:34.279Z",
            "body": "Hei alle...",
        },
    }
    out = render_header(rec)
    assert "Felles treninger på gresset - G-lag" in out
    assert "2026-05-03T10:23:34.279Z" in out


# ---------- RSVP: pin response field paths ----------


def test_render_header_event_rsvp_accepted(monkeypatch):
    monkeypatch.setattr(spondshow, "_RSVP_MEMBER_ID", "ME")
    rec = {
        "kind": "event",
        "fetched_at": "x",
        "data": {
            "id": "E", "heading": "h", "startTimestamp": "t",
            "responses": {"acceptedIds": ["ME"], "declinedIds": [], "unansweredIds": []},
        },
    }
    out = render_header(rec)
    assert "✓" in out


def test_render_header_event_rsvp_declined(monkeypatch):
    monkeypatch.setattr(spondshow, "_RSVP_MEMBER_ID", "ME")
    rec = {
        "kind": "event",
        "fetched_at": "x",
        "data": {
            "id": "E", "heading": "h", "startTimestamp": "t",
            "responses": {"acceptedIds": [], "declinedIds": ["ME"], "unansweredIds": []},
        },
    }
    out = render_header(rec)
    assert "✗" in out


def test_render_header_event_rsvp_member_not_on_list(monkeypatch):
    """When the member-id isn't in any response list, show em-dash."""
    monkeypatch.setattr(spondshow, "_RSVP_MEMBER_ID", "ME")
    rec = {
        "kind": "event",
        "fetched_at": "x",
        "data": {
            "id": "E", "heading": "h", "startTimestamp": "t",
            "responses": {"acceptedIds": ["someone-else"], "declinedIds": [], "unansweredIds": []},
        },
    }
    out = render_header(rec)
    assert "—" in out


def test_render_header_event_no_rsvp_when_member_id_unset(monkeypatch):
    """Without _RSVP_MEMBER_ID, no RSVP marker should appear."""
    monkeypatch.setattr(spondshow, "_RSVP_MEMBER_ID", None)
    rec = {
        "kind": "event",
        "fetched_at": "x",
        "data": {"id": "E", "heading": "h", "startTimestamp": "t"},
    }
    out = render_header(rec)
    for mark in ("✓", "✗", "?", "—"):
        assert mark not in out


def test_main_needs_no_trusted_config(tmp_path, monkeypatch, capsys):
    """2026-11-02: the whole Spond track read /etc/diary/trusted.toml for the
    RSVP member id and came out `!! FAILED`. RSVP markers are optional; the
    reader must render the track with no privileged config at all."""
    jdir = tmp_path / "spond"
    jdir.mkdir()
    record = {
        "kind": "event",
        "fetched_at": "2026-05-26T10:00:00Z",
        "data": {
            "id": "E1",
            "heading": "Training",
            "startTimestamp": "2026-05-26T15:30:00Z",
            "responses": {"acceptedIds": ["MEMBER"]},
        },
    }
    (jdir / "2026-05-26.jsonl").write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(spondshow, "JSONL_DIR", jdir)
    monkeypatch.delenv(spondshow.RSVP_ENV, raising=False)
    monkeypatch.setattr(spondshow, "_RSVP_MEMBER_ID", None)

    assert spondshow.main(["--headers-only"]) == 0
    out = capsys.readouterr().out
    assert "[event]" in out and "✓" not in out

    # An explicit member id still gives the marker when someone wants it.
    assert spondshow.main(["--headers-only", "--rsvp-as", "MEMBER"]) == 0
    assert "[event ✓]" in capsys.readouterr().out


# ---------- rsvp_status ----------


def test_rsvp_status_returns_none_when_member_absent():
    ev = {"responses": {"acceptedIds": ["A"], "declinedIds": [], "unansweredIds": []}}
    assert rsvp_status(ev, "Z") is None


def test_rsvp_status_handles_missing_responses_key():
    assert rsvp_status({}, "A") is None


# ---------- iter_records ----------


def test_iter_records_round_trip(tmp_path, monkeypatch):
    """Write JSONL, read it back via iter_records, confirm filtering by kind."""
    jdir = tmp_path / "spond"
    jdir.mkdir()
    recs = [
        {"kind": "chat", "fetched_at": "2026-05-26T10:00:00Z", "data": {"id": "C1"}},
        {"kind": "event", "fetched_at": "2026-05-26T10:00:01Z", "data": {"id": "E1"}},
        {"kind": "post", "fetched_at": "2026-05-26T10:00:02Z", "data": {"id": "P1"}},
    ]
    (jdir / "2026-05-26.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(spondshow, "JSONL_DIR", jdir)
    assert [r["data"]["id"] for r in iter_records(None, None, None)] == ["C1", "E1", "P1"]
    assert [r["data"]["id"] for r in iter_records(None, None, {"event"})] == ["E1"]
    assert [r["data"]["id"] for r in iter_records(None, None, {"chat", "post"})] == ["C1", "P1"]


def test_iter_records_since_filter_drops_older(tmp_path, monkeypatch):
    jdir = tmp_path / "spond"
    jdir.mkdir()
    (jdir / "2026-05-20.jsonl").write_text(
        json.dumps({"kind": "chat", "fetched_at": "2026-05-20T10:00:00Z", "data": {"id": "OLD"}}) + "\n",
        encoding="utf-8",
    )
    (jdir / "2026-05-26.jsonl").write_text(
        json.dumps({"kind": "chat", "fetched_at": "2026-05-26T10:00:00Z", "data": {"id": "NEW"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(spondshow, "JSONL_DIR", jdir)
    since = datetime(2026, 5, 25, tzinfo=timezone.utc)
    ids = [r["data"]["id"] for r in iter_records(since, None, None)]
    assert ids == ["NEW"]


def test_iter_records_returns_nothing_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(spondshow, "JSONL_DIR", tmp_path / "does-not-exist")
    assert list(iter_records(None, None, None)) == []


# ---------- explicit window ----------


def test_iter_records_until_is_inclusive_and_since_is_strict(tmp_path, monkeypatch):
    jdir = tmp_path / "spond"
    jdir.mkdir()
    recs = [
        {"kind": "chat", "fetched_at": "2026-05-26T09:59:59Z", "data": {"id": "A"}},
        {"kind": "chat", "fetched_at": "2026-05-26T10:00:00Z", "data": {"id": "B"}},
        {"kind": "chat", "fetched_at": "2026-05-26T10:00:01Z", "data": {"id": "C"}},
    ]
    (jdir / "2026-05-26.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    monkeypatch.setattr(spondshow, "JSONL_DIR", jdir)
    lo = datetime(2026, 5, 26, 10, 0, 0, tzinfo=timezone.utc)
    hi = datetime(2026, 5, 26, 10, 0, 1, tzinfo=timezone.utc)
    assert [r["data"]["id"] for r in iter_records(lo, hi, None)] == ["C"]


def test_the_reader_needs_no_trusted_state():
    """RSVP markers are cosmetic; a missing/denied trusted config must not
    take the track down (the 2026-11-02 bundle failed exactly that way)."""
    rc = spondshow.main(["--until", "2026-05-26T10:00:00Z"])
    assert rc == 1  # empty fixture dir -> quiet track, not a crash


# ---------- render_full truncation ----------


def test_render_full_truncates_when_over_max_chars():
    rec = {"kind": "chat", "fetched_at": "x", "data": {"id": "C", "name": "long" * 1000}}
    out = render_full(rec, max_chars=200)
    assert "truncated at 200 chars" in out


def test_render_full_zero_max_chars_means_no_truncation():
    rec = {"kind": "chat", "fetched_at": "x", "data": {"id": "C", "name": "x" * 5000}}
    out = render_full(rec, max_chars=0)
    assert "truncated" not in out
