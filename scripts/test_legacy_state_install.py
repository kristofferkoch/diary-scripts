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


def test_cursor_parent_replacement_cannot_escape_dirfd(roots, tmp_path: Path, monkeypatch):
    source, destination = roots
    moved = tmp_path / "moved-runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_link = os.link
    replaced = False

    def replace_parent(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            destination.rename(moved)
            destination.symlink_to(outside, target_is_directory=True)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", replace_parent)
    migrate(source, destination)
    assert (moved / "spond-state.json").read_text() == '{"track":"spond"}\n'
    assert not (outside / "spond-state.json").exists()


def test_jsonl_parent_replacement_cannot_escape_dirfd(roots, tmp_path: Path, monkeypatch):
    source, destination = roots
    moved = tmp_path / "moved-spond"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_link = os.link
    replaced = False

    def replace_parent(*args, **kwargs):
        nonlocal replaced
        if not replaced and str(args[1]).endswith(".jsonl"):
            replaced = True
            (destination / "spond").rename(moved)
            (destination / "spond").symlink_to(outside, target_is_directory=True)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", replace_parent)
    migrate(source, destination)
    assert (moved / "2026-11-01.jsonl").read_text() == '{"track":"spond"}\n'
    assert not (outside / "2026-11-01.jsonl").exists()


def test_nested_attachments_and_both_legacy_prefixes_are_migrated(roots):
    source, destination = roots
    nested = source / "vigilo/attachments/thread/nested"
    nested.mkdir(parents=True)
    (nested / "document.pdf").write_bytes(b"%PDF-test")
    (source / "vigilo/2026-11-01.jsonl").write_text(
        '{"data":{"messages":[{"attachments":[{"path":'
        '"/home/kek/diary/memory/vigilo/attachments/thread/nested/document.pdf"}]}]}}\n'
    )
    (source / "skolemelding/2026-11-01.jsonl").write_text(
        '{"data":{"attachments":[{"path":'
        '"/etc/diary/workspace/memory/skolemelding/attachments/42/plan.pdf"}]}}\n'
    )
    (source / "skolemelding/attachments/42").mkdir(parents=True)
    (source / "skolemelding/attachments/42/plan.pdf").write_bytes(b"plan")

    migrate(source, destination)

    assert (destination / "vigilo/attachments/thread/nested/document.pdf").read_bytes() == b"%PDF-test"
    assert (destination / "skolemelding/attachments/42/plan.pdf").read_bytes() == b"plan"
    vigilo = (destination / "vigilo/2026-11-01.jsonl").read_text()
    skolemelding = (destination / "skolemelding/2026-11-01.jsonl").read_text()
    assert "/var/lib/diary/legacy-state/vigilo/attachments/thread/nested/document.pdf" in vigilo
    assert "/var/lib/diary/legacy-state/skolemelding/attachments/42/plan.pdf" in skolemelding
    assert "/home/kek/diary/memory" not in vigilo + skolemelding
    assert "/etc/diary/workspace/memory" not in vigilo + skolemelding


def test_repeat_run_normalizes_old_destination_attachment_paths(roots):
    source, destination = roots
    migrate(source, destination)
    path = destination / "vigilo/2026-11-01.jsonl"
    path.write_text(
        '{"data":{"attachments":[{"path":'
        '"/home/kek/diary/memory/vigilo/attachments/old.pdf"}]}}\n'
    )
    migrate(source, destination)
    assert "/var/lib/diary/legacy-state/vigilo/attachments/old.pdf" in path.read_text()


def test_write_all_handles_short_writes(monkeypatch):
    from scripts.legacy_state_install import _write_all

    chunks = []
    monkeypatch.setattr(os, "write", lambda fd, data: chunks.append(data) or min(2, len(data)))
    _write_all(1, b"abcdef")
    assert chunks == [b"abcdef", b"cdef", b"ef"]


def test_all_source_tracks_absent_after_first_migration_preserve_destination(roots, tmp_path):
    source, destination = roots
    migrate(source, destination)
    for track in ("spond", "vigilo", "skolemelding"):
        (source / track).rename(tmp_path / f"deleted-{track}")

    result = migrate(source, destination)

    assert result["copied"] == 0
    for track in ("spond", "vigilo", "skolemelding"):
        assert (destination / f"{track}-state.json").read_text() == f'{{"track":"{track}"}}\n'
        assert (destination / track / "2026-11-01.jsonl").read_text() == f'{{"track":"{track}"}}\n'


def test_attachment_temp_is_unlinked_before_final_directory_sync(roots, monkeypatch):
    source, destination = roots
    attachment = source / "vigilo/attachments/crash.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"attachment")
    operations = []
    linked = False
    real_link = os.link
    real_fsync = os.fsync
    real_unlink = os.unlink

    def link(*args, **kwargs):
        nonlocal linked
        if args[1] == "crash.pdf":
            linked = True
            operations.append("link")
        return real_link(*args, **kwargs)

    def fsync(fd):
        if linked:
            operations.append("dir-fsync")
            raise OSError("simulated crash after attachment link")
        return real_fsync(fd)

    def unlink(path, **kwargs):
        operations.append("unlink")
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(OSError, match="simulated crash"):
        migrate(source, destination)
    assert operations.index("unlink") < operations.index("dir-fsync")
    final = destination / "vigilo/attachments/crash.pdf"
    assert final.stat().st_nlink == 1
    assert list(final.parent.glob("*.migrate.tmp")) == []


def test_malformed_truncated_and_nonobject_lines_survive_with_valid_path_conversion(roots):
    source, destination = roots
    path = source / "vigilo/2026-11-01.jsonl"
    path.write_bytes(
        b'{"data":{"attachments":[{"path":"/home/kek/diary/memory/vigilo/attachments/a.pdf"}]}}\n'
        b'{"unterminated middle line\n'
        b'[1,2]\n'
        b'"valid scalar"\n'
        b'\xff\n'
        b'{"data":{"attachments":[{"path":"/etc/diary/workspace/memory/vigilo/attachments/b.pdf"}]}}\r\n'
        b'{"data":{"truncated":'
    )

    migrate(source, destination)
    lines = (destination / "vigilo/2026-11-01.jsonl").read_bytes().splitlines(keepends=True)

    assert b"/var/lib/diary/legacy-state/vigilo/attachments/a.pdf" in lines[0]
    assert lines[1] == b'{"unterminated middle line\n'
    assert lines[2] == b'[1,2]\n'
    assert lines[3] == b'"valid scalar"\n'
    assert lines[4] == b'\xff\n'
    assert b"/var/lib/diary/legacy-state/vigilo/attachments/b.pdf" in lines[5]
    assert lines[5].endswith(b"\r\n")
    assert lines[6] == b'{"data":{"truncated":'
    assert b"/home/kek/diary/memory" not in lines[0]
    assert b"/etc/diary/workspace/memory" not in lines[5]

    original = (destination / "vigilo/2026-11-01.jsonl").read_bytes()
    result = migrate(source, destination)
    assert result["identical"] == 6
    assert (destination / "vigilo/2026-11-01.jsonl").read_bytes() == original
