"""Make the test suite hermetic with respect to runtime config.

The code reads real deployment values from a private config file
($DIARY_CONFIG or ~/.config/diary/config.toml) — see mail_reader/config.py.
Tests must NOT depend on whether that file happens to exist on the machine
running them: they assert against the placeholder values and fixtures shipped
in this repo. So neutralise any ambient config before test modules (and the
import-time constants in priority.py / kindle_dashboard/data.py) are imported.

This runs at conftest import, i.e. before test collection. Individual tests
that need a config (mail_reader/test_config.py) set DIARY_CONFIG themselves
via monkeypatch + config.reload().
"""
import os
from pathlib import Path

os.environ.pop("DIARY_CONFIG", None)
os.environ["XDG_CONFIG_HOME"] = "/nonexistent-diary-xdg-for-tests"
# Pin the workspace root to this repo dir so workspace_root() is deterministic
# in tests and never autodetects a real CALENDAR.md on the dev machine.
os.environ.setdefault("DIARY_ROOT", str(Path(__file__).resolve().parent))
