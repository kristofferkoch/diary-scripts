"""Shared allowlist checks for accidental identities in scripts queue changes."""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import stat
import tomllib
import urllib.parse
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/diary/review.toml")
DEFAULT_FAKE_IDENTITIES = (
    "server",
    "gpu-host",
    "localhost",
    "example.com",
    "example.net",
    "example.org",
    "example.invalid",
    "example.test",
    "github.com",
)
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,63})\b")
URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"')\]}]+")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:host|hostname|server|endpoint|url|address|"
    r"full_name|family_name|first_name|last_name|phone|mobile)\b"
    r"\s*[:=]\s*[\"'])(?P<value>[^\"'\r\n]{1,256})(?P<quote>[\"'])"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
MISLEADING_UNICODE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class PiiError(RuntimeError):
    """The allowlist configuration or input cannot be accepted."""


@dataclasses.dataclass(frozen=True)
class ReviewConfig:
    model: str
    allowed_identities: tuple[str, ...]
    max_diff_chars: int


@dataclasses.dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str


def read_config(path: Path = DEFAULT_CONFIG) -> ReviewConfig:
    """Read the root-provisioned reviewer settings; no environment override."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PiiError("review config must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            document = tomllib.load(handle)
    finally:
        os.close(fd)
    if set(document) != {"review"} or not isinstance(document["review"], dict):
        raise PiiError("review config must contain only [review]")
    values = document["review"]
    allowed_keys = {"model", "allowed_identities", "max_diff_chars"}
    if set(values) - allowed_keys:
        raise PiiError("review config contains unknown keys")
    model = values.get("model")
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise PiiError("review.model must be a bounded model identifier")
    configured = values.get("allowed_identities", [])
    if not isinstance(configured, list) or any(
        not isinstance(item, str) or not item or len(item) > 253
        or CONTROL_RE.search(item) or MISLEADING_UNICODE_RE.search(item)
        for item in configured
    ):
        raise PiiError("review.allowed_identities must contain bounded plain text")
    max_diff = values.get("max_diff_chars", 500_000)
    if isinstance(max_diff, bool) or not isinstance(max_diff, int) or not 1_000 <= max_diff <= 500_000:
        raise PiiError("review.max_diff_chars must be between 1000 and 500000")
    identities = tuple(dict.fromkeys((*DEFAULT_FAKE_IDENTITIES, *configured)))
    return ReviewConfig(model, identities, max_diff)


def _allowed_hostname(hostname: str, allowed: tuple[str, ...]) -> bool:
    hostname = hostname.rstrip(".").lower()
    for item in allowed:
        item = item.lower()
        if item.startswith(".") and hostname.endswith(item):
            return True
        if hostname == item:
            return True
    return False


def _allowed_email(value: str, allowed: tuple[str, ...]) -> bool:
    return _allowed_hostname(value.rsplit("@", 1)[1], allowed)


def _allowed_url(value: str, allowed: tuple[str, ...]) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.hostname is not None and _allowed_hostname(parsed.hostname, allowed)
    except ValueError:
        return False


def _allowed_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_loopback or address.is_unspecified
        or address in ipaddress.ip_network("192.0.2.0/24")
        or address in ipaddress.ip_network("198.51.100.0/24")
        or address in ipaddress.ip_network("203.0.113.0/24")
    )


def scan_identities(
    text: str, path: str, allowed: tuple[str, ...], findings: list[Finding], *, added_only: bool
) -> None:
    """Record simple unapproved identifiers without copying or changing content."""
    for line in text.splitlines():
        if added_only and not (line.startswith("+") and not line.startswith("+++")):
            continue

        def add(kind: str) -> None:
            finding = Finding(kind, path, f"unapproved {kind}-like value")
            if finding not in findings:
                findings.append(finding)

        for match in ASSIGNMENT_RE.finditer(line):
            candidate = match.group("value").strip()
            fake = _allowed_email(candidate, allowed) if "@" in candidate else _allowed_hostname(candidate, allowed)
            if not (fake or _allowed_url(candidate, allowed) or _allowed_ipv4(candidate)):
                add("identity")
        for match in URL_RE.finditer(line):
            if not _allowed_url(match.group(0), allowed):
                add("url")
        for match in EMAIL_RE.finditer(line):
            if not _allowed_email(match.group(0), allowed):
                add("email")
        for match in IPV4_RE.finditer(line):
            if not _allowed_ipv4(match.group(0)):
                add("network-identity")


def prescan(
    diff: str, messages: str, paths: tuple[str, ...], allowed: tuple[str, ...]
) -> list[Finding]:
    findings: list[Finding] = []
    scan_identities(diff, "<diff>", allowed, findings, added_only=True)
    scan_identities(messages, "<commit-message>", allowed, findings, added_only=False)
    for path in paths:
        scan_identities(path, path, allowed, findings, added_only=False)
    return findings
