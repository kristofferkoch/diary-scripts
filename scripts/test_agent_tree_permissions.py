"""Filesystem race and metadata isolation tests for agent tree repair."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts import agent_tree_permissions as permissions


def _account() -> permissions.Account:
    return permissions.Account("current", os.getuid(), os.getgid())


def _entries_for(entries: list[permissions.Entry], suffix: tuple[str, ...]) -> list[permissions.Entry]:
    return [entry for entry in entries if entry.components[-len(suffix) :] == suffix]


def test_fd_repair_changes_only_the_open_regular_file(tmp_path: Path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "file"
    target.write_text("data")
    target.chmod(0o600)
    monkeypatch.setattr(permissions, "_drop_privileges", lambda account: None)
    root_fd, entries = permissions._inspect(root, {os.getuid()})
    try:
        file_entry = _entries_for(entries, ("file",))
        permissions._run_owner(root_fd, root, file_entry, _account(), os.getgid())
    finally:
        os.close(root_fd)

    assert stat.S_IMODE(target.stat().st_mode) == 0o660
    assert target.stat().st_gid == os.getgid()
    assert target.read_text() == "data"


def test_parent_rename_and_intermediate_symlink_cannot_escape(tmp_path: Path):
    root = tmp_path / "tree"
    nested = root / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    target = nested / "file"
    target.write_text("inside")
    outside_target = outside / "file"
    outside_target.write_text("outside")
    outside_target.chmod(0o600)
    root_fd, entries = permissions._inspect(root, {os.getuid()})
    try:
        entry = _entries_for(entries, ("nested", "file"))[0]
        nested.rename(root / "original")
        nested.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            permissions._open_target(root_fd, root, entry)
    finally:
        os.close(root_fd)

    assert stat.S_IMODE(outside_target.stat().st_mode) == 0o600
    assert outside_target.read_text() == "outside"


def test_final_symlink_substitution_cannot_escape(tmp_path: Path):
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "file"
    target.write_text("inside")
    outside_target = outside / "target"
    outside_target.write_text("outside")
    outside_target.chmod(0o600)
    root_fd, entries = permissions._inspect(root, {os.getuid()})
    try:
        entry = _entries_for(entries, ("file",))[0]
        target.unlink()
        target.symlink_to(outside_target)
        with pytest.raises(OSError):
            permissions._open_target(root_fd, root, entry)
    finally:
        os.close(root_fd)

    assert stat.S_IMODE(outside_target.stat().st_mode) == 0o600
    assert outside_target.read_text() == "outside"


def test_hardlink_is_rejected_before_any_mutation(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    unchanged = root / "unchanged"
    unchanged.write_text("good")
    unchanged.chmod(0o600)
    linked = root / "linked"
    linked.write_text("shared")
    linked.chmod(0o600)
    outside = tmp_path / "outside"
    os.link(linked, outside)

    with pytest.raises(permissions.TreeError, match="multiply linked"):
        permissions.repair_tree(root, [_account()], os.getgid())

    assert stat.S_IMODE(unchanged.stat().st_mode) == 0o600
    assert stat.S_IMODE(linked.stat().st_mode) == 0o600
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_unexpected_owner_is_rejected_before_any_mutation(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "file"
    target.write_text("data")
    target.chmod(0o600)

    with pytest.raises(permissions.TreeError, match="unexpected owner"):
        permissions.repair_tree(root, [permissions.Account("other", os.getuid() + 1, os.getgid())], os.getgid())

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_symlink_is_rejected_during_preflight(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside")
    outside.chmod(0o600)
    (root / "link").symlink_to(outside)

    with pytest.raises(permissions.TreeError, match="symlink"):
        permissions.inspect_tree(root, {os.getuid()})

    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_nested_mount_table_entry_is_rejected_without_path_traversal(tmp_path: Path):
    root = tmp_path / "tree root"
    mountinfo = "26 1 8:1 / /tmp/example rw - ext4 /dev/example rw\n"
    mountinfo += f"27 26 8:2 / {str(root / 'bind mount').replace(' ', chr(92) + '040')} rw - ext4 /dev/other rw\n"

    with pytest.raises(permissions.TreeError, match="nested mount"):
        permissions._reject_nested_mounts(root, mountinfo)


def test_directory_mode_includes_group_access_and_setgid():
    entry = permissions.Entry((), 1, 2, stat.S_IFDIR | 0o700, os.getuid(), os.getgid(), 2)
    assert permissions._desired_mode(entry) == 0o2770


def test_regular_file_mode_does_not_restore_setuid_or_setgid():
    entry = permissions.Entry((), 1, 2, stat.S_IFREG | 0o6600, os.getuid(), os.getgid(), 1)
    assert permissions._desired_mode(entry) == 0o660
