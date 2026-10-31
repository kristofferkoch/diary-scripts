"""Regressions for owner-only collector state and broker/timer event delivery."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import skolemelding_sync, spond_sync, vigilo_sync
from scripts.diary_sync_broker import SYNC_TARGETS
from scripts.event_spool import EventSpool


@pytest.fixture
def spool_path(tmp_path: Path) -> Path:
    path = tmp_path / "events"
    path.mkdir(mode=0o750)
    return path


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "collector-state"
    path.mkdir(mode=0o700)
    return path


def _patch_legacy(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    legacy = tmp_path / f"{module.__name__}-legacy-state.json"
    monkeypatch.setattr(module, "STATE_PATH", legacy)
    monkeypatch.setattr(module, "LEGACY_STATE_PATH", legacy)
    monkeypatch.setattr(module, "today_jsonl", lambda: tmp_path / "legacy.jsonl")
    return legacy


@pytest.mark.parametrize("source", ["spond", "vigilo", "skolemelding"])
def test_broker_then_timer_exports_before_advancing_shared_trusted_cursor(
    source: str, spool_path: Path, state_dir: Path,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    modules = {
        "spond": spond_sync,
        "vigilo": vigilo_sync,
        "skolemelding": skolemelding_sync,
    }
    module = modules[source]
    legacy = _patch_legacy(module, tmp_path, monkeypatch)
    native_key = "threadUid" if source == "vigilo" else "id"
    fresh_record = {
        "kind": "message" if source != "spond" else "event",
        "fetched_at": "now",
        "data": {native_key: "native-1", "body": "fresh"},
    }
    calls = []

    def selected(state):
        calls.append(dict(state))
        if state.get("test_seen"):
            return 0, [], {}
        state["test_seen"] = True
        return 1, [fresh_record], {fresh_record["kind"]: 1}

    if source == "spond":
        async def run_once(kinds, state):
            return selected(state)
    elif source == "vigilo":
        def run_once(kinds, state):
            return selected(state)
    else:
        def run_once(state):
            return selected(state)
    monkeypatch.setattr(module, "run_once", run_once)

    broker_args = list(SYNC_TARGETS[source][1:])
    broker_args[broker_args.index("--event-spool") + 1] = str(spool_path)
    broker_args[broker_args.index("--state-dir") + 1] = str(state_dir)
    assert module.main(broker_args) == 0
    assert len(list(EventSpool(spool_path))) == 1
    trusted = json.loads((state_dir / f"{source}-state.json").read_text())
    assert trusted["test_seen"] is True
    assert (state_dir / f"{source}-state.json").stat().st_mode & 0o777 == 0o600
    assert state_dir.stat().st_mode & 0o777 == 0o700

    timer_args = list(broker_args)
    assert module.main(timer_args) == 0
    assert len(list(EventSpool(spool_path))) == 1
    assert len(calls) == 2 if source == "spond" else len(calls) == 1
    assert legacy.exists(), "legacy state is a mirror, never the trusted input"


@pytest.mark.parametrize("source", ["spond", "vigilo", "skolemelding"])
def test_first_trusted_run_backfills_spool_without_replaying_legacy_jsonl(
    source: str, spool_path: Path, state_dir: Path,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    modules = {
        "spond": spond_sync,
        "vigilo": vigilo_sync,
        "skolemelding": skolemelding_sync,
    }
    module = modules[source]
    legacy = _patch_legacy(module, tmp_path, monkeypatch)
    legacy.write_text(json.dumps({"test_seen": True, "forged_legacy": True}))
    legacy_jsonl = tmp_path / "legacy.jsonl"
    legacy_jsonl.write_text("existing legacy output\n")
    native_key = "threadUid" if source == "vigilo" else "id"
    fresh_record = {
        "kind": "message" if source != "spond" else "event",
        "fetched_at": "now",
        "data": {native_key: "native-1", "body": "backfill"},
    }

    def selected(state):
        assert "forged_legacy" not in state
        if state.get("test_seen"):
            return 0, [], {}
        state["test_seen"] = True
        return 1, [fresh_record], {fresh_record["kind"]: 1}

    if source == "spond":
        async def run_once(kinds, state):
            return selected(state)
    elif source == "vigilo":
        def run_once(kinds, state):
            return selected(state)
    else:
        def run_once(state):
            return selected(state)
    monkeypatch.setattr(module, "run_once", run_once)
    args = [
        "--once", "--event-spool", str(spool_path), "--state-dir", str(state_dir),
    ]

    assert module.main(args) == 0
    assert len(list(EventSpool(spool_path))) == 1
    assert legacy_jsonl.read_text() == "existing legacy output\n"
    trusted = json.loads((state_dir / f"{source}-state.json").read_text())
    assert trusted["test_seen"] is True
    assert "forged_legacy" not in trusted
    mirrored = json.loads(legacy.read_text())
    assert mirrored["test_seen"] is True
    assert "forged_legacy" not in mirrored


def test_forged_legacy_spond_seen_marker_does_not_suppress_trusted_event(
    spool_path: Path, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    legacy = _patch_legacy(spond_sync, tmp_path, monkeypatch)
    legacy.write_text(json.dumps({"seen_event_activity": {"event-1": "forged"}}))
    observed = []

    async def run_once(kinds, state):
        observed.append(state)
        record = {"kind": "event", "fetched_at": "now", "data": {"id": "event-1"}}
        return 1, [record], {"event": 1}

    monkeypatch.setattr(spond_sync, "run_once", run_once)
    args = [
        "--once", "--event-spool", str(spool_path), "--state-dir", str(state_dir),
    ]
    assert spond_sync.main(args) == 0
    assert observed[0]["seen_event_activity"] == {}
    assert len(list(EventSpool(spool_path))) == 1


def test_malformed_trusted_spond_seen_markers_reemit_instead_of_crashing(state_dir: Path):
    path = state_dir / "spond-state.json"
    path.write_text(json.dumps({
        "seen_chat_activity": "not-a-map",
        "seen_event_activity": [],
        "seen_post_ids": [1, None],
    }))
    state = spond_sync.load_state(path)
    assert state["seen_chat_activity"] == {}
    assert state["seen_event_activity"] == {}
    assert state["seen_post_ids"] == []
    class FakeSpond:
        async def get_events(self):
            return [{"id": "event-1", "heading": "fresh"}]

    out = []
    new = asyncio.run(spond_sync.fetch_events(FakeSpond(), state, out, None))
    assert new == 1
    assert out[0]["data"]["id"] == "event-1"


def test_forged_legacy_vigilo_tombstone_is_not_synthesized(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    legacy = _patch_legacy(vigilo_sync, tmp_path, monkeypatch)
    forged_id = "forged-note"
    legacy.write_text(json.dumps({
        "check_out_notes": {
            forged_id: {"sig": "forged", "title": "forged title", "unit": "forged unit"},
        },
    }))
    trusted = vigilo_sync.load_state(state_dir / "vigilo-state.json")

    class Api:
        def check_in_notes(self, child_id, from_date, to_date):
            return []

    out = []
    counts = vigilo_sync.fetch_check_out_notes(
        Api(), trusted, out, ["child-1"], [("child-1", "unit-1", "trusted unit")],
    )
    assert counts == {"check_out_note": 0}
    assert out == []
    assert forged_id not in trusted["check_out_notes"]


def test_malformed_trusted_vigilo_tombstones_and_seen_threads_are_ignored(state_dir: Path):
    path = state_dir / "vigilo-state.json"
    path.write_text(json.dumps({
        "threads": {"good": "not-an-entry"},
        "check_out_notes": ["not-a-map"],
        "attachment_retries": {"bad": "many"},
    }))
    state = vigilo_sync.load_state(path)
    assert state["threads"] == {}
    assert state["check_out_notes"] == {}
    assert state["attachment_retries"] == {}


def test_malformed_trusted_skolemelding_seen_markers_are_ignored(state_dir: Path):
    path = state_dir / "skolemelding-state.json"
    path.write_text(json.dumps({
        "messages": {"1": "not-an-entry"},
        "attachment_retries": {"1": "many"},
    }))
    state = skolemelding_sync.load_state(path)
    assert state["messages"] == {}
    assert state["attachment_retries"] == {}


@pytest.mark.parametrize("module", [vigilo_sync, skolemelding_sync])
def test_future_last_successful_run_never_skips_collection(module):
    future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert module.recently_ran({"last_successful_run": future}) is False
