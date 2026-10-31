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


@pytest.mark.parametrize("config_uid", [0, 1000])
def test_shell_installer_requires_root_config_and_uses_approved_units(
        tmp_path: Path, config_uid: int):
    source = os.environ.get("DIARY_SYNC_INSTALLER")
    if source is None:
        pytest.skip("set DIARY_SYNC_INSTALLER to test the diary installer")
    integration = tmp_path / "integration"
    approved_diary = tmp_path / "diary-approved"
    approved_scripts = tmp_path / "scripts-approved"
    owner_home = tmp_path / "owner-home"
    trusted_config = tmp_path / "trusted.toml"
    bus = tmp_path / "bus"
    for path in (
        integration / "sync",
        approved_diary / "sync",
        approved_scripts / "scripts",
        owner_home / ".config/diary",
        owner_home / ".password-store/spond",
        owner_home / ".password-store/vigilo",
        owner_home / ".gnupg",
    ):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("diary-sync-broker.service", "diary-sync-runtime.service"):
        (integration / "sync" / name).write_text("unreviewed\n")
        (approved_diary / "sync" / name).write_text("reviewed\n")
    for name in ("diary_sync_broker.py", "diary_sync_install.py", "safe_io.py"):
        (approved_scripts / "scripts" / name).write_text("")
    for path in (
        owner_home / ".config/diary/vigilo-tokens.json",
        owner_home / ".config/diary/skolemelding-auth.json",
        owner_home / ".password-store/spond/user.gpg",
        owner_home / ".password-store/vigilo/client-id.gpg",
        owner_home / ".password-store/vigilo/client-secret.gpg",
        trusted_config,
    ):
        path.write_text("{}\n")

    replacements = {
        "REPO": integration,
        "APPROVED_DIARY": approved_diary,
        "APPROVED": approved_scripts,
        "TRUSTED_CONFIG": trusted_config,
        "USER_BUS": bus,
    }
    lines = Path(source).read_text().splitlines()
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if separator and key in replacements:
            lines[index] = f"{key}={replacements[key]}"
    installer = tmp_path / "install"
    installer.write_text(
        "\n".join(lines) + "\n"
    )
    installer_text = installer.read_text().replace(
        "if ((EUID != 0)); then", "if false; then", 1
    ).replace("/home/$OWNER", str(owner_home)).replace(
        "/usr/bin/python3", "python3"
    )
    installer.write_text(installer_text)

    log = tmp_path / "operations.log"
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    for name in ("install", "systemctl", "systemd-analyze", "python3", "stat"):
        (mock_bin / name).write_text(f"""#!/bin/bash
printf '{name}' >> "$SYNC_INSTALL_TEST_LOG"
printf ' %s' "$@" >> "$SYNC_INSTALL_TEST_LOG"
printf '\n' >> "$SYNC_INSTALL_TEST_LOG"
""")
    (mock_bin / "stat").write_text("""#!/bin/bash
printf 'stat' >> "$SYNC_INSTALL_TEST_LOG"
printf ' %s' "$@" >> "$SYNC_INSTALL_TEST_LOG"
printf '\n' >> "$SYNC_INSTALL_TEST_LOG"
printf '%s\n' "$SYNC_INSTALL_TEST_CONFIG_UID"
""")
    for command in mock_bin.iterdir():
        command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{mock_bin}:{env['PATH']}",
        "SYNC_INSTALL_TEST_LOG": str(log),
        "SYNC_INSTALL_TEST_CONFIG_UID": str(config_uid),
    })
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(bus))
        proc = subprocess.run(
            ["bash", str(installer)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    operations = log.read_text().splitlines()
    if config_uid != 0:
        assert proc.returncode != 0
        assert "trusted config must be root-owned" in proc.stderr
        assert not any(line.startswith("install ") for line in operations)
        return

    assert proc.returncode == 0, proc.stderr
    assert (
        "systemd-analyze verify "
        f"{approved_diary}/sync/diary-sync-runtime.service "
        f"{approved_diary}/sync/diary-sync-broker.service"
    ) in operations
    for name in ("diary-sync-runtime.service", "diary-sync-broker.service"):
        assert (
            "install -o root -g root -m 0644 "
            f"{approved_diary}/sync/{name} /etc/systemd/system/{name}"
        ) in operations
    assert not any(f"{integration}/sync" in line for line in operations)
    assert not any("config/local.toml" in line for line in operations)
    assert "systemctl restart diary-sync-runtime.service diary-sync-broker.service" in operations
