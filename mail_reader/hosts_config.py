"""Strict parser for the root-owned, non-secret public hosts TOML."""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

HOSTS_CONFIG_PATH = Path("/etc/diary/hosts.toml")
ALLOWED_HOST_KEYS = frozenset({
    "embed", "vlm", "server", "tailnet_base", "dsh_web", "kimi_web", "immich",
})
REQUIRED_HOST_KEYS = frozenset({
    "embed", "vlm", "tailnet_base", "dsh_web", "kimi_web", "immich",
})


def validate_hosts(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("hosts must be a TOML table")
    keys = set(value)
    if not REQUIRED_HOST_KEYS <= keys or not keys <= ALLOWED_HOST_KEYS:
        raise ValueError("hosts table has missing or unapproved keys")
    hosts: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 2048 \
                or any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise ValueError(f"invalid hosts.{key} value")
        hosts[key] = item
    return hosts


def read_hosts(path: Path | None = None) -> dict[str, str]:
    path = path or HOSTS_CONFIG_PATH
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("hosts config must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            document = tomllib.load(handle)
    finally:
        os.close(fd)
    if set(document) != {"hosts"}:
        raise ValueError("public hosts config must contain only [hosts]")
    return validate_hosts(document["hosts"])


def render_hosts(hosts: dict[str, str]) -> str:
    hosts = validate_hosts(hosts)
    lines = ["[hosts]"]
    for key in sorted(hosts):
        lines.append(f"{key} = {json.dumps(hosts[key], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"
