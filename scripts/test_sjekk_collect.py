"""Tests for sjekk_collect.py — DB-free, stub commands only.

Nothing here touches real systemctl / psql / notmuch: every track is a
stub (`sh -c …`, `sleep`, a missing binary) so the suite is hermetic.
Run with `uv run pytest scripts/`.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from scripts import sjekk_collect
from scripts.sjekk_collect import (
    TRACKS,
    TrackResult,
    render_index,
    run_tracks,
    write_run,
)


def _sh(script: str) -> list[str]:
    return ["sh", "-c", script]


def test_run_tracks_captures_output() -> None:
    [res] = run_tracks({"a.txt": [_sh("echo hello")]}, timeout=10)
    assert res.filename == "a.txt"
    assert res.output == "hello\n"
    assert not res.failed
    assert not res.empty


def test_run_tracks_multi_command_track_concatenates_in_order() -> None:
    [res] = run_tracks(
        {"multi.txt": [_sh("echo one"), _sh("echo two")]}, timeout=10)
    assert res.output == "one\ntwo\n"
    assert not res.failed


def test_run_tracks_failure_degrades_to_marker() -> None:
    [res] = run_tracks({"bad.txt": [_sh("echo partial; exit 3")]}, timeout=10)
    assert res.failed
    assert "partial" in res.output
    assert "!! FAILED (exit 3)" in res.output


def test_run_tracks_timeout_degrades_to_marker() -> None:
    [res] = run_tracks({"slow.txt": [_sh("echo first; sleep 5")]}, timeout=1)
    assert res.failed
    assert res.timed_out
    assert "first" in res.output
    assert "!! TIMEOUT" in res.output


def test_run_tracks_missing_binary_degrades_to_marker() -> None:
    [res] = run_tracks(
        {"gone.txt": [["definitely-not-a-real-command-xyz"]]}, timeout=10)
    assert res.failed
    assert "!! FAILED" in res.output


def test_run_tracks_no_matching_is_nothing_new_not_failure() -> None:
    # The *show tools exit 1 on an empty result set — a quiet track, not
    # a failure. The collector must not flag it `!! FAILED`.
    [res] = run_tracks(
        {"quiet.txt": [_sh("echo '(cursor: 2026-10-11T07:00:00+00:00)'; "
                           "echo '(no matching messages)' >&2; exit 1")]},
        timeout=10)
    assert not res.failed
    assert res.nothing_new
    index = render_index("2026-10-11T12-00-00Z", [res])
    assert "quiet.txt — nothing new" in index
    assert "!! FAILED" not in res.output


def test_run_tracks_runs_in_parallel() -> None:
    # Two 0.4 s sleeps with timeout 2 s: serial would take ~0.8 s, parallel
    # ~0.4 s. Assert loosely (both complete well under the serial bound).
    import time
    start = time.monotonic()
    results = run_tracks(
        {"a.txt": [_sh("sleep 0.4; echo a")],
         "b.txt": [_sh("sleep 0.4; echo b")]}, timeout=2)
    elapsed = time.monotonic() - start
    assert {r.output.strip() for r in results} == {"a", "b"}
    assert elapsed < 0.75


def test_render_index_empty_vs_content_vs_failed() -> None:
    results = [
        TrackResult(filename="mail.txt", output="some mail\nmore\n"),
        TrackResult(filename="spond.txt", output=""),
        TrackResult(filename="backup-health.txt",
                    output="Result=exit-code\n!! FAILED (exit 1)\n",
                    exit_code=1),
    ]
    index = render_index("2026-10-11T12-00-00Z", results)
    assert "# sjekk-collect 2026-10-11T12-00-00Z" in index
    assert "mail.txt — 2 lines" in index
    assert "spond.txt — EMPTY" in index
    assert "**backup-health.txt** — !! FAILED (exit 1)" in index


def test_write_run_layout_and_latest_symlink(tmp_path) -> None:
    results = [TrackResult(filename="mail.txt", output="hi\n"),
               TrackResult(filename="spond.txt", output="")]
    run1 = write_run(tmp_path, results,
                     now=dt.datetime(2026, 10, 11, 12, 0, 0, tzinfo=dt.UTC))
    assert run1.name == "2026-10-11T12-00-00Z"
    assert (run1 / "mail.txt").read_text() == "hi\n"
    assert (run1 / "INDEX.md").exists()
    latest = tmp_path / "latest"
    assert latest.is_symlink()
    assert latest.resolve() == run1

    # A second run repoints `latest`.
    run2 = write_run(tmp_path, results,
                     now=dt.datetime(2026, 10, 11, 13, 0, 0, tzinfo=dt.UTC))
    assert latest.resolve() == run2


def test_embed_health_track_is_per_source() -> None:
    """The runbook calls a source green on its own gap, so the collector must
    group by source — one aggregate would let a stale source hide behind a
    fresh one."""
    [argv] = TRACKS["embed-health.txt"]
    assert "GROUP BY source" in argv[-1]


def test_skolemelding_track_reads_the_triage_cursor() -> None:
    """Like vigilo.txt: headers of records since the hand-advanced cursor."""
    [argv] = TRACKS["skolemelding.txt"]
    assert argv == ["skolemeldingshow", "--since-cursor", "--headers-only"]


# --- Phase-A gating: vigilo / skolemelding sync only on notification mail --


def _write_state(tmp_path, track: str, last_run: str) -> None:
    (tmp_path / f"{track}-state.json").write_text(
        json.dumps({"last_successful_run": last_run}))


def test_last_sync_epoch_parses_state(tmp_path) -> None:
    _write_state(tmp_path, "vigilo", "2026-10-13T11:16:28Z")
    got = sjekk_collect._last_sync_epoch(tmp_path / "vigilo-state.json")
    assert got == dt.datetime(
        2026, 10, 13, 11, 16, 28, tzinfo=dt.UTC).timestamp()


def test_last_sync_epoch_unknown_states(tmp_path) -> None:
    assert sjekk_collect._last_sync_epoch(tmp_path / "missing.json") is None
    bad = tmp_path / "vigilo-state.json"
    bad.write_text("not json")
    assert sjekk_collect._last_sync_epoch(bad) is None
    bad.write_text(json.dumps({"auth_required": False}))
    assert sjekk_collect._last_sync_epoch(bad) is None


def test_latest_notification_epoch_parses_summary(monkeypatch) -> None:
    payload = json.dumps([{"thread": "t", "timestamp": 1791890119,
                           "authors": "x", "subject": "y"}])
    monkeypatch.setattr(
        sjekk_collect, "_run_one",
        lambda argv, timeout: TrackResult(filename="", output=payload))
    assert sjekk_collect._latest_notification_epoch("subject:x") == 1791890119


def test_latest_notification_epoch_no_hits_is_none(monkeypatch) -> None:
    monkeypatch.setattr(
        sjekk_collect, "_run_one",
        lambda argv, timeout: TrackResult(filename="", output="[]"))
    assert sjekk_collect._latest_notification_epoch("subject:x") is None


def test_latest_notification_epoch_failure_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        sjekk_collect, "_run_one",
        lambda argv, timeout: TrackResult(filename="", output="boom",
                                          exit_code=2))
    with pytest.raises(RuntimeError):
        sjekk_collect._latest_notification_epoch("subject:x")


_NOW = dt.datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt.UTC)


def test_notification_pending_no_state_file_syncs(tmp_path) -> None:
    assert sjekk_collect.notification_pending("vigilo", state_dir=tmp_path,
                                              now=_NOW)


def test_notification_pending_no_mail_skips(tmp_path, monkeypatch) -> None:
    _write_state(tmp_path, "vigilo", "2026-10-13T11:00:00Z")
    monkeypatch.setattr(sjekk_collect, "_latest_notification_epoch",
                        lambda query: None)
    assert not sjekk_collect.notification_pending("vigilo", state_dir=tmp_path,
                                                  now=_NOW)


def test_notification_pending_mail_newer_than_sync(tmp_path,
                                                   monkeypatch) -> None:
    _write_state(tmp_path, "vigilo", "2026-10-13T11:00:00Z")
    newer = dt.datetime(2026, 10, 13, 11, 30, tzinfo=dt.UTC).timestamp()
    monkeypatch.setattr(sjekk_collect, "_latest_notification_epoch",
                        lambda query: newer)
    assert sjekk_collect.notification_pending("vigilo", state_dir=tmp_path,
                                              now=_NOW)


def test_notification_pending_mail_older_than_sync_skips(tmp_path,
                                                         monkeypatch) -> None:
    _write_state(tmp_path, "skolemelding", "2026-10-13T11:00:00Z")
    older = dt.datetime(2026, 10, 13, 10, 30, tzinfo=dt.UTC).timestamp()
    monkeypatch.setattr(sjekk_collect, "_latest_notification_epoch",
                        lambda query: older)
    assert not sjekk_collect.notification_pending(
        "skolemelding", state_dir=tmp_path, now=_NOW)


def test_notification_pending_stale_sync_falls_back(tmp_path,
                                                    monkeypatch) -> None:
    # Last sync older than NOTIFY_FALLBACK_AGE → sync even with no mail, in
    # case notifications silently stopped.
    _write_state(tmp_path, "vigilo", "2026-10-01T11:00:00Z")
    monkeypatch.setattr(sjekk_collect, "_latest_notification_epoch",
                        lambda query: None)
    assert sjekk_collect.notification_pending("vigilo", state_dir=tmp_path,
                                              now=_NOW)


def test_notification_pending_notmuch_error_syncs(tmp_path,
                                                  monkeypatch) -> None:
    _write_state(tmp_path, "vigilo", "2026-10-13T11:00:00Z")

    def _boom(query):
        raise RuntimeError("notmuch failed")

    monkeypatch.setattr(sjekk_collect, "_latest_notification_epoch", _boom)
    assert sjekk_collect.notification_pending("vigilo", state_dir=tmp_path,
                                              now=_NOW)


def test_notify_queries_are_service_subjects() -> None:
    """Gating keys must match the track names used in phase_a."""
    assert set(sjekk_collect.NOTIFY_QUERIES) == {"vigilo", "skolemelding"}
