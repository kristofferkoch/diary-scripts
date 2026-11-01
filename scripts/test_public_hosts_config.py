"""Regression tests for the deterministic public-hosts migration."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mail_reader import hosts_config
from scripts import public_hosts_config as migration

HOSTS = """
[hosts]
embed = "gpu-host:8081"
vlm = "gpu-host:8080"
server = "server"
tailnet_base = "https://server"
dsh_web = "https://server:8444"
kimi_web = "https://server:8446"
immich = "server:2283"
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "trusted.toml"
    source.write_text(
        '[paths]\nproject = "/private/project"\n'
        + HOSTS
        + '[spond]\nusername = "private-user"\ncollector_secret = "DO-NOT-COPY"\n'
    )
    destination = tmp_path / "public" / "hosts.toml"
    destination.parent.mkdir()
    monkeypatch.setattr(migration, "TRUSTED_CONFIG_PATH", source)
    monkeypatch.setattr(migration, "HOSTS_CONFIG_PATH", destination)
    monkeypatch.setattr(hosts_config, "HOSTS_CONFIG_PATH", destination)
    chown_calls = []
    monkeypatch.setattr(migration.os, "chown", lambda *args: chown_calls.append(args))
    real_stat = os.stat

    def root_owned_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if Path(path) == destination:
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(migration.os, "stat", root_owned_stat)
    return source, destination, chown_calls


def test_migration_extracts_only_hosts_with_modes_and_deterministic_content(paths):
    source, destination, chown_calls = paths
    migration.install_public_hosts()
    text = destination.read_text()
    assert text.startswith("[hosts]\n")
    assert "private-user" not in text
    assert "DO-NOT-COPY" not in text
    assert "/private/project" not in text
    assert "[spond]" not in text and "[paths]" not in text
    assert destination.stat().st_mode & 0o777 == 0o644
    assert chown_calls == [(destination, 0, 0)]
    first = text
    migration.install_public_hosts()
    assert destination.read_text() == first
    assert hosts_config.read_hosts() == migration.extract_hosts()


def test_source_symlink_is_rejected_without_output(paths, tmp_path: Path):
    source, destination, _ = paths
    target = tmp_path / "actual-trusted.toml"
    source.rename(target)
    source.symlink_to(target)
    with pytest.raises(OSError):
        migration.install_public_hosts()
    assert not destination.exists()


def test_destination_symlink_is_replaced_not_followed(paths, tmp_path: Path):
    _, destination, _ = paths
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    destination.symlink_to(outside)
    migration.install_public_hosts()
    assert outside.read_text() == "unchanged"
    assert not destination.is_symlink()
    assert hosts_config.read_hosts()["dsh_web"] == "https://server:8444"


def test_unapproved_host_key_is_rejected_without_copying_secrets(paths):
    source, destination, _ = paths
    source.write_text(HOSTS + 'password = "DO-NOT-COPY"\n')
    with pytest.raises(ValueError, match="unapproved"):
        migration.install_public_hosts()
    assert not destination.exists()


def test_missing_hosts_table_is_rejected(paths):
    source, destination, _ = paths
    source.write_text('[paths]\nproject = "/private/project"\n')
    with pytest.raises(ValueError, match=r"no \[hosts\]"):
        migration.install_public_hosts()
    assert not destination.exists()


def test_public_reader_rejects_extra_tables(paths):
    _, destination, _ = paths
    destination.write_text(HOSTS + '[spond]\nusername = "private-user"\n')
    with pytest.raises(ValueError, match=r"only \[hosts\]"):
        hosts_config.read_hosts()


def test_public_reader_rejects_malformed_toml(paths):
    _, destination, _ = paths
    destination.write_text('[hosts\nnot valid')
    with pytest.raises(hosts_config.tomllib.TOMLDecodeError):
        hosts_config.read_hosts()


def test_public_reader_missing_and_symlink_fail(paths, tmp_path: Path):
    _, destination, _ = paths
    with pytest.raises(FileNotFoundError):
        hosts_config.read_hosts()
    outside = tmp_path / "outside"
    outside.write_text(HOSTS)
    destination.symlink_to(outside)
    with pytest.raises(OSError):
        hosts_config.read_hosts()
