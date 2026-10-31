"""Security and determinism tests for the trusted immutable event spool."""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.event_spool import (
    EVENT_FIELDS,
    EventConflictError,
    EventSpool,
    EventValidationError,
    build_event,
    canonical_json,
    parse_event,
)


@pytest.fixture
def spool(tmp_path: Path) -> EventSpool:
    root = tmp_path / "events"
    root.mkdir(mode=0o750)
    return EventSpool(root)


def event(**overrides) -> dict:
    result = build_event(
        "spond", "event", "upstream-1",
        {"heading": "Practice", "starts": "2026-11-01T10:00:00Z"},
    )
    result.update(overrides)
    return result


def test_ids_and_canonical_bytes_are_stable_across_restart(spool: EventSpool):
    first = event()
    assert spool.write(first) == "written"
    original = spool.path_for(first["event_id"]).read_bytes()

    restarted = EventSpool(spool.root)
    same = build_event(
        "spond", "event", "upstream-1",
        {"starts": "2026-11-01T10:00:00Z", "heading": "Practice"},
    )
    assert same["event_id"] == first["event_id"]
    assert restarted.write(same) == "duplicate"
    assert restarted.path_for(first["event_id"]).read_bytes() == original
    assert restarted.read(first["event_id"]) == first


def test_id_is_stable_in_fresh_python_processes():
    code = """
import json
from scripts.event_spool import build_event
e = build_event('vigilo', 'message', 'thread-1', {'body': 'hello', 'n': 1.5})
print(json.dumps(e, sort_keys=True))
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
        )
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1]
    first = json.loads(outputs[0])
    assert first["event_id"] == json.loads(outputs[1])["event_id"]


def test_changed_payload_is_a_new_immutable_event(spool: EventSpool):
    first = event()
    changed = build_event(
        "spond", "event", "upstream-1",
        {"heading": "Practice", "starts": "2026-11-01T11:00:00Z"},
    )
    assert first["event_id"] != changed["event_id"]
    spool.write(first)
    spool.write(changed)
    assert len(list(spool)) == 2


def test_duplicate_is_a_noop_and_conflict_never_overwrites(spool: EventSpool):
    original = build_event("mail", "message", "id-1", {"body": "original"}, revision="notmuch-v1")
    changed = build_event("mail", "message", "id-1", {"body": "changed"}, revision="notmuch-v1")
    assert original["event_id"] == changed["event_id"]
    assert spool.write(original) == "written"
    path = spool.path_for(original["event_id"])
    before = path.read_bytes()
    assert spool.write(original) == "duplicate"
    with pytest.raises(EventConflictError):
        spool.write(changed)
    assert path.read_bytes() == before


def test_concurrent_duplicates_link_exactly_once(spool: EventSpool):
    item = event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: spool.write(item), range(16)))
    assert results.count("written") == 1
    assert results.count("duplicate") == 15
    assert spool.read(item["event_id"]) == item
    assert [p.name for p in spool.root.iterdir()] == [item["event_id"] + ".json"]


def test_native_id_cannot_escape_the_spool(spool: EventSpool):
    hostile = build_event("spond", "event", "../../outside", {"x": "\u202e\x00bidi"})
    spool.write(hostile)
    assert spool.path_for(hostile["event_id"]).parent == spool.root
    assert not (spool.root.parent / "outside").exists()
    assert spool.read(hostile["event_id"])["payload"] == hostile["payload"]


@pytest.mark.parametrize("event_id", ["", "../x", "evt_" + "A" * 64, "evt_" + "0" * 63])
def test_invalid_event_ids_are_rejected(spool: EventSpool, event_id: str):
    with pytest.raises(EventValidationError):
        spool.read(event_id)
    with pytest.raises(EventValidationError):
        spool.path_for(event_id)


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"schema_version": True},
        {"schema_version": 2},
        {"source": "unknown"},
        {"source": []},
        {"kind": "../event"},
        {"kind": "EVENT"},
        {"native_id": ""},
        {"native_id": "a\x00b"},
        {"native_id": None},
        {"revision": "random"},
        {"payload": []},
        {"event_id": "evt_" + "0" * 64},
    ],
)
def test_schema_rejects_unknown_and_malformed_fields(mutation: dict):
    candidate = event()
    candidate.update(mutation)
    with pytest.raises(EventValidationError):
        build_and_validate(candidate)


def build_and_validate(candidate: dict) -> None:
    from scripts.event_spool import validate_event
    validate_event(candidate)


def test_unknown_top_level_field_is_rejected():
    candidate = event()
    candidate["surprise"] = "field"
    with pytest.raises(EventValidationError):
        parse_event(canonical_json(candidate))
    assert set(event()) == EVENT_FIELDS


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float):
    with pytest.raises(EventValidationError):
        build_event("spond", "event", "id", {"number": value})


def test_non_json_and_non_string_keys_are_rejected():
    with pytest.raises(EventValidationError):
        build_event("spond", "event", "id", {"set": {"x"}})
    with pytest.raises(EventValidationError):
        canonical_json({1: "not a string key"})


def test_excessive_nesting_is_rejected_without_recursion_failure():
    nested: dict = {}
    current = nested
    for _ in range(100):
        current["child"] = {}
        current = current["child"]
    with pytest.raises(EventValidationError, match="nesting"):
        build_event("spond", "event", "id", nested)
    with pytest.raises(EventValidationError):
        parse_event(b'{"child":' * 1000 + b"{}" + b"}" * 1000)


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json}\n",
        b"\xff\xfe",
        b'{"x": NaN}',
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"event_id":"x","source":"spond","kind":"event",'
        b'"native_id":"id","revision":"notmuch-v1","payload":{}}',
    ],
)
def test_malformed_serialized_events_are_rejected(raw: bytes):
    with pytest.raises(EventValidationError):
        parse_event(raw)


def test_revision_must_match_payload():
    candidate = event()
    candidate["payload"]["heading"] = "tampered"
    with pytest.raises(EventValidationError, match="revision"):
        parse_event(canonical_json(candidate))


def test_symlink_destination_is_not_followed(spool: EventSpool, tmp_path: Path):
    item = event()
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    spool.path_for(item["event_id"]).symlink_to(outside)
    with pytest.raises((OSError, EventValidationError)):
        spool.write(item)
    assert outside.read_text() == "unchanged"


def test_hard_linked_destination_is_rejected(spool: EventSpool, tmp_path: Path):
    item = event()
    spool.write(item)
    path = spool.path_for(item["event_id"])
    os.link(path, tmp_path / "second-link")
    with pytest.raises(EventValidationError, match="single-link"):
        spool.read(item["event_id"])


def test_group_writable_spool_root_is_rejected(tmp_path: Path):
    root = tmp_path / "events"
    root.mkdir(mode=0o750)
    root.chmod(0o770)
    with pytest.raises(EventValidationError, match="group/other"):
        EventSpool(root).write(event())


def test_event_file_mode_and_iteration_order(spool: EventSpool):
    items = [
        build_event("spond", "post", "2", {"n": 2}),
        build_event("spond", "post", "1", {"n": 1}),
    ]
    for item in items:
        spool.write(item)
        assert spool.path_for(item["event_id"]).stat().st_mode & 0o777 == 0o640
    assert [e["event_id"] for e in spool] == sorted(e["event_id"] for e in items)
    assert list(spool) == sorted(items, key=lambda e: e["event_id"])


def test_oversized_serialized_event_is_rejected(spool: EventSpool):
    item = event()
    path = spool.path_for(item["event_id"])
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(EventValidationError, match="too large"):
        spool.read(item["event_id"])


def test_temp_is_unlinked_before_directory_fsync_crash(
    spool: EventSpool, monkeypatch: pytest.MonkeyPatch,
):
    item = event()
    operations = []
    real_fsync = os.fsync
    real_unlink = os.unlink
    real_fchmod = os.fchmod

    def fsync(fd):
        operations.append("file-fsync" if len([op for op in operations if op == "file-fsync"]) == 0 else "dir-fsync")
        if operations[-1] == "dir-fsync":
            raise OSError("simulated directory sync crash")
        real_fsync(fd)

    def unlink(path, **kwargs):
        operations.append("unlink")
        return real_unlink(path, **kwargs)

    def fchmod(fd, mode):
        operations.append("fchmod")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "fchmod", fchmod)
    with pytest.raises(OSError, match="directory sync crash"):
        spool.write(item)
    assert operations.index("fchmod") < operations.index("file-fsync")
    assert operations.index("unlink") < operations.index("dir-fsync")
    path = spool.path_for(item["event_id"])
    assert path.stat().st_nlink == 1
    assert spool.read(item["event_id"]) == item
    assert list(spool.root.glob("*.tmp")) == []
