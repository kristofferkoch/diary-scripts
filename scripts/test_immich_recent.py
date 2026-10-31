"""Security boundary tests for Immich credential lookup."""
from __future__ import annotations

import subprocess

from scripts import immich_recent


def test_agent_config_cannot_replace_fixed_secret_argv(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run"
    config = tmp_path / "local.toml"
    config.write_text(f'[immich]\napi_key_cmd = "touch {marker}"\n')
    monkeypatch.setenv("DIARY_CONFIG", str(config))
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="read-only-key\n")

    monkeypatch.setattr(immich_recent.subprocess, "run", fake_run)

    assert immich_recent._api_key() == "read-only-key"
    assert calls == [(
        ("pass", "show", "immich/api-key"),
        {"check": True, "capture_output": True, "text": True},
    )]
    assert not marker.exists()
