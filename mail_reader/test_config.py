"""Tests for the config overlay (mail_reader/config.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mail_reader import config


@pytest.fixture
def with_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point DIARY_CONFIG at a temp TOML and reset the cache around the test."""
    def _write(text: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(text)
        monkeypatch.setenv("DIARY_CONFIG", str(p))
        config.reload()
        return p
    yield _write
    config.reload()


def test_missing_config_returns_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DIARY_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-xdg-dir")
    config.reload()
    assert config.cfg("weather.lat", 59.913) == 59.913
    assert config.cfg("a.b.c", "fallback") == "fallback"


def test_override_scalar_and_nested(with_config):
    with_config(
        """
        [weather]
        lat = 1.25
        lon = 6.50
        [family]
        members = ["Alpha", "Beta", "Gamma"]
        """
    )
    assert config.cfg("weather.lat", 59.913) == 1.25
    assert config.cfg("family.members", []) == ["Alpha", "Beta", "Gamma"]
    # absent key still falls back
    assert config.cfg("weather.ua", "default-ua") == "default-ua"


def test_inline_table_member_ids(with_config):
    with_config('[spond]\nmember_ids = { "ABC123" = "H", "DEF456" = "E" }\n')
    assert config.cfg("spond.member_ids", {}) == {"ABC123": "H", "DEF456": "E"}


def test_cfg_path_relative_resolves_against_config_dir(with_config):
    p = with_config('important_senders = "senders.txt"\n')
    got = config.cfg_path("important_senders", Path("/fallback/senders.txt"))
    assert got == p.parent / "senders.txt"


def test_cfg_path_absolute_is_untouched(with_config):
    with_config('important_senders = "/etc/diary/senders.txt"\n')
    got = config.cfg_path("important_senders", Path("/fallback/senders.txt"))
    assert got == Path("/etc/diary/senders.txt")


def test_cfg_path_default_when_absent(with_config):
    with_config("# empty config\n")
    fallback = Path("/fallback/senders.txt")
    assert config.cfg_path("important_senders", fallback) == fallback


def test_explicit_missing_config_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DIARY_CONFIG", "/no/such/config.toml")
    config.reload()
    with pytest.raises(FileNotFoundError):
        config.cfg("anything", None)
    config.reload()
