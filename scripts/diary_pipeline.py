#!/usr/bin/python3
"""Fixed health probe for the permanently contained diary-pipeline identity."""
from __future__ import annotations

import argparse
import dataclasses
import http.client
import ipaddress
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any

PIPELINE_CONFIG_PATH = Path("/etc/diary/pipeline.toml")
ROOT_UID = 0
MAX_CONFIG_BYTES = 4096
MAX_HEALTH_BYTES = 4096
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class PipelineError(RuntimeError):
    """The fixed pipeline path cannot be verified without weakening containment."""


@dataclasses.dataclass(frozen=True)
class ModelEndpoint:
    address: str
    port: int
    model: str


def _parse_document(document: Any) -> ModelEndpoint:
    if not isinstance(document, dict) or set(document) != {"pipeline"}:
        raise PipelineError("pipeline config must contain only [pipeline]")
    settings = document["pipeline"]
    if not isinstance(settings, dict) or set(settings) != {"model", "model_ip", "model_port"}:
        raise PipelineError("pipeline config requires only model, model_ip and model_port")
    model = settings["model"]
    if not isinstance(model, str) or not MODEL_NAME_RE.fullmatch(model):
        raise PipelineError("model must be a bounded ASCII identifier")
    value = settings["model_ip"]
    if not isinstance(value, str):
        raise PipelineError("model_ip must be a numeric IPv4 string")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise PipelineError("model_ip must be a numeric IPv4 string") from exc
    if address.is_unspecified or address.is_multicast:
        raise PipelineError("model_ip must identify one model host")
    port = settings["model_port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise PipelineError("model_port must be a TCP port number")
    return ModelEndpoint(str(address), port, model)


def read_pipeline_config(path: Path = PIPELINE_CONFIG_PATH) -> ModelEndpoint:
    """Read the one fixed, root-owned endpoint without DNS or environment overrides."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PipelineError("pipeline config must be a single-link regular file")
        if info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise PipelineError("pipeline config must be root-owned and not group/other writable")
        if info.st_size > MAX_CONFIG_BYTES:
            raise PipelineError("pipeline config is too large")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PipelineError("pipeline config is malformed TOML") from exc
    finally:
        os.close(fd)
    return _parse_document(document)


def probe_model(endpoint: ModelEndpoint, timeout: float = 10.0) -> None:
    """GET the fixed model's health path, with no proxy, redirect, or fallback."""
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=timeout)
    try:
        connection.request("GET", "/health", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise PipelineError(f"fixed model health returned HTTP {response.status}")
        if len(response.read(MAX_HEALTH_BYTES + 1)) > MAX_HEALTH_BYTES:
            raise PipelineError("fixed model health response is too large")
    except PipelineError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise PipelineError(f"fixed model health failed: {exc}") from exc
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        endpoint = read_pipeline_config()
        probe_model(endpoint)
    except (PipelineError, OSError) as exc:
        print(f"diary-pipeline: {exc}", file=sys.stderr)
        return 1
    print(f"ok: fixed model health at {endpoint.address}:{endpoint.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
