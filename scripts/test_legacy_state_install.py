"""Tests for idempotent, no-follow legacy collector-state migration."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.legacy_state_install import MigrationConflict, migrate_legacy_state


@pytest.fixture(autouse=True)
def sandbox_special_bits(monkeypatch: pytest.MonkeyPatch):
    real_fchmod = os.fchmod
    real_mkdir = os.mkdir
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: real_fchmod(fd, mode & 0o777))
    monkeypatch.setattr(
        os, "mkdir", lambda path, mode, **kwargs: real_mkdir(path, mode & 0o777, **kwargs)
    )


@pytest.fixture
def roots(tmp_path: Path):
    source = tmp_path / "memory"
    destination = tmp_path / "legacy-state"
    for track in ("spond", "vigilo", "skolemelding"):
        (source / track).mkdir(parents=True)
        (source / f"{track}-state.json").write_text(f'{{"track":"{track}"}}\n')
        (source / track / "2026-11-01.jsonl").write_text(f'{{"track":"{track}"}}\n')
    return source, destination


def migrate(source: Path, destination: Path):
    return migrate_legacy_state(source, destination, uid=os.getuid(), gid=os.getgid())


def test_first_and_repeat_runs_copy_then_preserve_everything(roots):
    source, destination = roots
    first = migrate(source, destination)
    assert first["copied"] == 6
    assert (destination / "spond-state.json").read_text() == '{"track":"spond"}\n'
    assert (destination / "vigilo/2026-11-01.jsonl").read_text() == '{"track":"vigilo"}\n'
    assert destination.stat().st_mode & 0o777 == 0o775
    assert (destination / "spond").stat().st_mode & 0o777 == 0o775
    assert (destination / "spond-state.json").stat().st_mode & 0o777 == 0o664
    assert (source / "spond-state.json").exists()

    second = migrate(source, destination)
    assert second["copied"] == 0
    assert second["identical"] == 6
    assert (destination / "skolemelding-state.json").read_text() == '{"track":"skolemelding"}\n'


def test_newer_different_destination_is_preserved(roots):
    source, destination = roots
    migrate(source, destination)
    destination_file = destination / "spond/2026-11-01.jsonl"
    destination_file.write_text('{"destination":"newer"}\n')
    source_file = source / "spond/2026-11-01.jsonl"
    source_file.write_text('{"source":"older"}\n')
    os.utime(source_file, ns=(1, 1))
    os.utime(destination_file, ns=(2_000_000_000, 2_000_000_000))

    result = migrate(source, destination)

    assert result["preserved-newer"] == 1
    assert destination_file.read_text() == '{"destination":"newer"}\n'


def test_older_different_destination_conflicts_without_clobber(roots):
    source, destination = roots
    migrate(source, destination)
    destination_file = destination / "spond/2026-11-01.jsonl"
    destination_file.write_text('{"destination":"older"}\n')
    source_file = source / "spond/2026-11-01.jsonl"
    source_file.write_text('{"source":"newer"}\n')
    os.utime(destination_file, ns=(1, 1))
    os.utime(source_file, ns=(2_000_000_000, 2_000_000_000))

    with pytest.raises(MigrationConflict):
        migrate(source, destination)
    assert destination_file.read_text() == '{"destination":"older"}\n'


def test_source_symlink_is_rejected(roots, tmp_path: Path):
    source, destination = roots
    target = tmp_path / "outside"
    target.write_text("unchanged")
    (source / "spond-state.json").unlink()
    (source / "spond-state.json").symlink_to(target)
    with pytest.raises(OSError):
        migrate(source, destination)
    assert target.read_text() == "unchanged"


def test_source_track_symlink_is_rejected(roots, tmp_path: Path):
    source, destination = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "spond").rename(outside / "spond")
    (source / "spond").symlink_to(outside / "spond", target_is_directory=True)
    with pytest.raises(OSError):
        migrate(source, destination)


def test_destination_symlink_is_rejected_without_following(roots, tmp_path: Path):
    source, destination = roots
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (destination / "spond-state.json").symlink_to(outside)
    with pytest.raises(OSError):
        migrate(source, destination)
    assert outside.read_text() == "unchanged"


def test_missing_sources_get_empty_cursors_without_jsonl(roots):
    source, destination = roots
    for track in ("spond", "vigilo", "skolemelding"):
        (source / f"{track}-state.json").unlink()
    result = migrate(source, destination)
    assert result["copied"] == 3
    for track in ("spond", "vigilo", "skolemelding"):
        assert (destination / f"{track}-state.json").read_text() == "{}\n"
