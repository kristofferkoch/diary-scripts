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
    assert got == p.resolve().parent / "senders.txt"


def test_cfg_path_relative_resolves_through_symlink(tmp_path, monkeypatch):
    real_dir = tmp_path / "diary" / "config"
    real_dir.mkdir(parents=True)
    real = real_dir / "config.toml"
    real.write_text('important_senders = "senders.txt"\n')
    link_dir = tmp_path / ".config" / "diary"
    link_dir.mkdir(parents=True)
    link = link_dir / "config.toml"
    link.symlink_to(real)
    monkeypatch.setenv("DIARY_CONFIG", str(link))
    config.reload()
    got = config.cfg_path("important_senders", Path("/fallback/senders.txt"))
    assert got == real_dir / "senders.txt"   # next to the real file, not the link
    config.reload()


def test_cfg_path_absolute_is_untouched(with_config):
    with_config('important_senders = "/etc/diary/senders.txt"\n')
    got = config.cfg_path("important_senders", Path("/fallback/senders.txt"))
    assert got == Path("/etc/diary/senders.txt")


def test_cfg_path_default_when_absent(with_config):
    with_config("# empty config\n")
    fallback = Path("/fallback/senders.txt")
    assert config.cfg_path("important_senders", fallback) == fallback


def test_host_urls_come_from_config(with_config, monkeypatch: pytest.MonkeyPatch):
    """Regression for 2026-06-02: the host must be read from `hosts.*`, not a
    hardcoded `gpu-host` default that silently leaked when $OLLAMA_URL was unset."""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("MLX_BASE", raising=False)
    monkeypatch.delenv("NUEXTRACT_BASE", raising=False)
    with_config(
        """
        [hosts]
        llm = "studio.example:11434"
        mlx = "studio.example:8080"
        nuextract = "studio.example:8081"
        """
    )
    assert config.ollama_url() == "http://studio.example:11434"
    assert config.mlx_url() == "http://studio.example:8080"
    assert config.nuextract_url() == "http://studio.example:8081"


def test_host_url_env_overrides_config(with_config, monkeypatch: pytest.MonkeyPatch):
    with_config('[hosts]\nllm = "studio.example:11434"\n')
    monkeypatch.setenv("OLLAMA_URL", "http://override:9999/")
    assert config.ollama_url() == "http://override:9999"  # env wins, trailing / trimmed


def test_host_url_falls_back_to_placeholder_when_unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("DIARY_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-xdg-dir")
    config.reload()
    assert config.ollama_url() == "http://gpu-host:11434"


def test_summaries_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch):
    """Public submodule default: no config, no env → summaries run."""
    monkeypatch.delenv("SUMMARY_ENABLED", raising=False)
    monkeypatch.delenv("DIARY_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-xdg-dir")
    config.reload()
    assert config.summaries_enabled() is True


def test_summaries_disabled_via_config(with_config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SUMMARY_ENABLED", raising=False)
    with_config("[summaries]\nenabled = false\n")
    assert config.summaries_enabled() is False


@pytest.mark.parametrize(
    "val,expected",
    [("0", False), ("false", False), ("off", False), ("no", False), ("", False),
     ("1", True), ("true", True), ("yes", True)],
)
def test_summaries_env_overrides_config(with_config, monkeypatch, val, expected):
    """Env var wins over config, in both directions."""
    with_config("[summaries]\nenabled = false\n")  # config says off
    monkeypatch.setenv("SUMMARY_ENABLED", val)
    assert config.summaries_enabled() is expected


def test_explicit_missing_config_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DIARY_CONFIG", "/no/such/config.toml")
    config.reload()
    with pytest.raises(FileNotFoundError):
        config.cfg("anything", None)
    config.reload()
