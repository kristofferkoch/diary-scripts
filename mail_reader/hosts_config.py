"""Strict parser for the root-owned, non-secret public hosts TOML."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HOSTS_CONFIG_PATH = Path("/etc/diary/hosts.toml")
ALLOWED_HOST_KEYS = frozenset({
    "embed", "vlm", "server", "tailnet_base", "dsh_web", "kimi_web", "immich",
})
REQUIRED_HOST_KEYS = frozenset({
    "embed", "vlm", "tailnet_base", "dsh_web", "kimi_web", "immich",
})
HOST_PORT_KEYS = frozenset({"embed", "vlm", "immich"})
HTTPS_URL_KEYS = frozenset({"tailnet_base", "dsh_web", "kimi_web"})
HOSTNAME_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _valid_hostname(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253:
        return False
    return all(HOSTNAME_LABEL.fullmatch(label) for label in value.split("."))


def _valid_port(value: int | None, *, required: bool) -> bool:
    return (not required and value is None) or (value is not None and 1 <= value <= 65535)


def _valid_host_port(value: str) -> bool:
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except ValueError:
        return False
    return (
        not parsed.path and not parsed.query and not parsed.fragment
        and parsed.username is None and parsed.password is None
        and parsed.hostname is not None and _valid_hostname(parsed.hostname)
        and _valid_port(port, required=True)
    )


def _valid_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https" and parsed.path in ("", "/")
        and not parsed.query and not parsed.fragment
        and parsed.username is None and parsed.password is None
        and parsed.hostname is not None and _valid_hostname(parsed.hostname)
        and _valid_port(port, required=False)
    )


def _valid_plain_host(value: str) -> bool:
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except ValueError:
        return False
    return (
        not parsed.path and not parsed.query and not parsed.fragment
        and parsed.username is None and parsed.password is None
        and parsed.hostname is not None and _valid_hostname(parsed.hostname)
        and port is None
    )


def _valid_endpoint(key: str, value: str) -> bool:
    if key in HOST_PORT_KEYS:
        return _valid_host_port(value)
    if key in HTTPS_URL_KEYS:
        return _valid_https_url(value)
    return _valid_plain_host(value)


def validate_hosts(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("hosts must be a TOML table")
    keys = set(value)
    if not REQUIRED_HOST_KEYS <= keys or not keys <= ALLOWED_HOST_KEYS:
        raise ValueError("hosts table has missing or unapproved keys")
    hosts: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 2048 \
                or any(ord(char) < 32 or ord(char) == 127 for char in item) \
                or not _valid_endpoint(key, item):
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
