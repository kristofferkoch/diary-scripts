"""Tests for signalshow: explicit window filtering, peer/sender filters."""
from __future__ import annotations

import json

from scripts import signalshow as show


def _write(dir_, name, recs):
    (dir_ / name).write_text("".join(json.dumps(r) + "\n" for r in recs))


def _rec(ts, iso, direction="in", name="Alice", text="hi", group=None):
    base = {
        "kind": "message" if direction == "in" else "sent",
        "direction": direction, "ts": ts, "iso": iso, "text": text,
        "group": group,
    }
    if direction == "in":
        base["from"] = {"name": name, "number": "+4700000001"}
        base["to"] = None
    else:
        base["from"] = {"name": "Me"}
        base["to"] = {"name": name, "number": "+4700000001"}
    return base


def test_since_is_strict_and_until_is_inclusive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    _write(tmp_path, "2026-06-07.jsonl", [
        _rec(1, "2026-06-07T11:59:00Z", text="old"),
        _rec(2, "2026-06-07T12:00:00Z", text="boundary"),  # == since → excluded
        _rec(3, "2026-06-07T12:30:00Z", text="new"),
        _rec(4, "2026-06-07T13:00:00Z", text="after-until"),
    ])
    rc = show.main(["--since", "2026-06-07T12:00:00Z",
                    "--until", "2026-06-07T12:30:00Z"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" in out
    assert "old" not in out and "boundary" not in out and "after-until" not in out


def test_the_reader_writes_no_state(tmp_path, monkeypatch):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    _write(tmp_path, "2026-06-07.jsonl", [_rec(1, "2026-06-07T10:00:00Z")])
    before = sorted(p.name for p in tmp_path.iterdir())
    show.main(["--since", "2026-06-07T09:00:00Z"])
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_from_filter_matches_name_substring(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    _write(tmp_path, "2026-06-07.jsonl", [
        _rec(1, "2026-06-07T10:00:00Z", name="Alice", text="from-alice"),
        _rec(2, "2026-06-07T10:01:00Z", name="Bob", text="from-bob"),
    ])
    show.main(["--from", "alic"])
    out = capsys.readouterr().out
    assert "from-alice" in out and "from-bob" not in out


def test_with_filter_includes_own_replies(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    _write(tmp_path, "2026-06-07.jsonl", [
        _rec(1, "2026-06-07T10:00:00Z", direction="in", name="Alice", text="q"),
        _rec(2, "2026-06-07T10:01:00Z", direction="out", name="Alice", text="my-reply"),
        _rec(3, "2026-06-07T10:02:00Z", direction="in", name="Bob", text="other"),
    ])
    show.main(["--with", "alice"])
    out = capsys.readouterr().out
    assert "q" in out and "my-reply" in out and "other" not in out
