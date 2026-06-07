"""Tests for signalshow: cursor filtering, peer/sender filters, --bump."""
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


def test_since_cursor_excludes_at_or_before(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    monkeypatch.setattr(show, "STATE_PATH", tmp_path / "signal-state.json")
    (tmp_path / "signal-state.json").write_text(json.dumps({"cursor": "2026-06-07T12:00:00Z"}))
    _write(tmp_path, "2026-06-07.jsonl", [
        _rec(1, "2026-06-07T11:59:00Z", text="old"),
        _rec(2, "2026-06-07T12:00:00Z", text="boundary"),  # == cursor → excluded
        _rec(3, "2026-06-07T12:30:00Z", text="new"),
    ])
    rc = show.main(["--since-cursor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" in out
    assert "old" not in out and "boundary" not in out


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


def test_bump_sets_cursor_to_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(show, "JSONL_DIR", tmp_path)
    monkeypatch.setattr(show, "STATE_PATH", tmp_path / "signal-state.json")
    _write(tmp_path, "2026-06-07.jsonl", [
        _rec(1, "2026-06-07T10:00:00Z"),
        _rec(3, "2026-06-07T12:00:00Z"),
        _rec(2, "2026-06-07T11:00:00Z"),
    ])
    show.main(["--bump"])
    saved = json.loads((tmp_path / "signal-state.json").read_text())
    assert saved["cursor"] == "2026-06-07T12:00:00Z"  # newest by ts, not file order
