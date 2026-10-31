"""Regression tests for no-follow filesystem writes and their sync consumers."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import skolemelding_sync, spond_sync, vigilo_sync
from scripts.safe_io import append_text, ensure_dir


def test_append_text_creates_and_appends_to_a_real_file(tmp_path: Path):
    path = tmp_path / "nested" / "records.jsonl"
    append_text(path, "first\n")
    append_text(path, "second\n")
    assert path.read_text() == "first\nsecond\n"


def test_append_text_rejects_a_symlinked_parent_component(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "records.jsonl"
    target.write_text("unchanged\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        append_text(root / "linked" / "records.jsonl", "not written\n")
    assert target.read_text() == "unchanged\n"


@pytest.mark.parametrize("module", [spond_sync, vigilo_sync, skolemelding_sync])
def test_sync_jsonl_rejects_an_agent_created_output_symlink(module, tmp_path: Path):
    target = tmp_path / "outside"
    target.write_text("unchanged\n")
    path = tmp_path / "records.jsonl"
    path.symlink_to(target)

    with pytest.raises(OSError):
        module.append_jsonl(path, [{"kind": "test"}])
    assert target.read_text() == "unchanged\n"


def test_ensure_dir_rejects_a_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ensure_dir(linked)


def test_spond_empty_or_non_object_state_uses_the_normal_default(tmp_path: Path, monkeypatch):
    state = tmp_path / "spond-state.json"
    monkeypatch.setattr(spond_sync, "STATE_PATH", state)
    for content in ("", "[]"):
        state.write_text(content)
        loaded = spond_sync.load_state()
        assert loaded["seen_chat_activity"] == {}
        assert loaded["seen_event_activity"] == {}
        assert loaded["seen_post_ids"] == []
        assert loaded["last_successful_run"] is None
