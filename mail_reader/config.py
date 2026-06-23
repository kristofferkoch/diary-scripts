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
    """Drop the cached parse + workspace root (used by tests after changing env)."""
    _data.cache_clear()
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
        1. config ``paths.project``
        2. ``$DIARY_ROOT``
        3. nearest ancestor of the CWD that contains ``CALENDAR.md`` (dev convenience)
        4. the CWD
    """
    val = cfg("paths.project", None)
    if val:
        return Path(val).expanduser()
    env = os.environ.get("DIARY_ROOT")
    if env:
        return Path(env).expanduser()
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


def _host_url(env_var: str, dotted: str, default_hostport: str) -> str:
    """Build ``http://host:port`` from an env override or a ``hosts.*`` config key.

    The host MUST be sourced from config, not just a hardcoded default. When
    this code moved to the public ``diary-scripts`` submodule the real hosts
    were wiped to ``gpu-host`` placeholders; consumers read only their env var
    (``$OLLAMA_URL`` etc.), so once that var stopped being exported the dead
    placeholder leaked through, DNS failed, and the 15-min embed pipeline went
    down silently for hours (2026-06-02). Reading the ``hosts.*`` config key
    here closes that gap; the env var still wins for ad-hoc overrides.
    """
    return (os.environ.get(env_var)
            or f"http://{cfg(dotted, default_hostport)}").rstrip("/")


def summaries_enabled() -> bool:
    """Whether the per-mail LLM summary passes run.

    Default on, so the public submodule keeps its original behaviour. Set
    ``summaries.enabled = false`` in the private config (or export
    ``SUMMARY_ENABLED=0``) to stop both *enqueueing* new summary work and
    *spawning* the worker tasks — in particular the GPU-heavy tier-2 pass
    (a 35B model that pins VRAM on the LLM host). Already-stored ``done``
    summaries still render in the UI; only new generation is suppressed.
    The env var wins over config for ad-hoc overrides.
    """
    env = os.environ.get("SUMMARY_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(cfg("summaries.enabled", True))


def summaries_max_tier() -> int:
    """Highest summary *tier* that is allowed to run. Passes above this are
    neither enqueued nor given a worker.

    Lets you keep the cheap draft tier (qwen2.5:3b, tier 1) while suppressing
    the GPU-heavy tier-2 final pass (a 35B model that pins VRAM on the LLM
    host) without disabling summaries entirely. Default is a large sentinel
    so every configured pass runs unless capped. Set ``summaries.max_tier``
    in the private config (or export ``SUMMARY_MAX_TIER``). Only meaningful
    when ``summaries_enabled()`` is true.
    """
    env = os.environ.get("SUMMARY_MAX_TIER")
    if env is not None:
        return int(env)
    return int(cfg("summaries.max_tier", 99))


def ollama_url() -> str:
    """Ollama embedding/chat server, e.g. ``http://gpu-host:11434``."""
    return _host_url("OLLAMA_URL", "hosts.llm", "gpu-host:11434")


def mlx_url() -> str:
    """pr_compose MLX server (classifier/writer), e.g. ``http://gpu-host:8080``."""
    return _host_url("MLX_BASE", "hosts.mlx", "gpu-host:8080")


def nuextract_url() -> str:
    """pr_compose NuExtract extractor server, e.g. ``http://gpu-host:8081``."""
    return _host_url("NUEXTRACT_BASE", "hosts.nuextract", "gpu-host:8081")
