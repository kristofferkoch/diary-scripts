"""Tests for sjekk-advance: the only writer of the triage cursor."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import sjekk_advance, sjekk_cursor


def _manifest(run: Path, tracks: dict) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text(json.dumps({"version": 1, "tracks": tracks}))
    return run


def test_advances_from_the_manifest_window(tmp_path):
    run = _manifest(tmp_path / "run", {
        "mail": {"until": "2026-11-02T09:00:00Z", "ok": True},
    })
    assert sjekk_advance.main([
        "--manifest", str(run), "--state-dir", str(tmp_path / "state"), "mail"]) == 0
    cursor = sjekk_cursor.load(tmp_path / "state" / "cursor.json")
    assert cursor["mail"]["to"] == "2026-11-02T09:00:00Z"


def test_refuses_a_failed_track(tmp_path, capsys):
    run = _manifest(tmp_path / "run", {
        "spond": {"until": "2026-11-02T09:00:00Z", "ok": False},
    })
    assert sjekk_advance.main([
        "--manifest", str(run), "--state-dir", str(tmp_path / "state"), "spond"]) == 1
    assert not (tmp_path / "state" / "cursor.json").exists()
    assert "bundle failed" in capsys.readouterr().err


def test_refuses_a_track_missing_from_the_manifest(tmp_path, capsys):
    run = _manifest(tmp_path / "run", {"mail": {"until": "2026-11-02T09:00:00Z", "ok": True}})
    assert sjekk_advance.main([
        "--manifest", str(run), "--state-dir", str(tmp_path / "state"), "signal"]) == 1
    assert "not in" in capsys.readouterr().err


def test_carries_overlap_ids(tmp_path):
    run = _manifest(tmp_path / "run", {
        "mail": {"until": "2026-11-02T09:00:00Z", "ok": True,
                 "ids_file": "mail.ids"},
    })
    (run / "mail.ids").write_text("a\nb\n")
    assert sjekk_advance.main([
        "--manifest", str(run), "--state-dir", str(tmp_path / "state"), "mail"]) == 0
    cursor = sjekk_cursor.load(tmp_path / "state" / "cursor.json")
    assert cursor["mail"]["ids"] == ["a", "b"]


def test_second_advance_is_a_no_op(tmp_path):
    run = _manifest(tmp_path / "run", {"mail": {"until": "2026-11-02T09:00:00Z", "ok": True}})
    state = tmp_path / "state"
    for _ in range(2):
        assert sjekk_advance.main([
            "--manifest", str(run), "--state-dir", str(state), "mail"]) == 0
    assert sjekk_cursor.load(state / "cursor.json")["mail"]["to"] == "2026-11-02T09:00:00Z"


def test_init_seeds_from_the_old_tracked_cursors(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "mail-state.json").write_text(
        json.dumps({"last_successful_run": "2026-11-01T06:52:57Z"}))
    (memory / "signal-state.json").write_text(
        json.dumps({"cursor": "2026-11-01T19:41:56Z"}))
    assert sjekk_advance.main(["--init", "--state-dir", str(memory / "sjekk")]) == 0
    cursor = sjekk_cursor.load(memory / "sjekk" / "cursor.json")
    assert cursor["mail"]["to"] == "2026-11-01T06:52:57Z"
    assert cursor["signal"]["to"] == "2026-11-01T19:41:56Z"


def test_init_refuses_to_overwrite(tmp_path, capsys):
    state = tmp_path / "state"
    sjekk_cursor.save({"mail": {"to": "2026-11-01T00:00:00Z"}}, state / "cursor.json")
    assert sjekk_advance.main(["--init", "--state-dir", str(state)]) == 1
    assert "refusing" in capsys.readouterr().err
