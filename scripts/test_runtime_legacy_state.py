"""Runtime legacy-root compatibility across collection, readers, and dashboards."""
from __future__ import annotations

import json
import os
from pathlib import Path

from mail_reader.config import legacy_state_root
from scripts import embed_messages, sjekk_collect, spond_sync, spondshow
from scripts import skolemelding_sync, skolemeldingshow, vigiloshown, vigilo_sync
from scripts.kindle_dashboard import data as dashboard


def test_all_producers_and_readers_share_runtime_root_outside_tracked_memory():
    root = legacy_state_root()
    assert root == Path(os.environ["DIARY_LEGACY_STATE_DIR"])
    for path in (
        spond_sync.STATE_PATH,
        spond_sync.OUT_DIR,
        spondshow.STATE_PATH,
        spondshow.JSONL_DIR,
        vigilo_sync.STATE_PATH,
        vigilo_sync.OUT_DIR,
        vigiloshown.STATE_PATH,
        vigiloshown.JSONL_DIR,
        skolemelding_sync.STATE_PATH,
        skolemelding_sync.OUT_DIR,
        skolemeldingshow.STATE_PATH,
        skolemeldingshow.JSONL_DIR,
        dashboard.SPOND_DIR,
    ):
        assert path == root or root in path.parents
        assert "memory" not in path.parts


def test_runtime_jsonl_and_cursor_are_readable_by_existing_workflows(
        tmp_path: Path, monkeypatch):
    record = {"kind": "event", "fetched_at": "2026-11-01T10:00:00Z", "data": {"id": "event-1"}}
    output_dir = tmp_path / "spond"
    output_dir.mkdir()
    (output_dir / "2026-11-01.jsonl").write_text(json.dumps(record) + "\n")
    state = tmp_path / "spond-state.json"
    state.write_text('{"last_successful_run":"2026-11-01T10:00:00Z"}')

    assert sjekk_collect._last_sync_epoch(state) is not None
    records = list(embed_messages.iter_records("spond", False))
    assert records == []

    monkeypatch.setattr(embed_messages, "legacy_state_root", lambda: tmp_path)
    monkeypatch.setattr(spondshow, "JSONL_DIR", output_dir)
    assert list(embed_messages.iter_records("spond", False)) == [record]
    assert list(spondshow.iter_records(None, None)) == [record]


def test_sync_daily_output_never_targets_tracked_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(spond_sync, "OUT_DIR", tmp_path / "runtime-spond")
    path = spond_sync.today_jsonl()
    assert path.parent == tmp_path / "runtime-spond"
    assert "memory" not in path.parts
