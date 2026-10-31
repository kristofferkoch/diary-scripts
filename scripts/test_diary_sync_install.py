"""Tests for the dirfd-safe diary-sync installer preparation."""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

import scripts.diary_sync_install as installer
from scripts.diary_sync_install import TRACKS, prepare_memory, require_unix_socket


def test_socket_preflight_accepts_a_real_unix_socket(tmp_path: Path):
    path = tmp_path / "bus"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(path))
        require_unix_socket(path)
    regular = tmp_path / "regular"
    regular.write_text("not a socket")
    with pytest.raises(RuntimeError, match="not a Unix socket"):
        require_unix_socket(regular)


@pytest.fixture
def requested_modes(monkeypatch):
    calls = []
    real_fchmod = os.fchmod
    real_mkdir = os.mkdir

    def without_sandbox_special_bits(fd, mode):
        calls.append(mode)
        real_fchmod(fd, mode & 0o777)

    def mkdir_without_sandbox_special_bits(path, mode, **kwargs):
        return real_mkdir(path, mode & 0o777, **kwargs)

    monkeypatch.setattr(os, "fchmod", without_sandbox_special_bits)
    monkeypatch.setattr(os, "mkdir", mkdir_without_sandbox_special_bits)
    return calls


def test_prepare_memory_creates_valid_state_and_pins_mount_sources(tmp_path: Path, requested_modes):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "spond-state.json").write_text("")

    prepare_memory(memory, os.getuid(), os.getgid())

    assert requested_modes[0] == 0o3775
    assert memory.stat().st_mode & 0o777 == 0o775
    for track in TRACKS:
        output = memory / track
        state = memory / f"{track}-state.json"
        assert output.is_dir() and not output.is_symlink()
        assert output.stat().st_mode & 0o777 == 0o775
        assert state.is_file() and not state.is_symlink()
        assert state.stat().st_mode & 0o777 == 0o664
        assert state.read_text() == "{}\n"
    assert requested_modes.count(0o2775) == len(TRACKS)

    state = memory / "spond-state.json"
    state.write_text('{"last_successful_run": "kept"}\n')
    prepare_memory(memory, os.getuid(), os.getgid())
    assert state.read_text() == '{"last_successful_run": "kept"}\n'


def test_prepare_memory_rejects_an_output_directory_symlink(tmp_path: Path, requested_modes):
    memory = tmp_path / "memory"
    outside = tmp_path / "outside"
    memory.mkdir()
    outside.mkdir()
    (memory / "spond").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        prepare_memory(memory, os.getuid(), os.getgid())
    assert outside.is_dir()
    assert not (memory / "spond-state.json").exists()


def test_prepare_memory_rejects_a_state_symlink_without_touching_target(tmp_path: Path, requested_modes):
    memory = tmp_path / "memory"
    memory.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("unchanged\n")
    original_mode = target.stat().st_mode
    (memory / "spond-state.json").symlink_to(target)

    with pytest.raises(OSError):
        prepare_memory(memory, os.getuid(), os.getgid())
    assert target.read_text() == "unchanged\n"
    assert target.stat().st_mode == original_mode


def test_isolated_module_launch_ignores_decoy_package_in_invoking_cwd(tmp_path: Path):
    decoy_cwd = tmp_path / "decoy"
    decoy_package = decoy_cwd / "scripts"
    decoy_package.mkdir(parents=True)
    (decoy_package / "__init__.py").write_text("")
    (decoy_package / "diary_sync_install.py").write_text(
        'raise RuntimeError("decoy package executed")\n'
    )
    trusted_root = Path(installer.__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(trusted_root)

    result = subprocess.run(
        ["/usr/bin/python3", "-P", "-m", "scripts.diary_sync_install", "--help"],
        cwd=decoy_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "decoy package executed" not in result.stderr


def test_prepare_memory_detects_directory_rename_during_metadata(
        tmp_path: Path, monkeypatch, requested_modes):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "spond").mkdir()
    real_fchmod = os.fchmod
    replaced = False

    def replace_directory(fd, mode):
        nonlocal replaced
        real_fchmod(fd, mode)
        if mode == 0o2775 and not replaced:
            replaced = True
            os.rename(memory / "spond", memory / "spond-old")
            os.mkdir(memory / "spond", 0o775)

    monkeypatch.setattr(os, "fchmod", replace_directory)
    with pytest.raises(RuntimeError, match="replaced while being secured"):
        prepare_memory(memory, os.getuid(), os.getgid())
    assert replaced is True


def test_prepare_memory_detects_state_rename_during_metadata(
        tmp_path: Path, monkeypatch, requested_modes):
    memory = tmp_path / "memory"
    memory.mkdir()
    state = memory / "spond-state.json"
    state.write_text("original\n")
    real_fchmod = os.fchmod
    replaced = False

    def replace_state(fd, mode):
        nonlocal replaced
        real_fchmod(fd, mode)
        if mode == 0o664 and not replaced:
            replaced = True
            os.rename(state, memory / "spond-state-old.json")
            state.write_text("replacement\n")

    monkeypatch.setattr(os, "fchmod", replace_state)
    with pytest.raises(RuntimeError, match="replaced while being secured"):
        prepare_memory(memory, os.getuid(), os.getgid())
    assert replaced is True
