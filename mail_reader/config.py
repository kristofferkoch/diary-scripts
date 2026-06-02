"""Runtime configuration overlay.

This public repo ships *placeholder* defaults inline in the code. Real
deployment values (hostnames, paths, home coordinates, family names, the
real important-senders list, …) live in a private TOML file kept outside
this repo — in the diary workspace, symlinked to the XDG path — so the code
stays publishable while still running for real.

Every consumer reads a value with a hardcoded fallback::

    from mail_reader.config import cfg
    WEATHER_LAT = cfg("weather.lat", 59.913)

so nothing breaks when no config file is present (the public default wins).

File lookup order:
    1. ``$DIARY_CONFIG``                         (explicit; must exist if set)
    2. ``$XDG_CONFIG_HOME/diary/config.toml``    (default ``~/.config``)

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
    """Drop the cached parse (used by tests after changing the env)."""
    _data.cache_clear()


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
