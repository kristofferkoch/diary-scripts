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


@pytest.fixture
def with_trusted_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the fixed trusted-config path at a temp file and reset its cache."""
    def _write(text: str) -> Path:
        p = tmp_path / "trusted.toml"
        p.write_text(text)
        monkeypatch.setattr(config, "TRUSTED_CONFIG_PATH", p)
        config.reload()
        return p
    yield _write
    config.reload()


def test_host_urls_come_from_trusted_config(with_trusted_config):
    with_trusted_config(
        """
        [hosts]
        embed = "studio.example:8081"
        vlm = "studio.example:8083"
        immich = "server.example:2283"
        tailnet_base = "https://server.example"
        dsh_web = "https://server.example:8444"
        kimi_web = "https://server.example:8446"
        """
    )
    assert config.embed_url() == "http://studio.example:8081"
    assert config.vlm_url() == "http://studio.example:8083"
    assert config.immich_url() == "http://server.example:2283"
    assert config.mail_web_url() == "https://server.example/mail"
    assert config.dsh_web_url() == "https://server.example:8444"
    assert config.kimi_web_url() == "https://server.example:8446"


def test_agent_config_and_env_cannot_redirect_host_urls(
    with_config, with_trusted_config, monkeypatch: pytest.MonkeyPatch
):
    with_config(
        """
        [hosts]
        embed = "attacker.invalid:1"
        vlm = "attacker.invalid:2"
        immich = "attacker.invalid:3"
        tailnet_base = "https://attacker.invalid"
        dsh_web = "https://attacker.invalid:4"
        kimi_web = "https://attacker.invalid:5"
        """
    )
    with_trusted_config(
        """
        [hosts]
        embed = "studio.example:8081"
        vlm = "studio.example:8083"
        immich = "server.example:2283"
        tailnet_base = "https://server.example"
        dsh_web = "https://server.example:8444"
        kimi_web = "https://server.example:8446"
        """
    )
    for name in ("EMBED_URL", "VLM_URL", "IMMICH_URL", "MAIL_WEB_URL",
                 "DSH_WEB_URL", "KIMI_WEB_URL"):
        monkeypatch.setenv(name, "https://attacker.invalid/")

    assert config.embed_url() == "http://studio.example:8081"
    assert config.vlm_url() == "http://studio.example:8083"
    assert config.immich_url() == "http://server.example:2283"
    assert config.mail_web_url() == "https://server.example/mail"
    assert config.dsh_web_url() == "https://server.example:8444"
    assert config.kimi_web_url() == "https://server.example:8446"


def test_missing_trusted_config_does_not_fall_back_to_agent_config(
    with_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with_config('[hosts]\nembed = "attacker.invalid:9999"\n')
    monkeypatch.setattr(config, "TRUSTED_CONFIG_PATH", tmp_path / "missing.toml")
    monkeypatch.setenv("EMBED_URL", "http://attacker.invalid:9999")
    config.reload()
    assert config.embed_url() == "http://gpu-host:8081"


def test_explicit_missing_config_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DIARY_CONFIG", "/no/such/config.toml")
    config.reload()
    with pytest.raises(FileNotFoundError):
        config.cfg("anything", None)
    config.reload()
