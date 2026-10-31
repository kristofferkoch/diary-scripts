"""Installer-level tests for owner-pinned first-run bootstrap."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def run(*args: str, cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    run("git", "add", name, cwd=repo)
    run("git", "commit", "-m", message, cwd=repo)
    return run("git", "rev-parse", "HEAD", cwd=repo)


def test_substituted_integration_master_aborts_before_install_or_restart(tmp_path: Path):
    source = os.environ.get("DIARY_BOOTSTRAP_INSTALLER")
    if source is None:
        pytest.skip("set DIARY_BOOTSTRAP_INSTALLER to test the diary installer")
    installer_text = Path(source).read_text()

    integration = tmp_path / "integration"
    integration.mkdir()
    run("git", "init", "-b", "master", cwd=integration)
    run("git", "config", "user.name", "Bootstrap Test", cwd=integration)
    run("git", "config", "user.email", "bootstrap@example.invalid", cwd=integration)
    reviewed = commit(integration, "README.md", "reviewed\n", "reviewed")
    run("git", "init", "--bare", str(tmp_path / "origin.git"), cwd=tmp_path)
    run("git", "remote", "add", "origin", str(tmp_path / "origin.git"), cwd=integration)
    substituted = commit(integration, "AGENTS.md", "unreviewed\n", "substitute integration master")
    assert substituted != reviewed

    approved = tmp_path / "approved"
    replacements = {"REPO": integration, "TRUSTED": approved, "OWNER": "owner"}
    lines = installer_text.splitlines()
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if separator and key in replacements:
            lines[index] = f"{key}={replacements[key]}"
    installer = tmp_path / "install"
    installer.write_text("\n".join(lines) + "\n")

    log = tmp_path / "operations.log"
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    (mock_bin / "runuser").write_text("""#!/bin/bash
printf 'runuser' >> "$BOOTSTRAP_TEST_LOG"
printf ' %s' "$@" >> "$BOOTSTRAP_TEST_LOG"
printf '\n' >> "$BOOTSTRAP_TEST_LOG"
if [[ $1 == -u ]]; then shift 2; fi
if [[ $1 == -- ]]; then shift; fi
exec "$@"
""")
    for name in ("systemctl", "install"):
        (mock_bin / name).write_text(f"""#!/bin/bash
printf '{name}' >> "$BOOTSTRAP_TEST_LOG"
printf ' %s' "$@" >> "$BOOTSTRAP_TEST_LOG"
printf '\n' >> "$BOOTSTRAP_TEST_LOG"
""")
    for command in mock_bin.iterdir():
        command.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{mock_bin}:{env['PATH']}",
        "BOOTSTRAP_TEST_LOG": str(log),
        "DIARY_APPROVED_OID": reviewed,
        "DIARY_SCRIPTS_APPROVED_OID": "b" * 40,
    })
    proc = subprocess.run(
        ["bash", str(installer)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode != 0
    assert "does not match owner OID" in proc.stderr
    assert not (approved / "README.md").exists(), "no checkout happens before pin verification"
    operations = log.read_text().splitlines()
    assert operations == [
        "systemctl disable --now agent-merge.socket",
        "runuser -u owner -- git -C " + str(integration) + " remote get-url origin",
        "runuser -u owner -- git clone --no-checkout --no-hardlinks " + str(integration) + " " + str(approved),
        "runuser -u owner -- git -C " + str(approved) + " rev-parse master^{commit}",
    ]
    assert not any(line.startswith("install ") for line in operations)
    assert not any("uv sync" in line for line in operations)
    assert not any("try-restart" in line or "enable --now" in line for line in operations)
