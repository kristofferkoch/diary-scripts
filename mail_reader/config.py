"""Runtime configuration overlays.

This public repo ships *placeholder* defaults inline in the code. Ordinary
private values (paths, home coordinates, family names, the real
important-senders list, …) live in a TOML file kept outside this repo — in the
diary workspace, symlinked to the XDG path — so the code stays publishable
while still running for real.

Every ordinary consumer reads a value with a hardcoded fallback::

    from mail_reader.config import cfg
    WEATHER_LAT = cfg("weather.lat", 59.913)

so nothing breaks when no config file is present (the public default wins).

Ordinary file lookup order:
    1. ``$DIARY_CONFIG``                         (explicit; must exist if set)
    2. ``$XDG_CONFIG_HOME/diary/config.toml``    (default ``~/.config``)

Non-secret host endpoints come only from the fixed, root-owned world-readable
``/etc/diary/hosts.toml``. Privileged collector settings instead come only from
the fixed ``root:kek`` ``0640`` ``/etc/diary/trusted.toml`` through
``trusted_cfg``. Neither file has an environment override or ordinary-config
fallback; a missing or malformed public hosts file fails loudly.

A malformed TOML file raises (loud failure); a *missing* file in the XDG
slot is simply "no overrides". An explicitly-set ``$DIARY_CONFIG`` that
doesn't exist is a misconfiguration and raises.
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any


def _config_path() -> Path | None:
    env = os.environ.get("DIARY_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"DIARY_CONFIG set but file not found: {p}")
        return p
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    p = xdg / "diary" / "config.toml"
    return p if p.exists() else None


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    p = _config_path()
    if p is None:
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def reload() -> None:
    """Drop cached config and workspace root (used by tests after changing env)."""
    _data.cache_clear()
    _trusted_data.cache_clear()
    _hosts_data.cache_clear()
    workspace_root.cache_clear()


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    """The diary DATA root — where ``CALENDAR.md``, ``memory/`` and the topic
    files live. **Single source of truth.**

    Modules must call this instead of deriving the root by walking their own
    ``__file__`` up a couple of levels: that silently returns the wrong
    directory the moment the code is relocated — e.g. mounted as a git
    submodule one level deeper — because it depends on *where the module
    sits*, not on where the data is.

    Resolution order:
        1. ``$DIARY_ROOT``
        2. config ``paths.project``
        3. nearest ancestor of the CWD that contains ``CALENDAR.md`` (dev convenience)
        4. the CWD

    The environment wins so a fixed-purpose service can pin its data root even
    when its ordinary runtime configuration is writable by a less-trusted user.
    """
    env = os.environ.get("DIARY_ROOT")
    if env:
        return Path(env).expanduser()
    val = cfg("paths.project", None)
    if val:
        return Path(val).expanduser()
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        if (base / "CALENDAR.md").exists():
            return base
    return cwd


def cfg(dotted: str, default: Any) -> Any:
    """Return the config value at a dotted key, or ``default`` if absent.

    Pure dict traversal over the parsed TOML — only the file read is cached::

        cfg("weather.lat", 59.913)   # the config value if set, else 59.913
        cfg("a.b.c", "fallback")     # "fallback" when the key is absent

    (Behaviour is pinned by ``test_config.py``, which controls the env.)
    """
    node: Any = _data()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def cfg_path(dotted: str, default: Path) -> Path:
    """Like :func:`cfg` but for a filesystem path. A relative value is
    resolved against the config file's directory, so the private config can
    point at sibling files (e.g. ``important_senders = "important_senders.txt"``)."""
    val = cfg(dotted, None)
    if val is None:
        return default
    p = Path(val).expanduser()
    if not p.is_absolute():
        base = _config_path()
        if base is not None:
            # resolve() so a relative value lands next to the *real* config
            # file even when reached through a symlink (e.g. the XDG slot
            # symlinked into the diary workspace).
            p = base.resolve().parent / p
    return p


TRUSTED_CONFIG_PATH = Path("/etc/diary/trusted.toml")


@lru_cache(maxsize=1)
def _trusted_data() -> dict[str, Any]:
    """Load security-sensitive settings from the root-owned system config.

    There is deliberately no environment override or fallback to the ordinary
    diary config: those are agent-publishable in the attended-agent threat model.
    """
    if not TRUSTED_CONFIG_PATH.exists():
        return {}
    with TRUSTED_CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def trusted_cfg(dotted: str, default: Any) -> Any:
    """Return a value from the fixed trusted config, or ``default`` if absent."""
    node: Any = _trusted_data()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


HOSTS_CONFIG_PATH = Path("/etc/diary/hosts.toml")


@lru_cache(maxsize=1)
def _hosts_data() -> dict[str, str]:
    from mail_reader.hosts_config import read_hosts
    return read_hosts(HOSTS_CONFIG_PATH)


def _host_value(key: str) -> str:
    return _hosts_data()[key]


def _host_url(key: str) -> str:
    """Build ``http://host:port`` from the fixed public hosts config."""
    return f"http://{_host_value(key)}".rstrip("/")


def embed_url() -> str:
    """OpenAI-compatible embedding server (llama.cpp ``/v1/embeddings``)."""
    return _host_url("embed")


def vlm_url() -> str:
    """Vision-language model server (OpenAI-compatible chat completions)."""
    return _host_url("vlm")


def immich_url() -> str:
    """Immich photo server API base."""
    return _host_url("immich")


def kimi_web_url() -> str:
    """Public base URL of the kimi web UI, without a trailing slash."""
    return _host_value("kimi_web").rstrip("/")


def dsh_web_url() -> str:
    """Public base URL of the dsh web UI, without a trailing slash."""
    return _host_value("dsh_web").rstrip("/")


def mail_web_url() -> str:
    """Public base URL of the authenticated mail UI, without a trailing slash."""
    return (_host_value("tailnet_base") + "/mail").rstrip("/")
