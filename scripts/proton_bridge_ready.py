"""Wait for Proton Bridge accounts to be connected without using IMAP logins."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import grpc

CONNECTED = 2
_METHOD = "/grpc.Bridge/GetUserList"


@dataclass(frozen=True)
class BridgeConfig:
    target: str
    certificate: bytes
    token: str


def default_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "protonmail" / "bridge-v3" / "grpcServerConfig.json"


def load_config(path: Path) -> BridgeConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Bridge gRPC config is not an object")
    certificate = raw.get("cert")
    token = raw.get("token")
    if not isinstance(certificate, str) or not certificate:
        raise ValueError("Bridge gRPC config has no certificate")
    if not isinstance(token, str) or not token:
        raise ValueError("Bridge gRPC config has no token")
    socket_path = raw.get("fileSocketPath")
    if socket_path:
        if not isinstance(socket_path, str) or not Path(socket_path).is_absolute():
            raise ValueError("Bridge gRPC socket path is not absolute")
        target = f"unix://{socket_path}"
    else:
        port = raw.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Bridge gRPC config has no valid port")
        target = f"127.0.0.1:{port}"
    return BridgeConfig(target, certificate.encode("ascii"), token)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    raise ValueError("protobuf varint is too long")


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field, wire_type = key >> 3, key & 7
        if field == 0:
            raise ValueError("invalid protobuf field zero")
        if wire_type == 0:
            value: int | bytes
            value, offset = _read_varint(data, offset)
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            offset = end
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated protobuf fixed64 field")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated protobuf fixed32 field")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        yield field, wire_type, value


def _parse_user_state(data: bytes) -> int:
    state = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 4:
            if wire_type != 0 or not isinstance(value, int):
                raise ValueError("invalid Bridge account state")
            state = value
    return state


def parse_user_states(data: bytes) -> list[int]:
    states = []
    for field, wire_type, value in _iter_fields(data):
        if field == 1:
            if wire_type != 2 or not isinstance(value, bytes):
                raise ValueError("invalid Bridge account entry")
            states.append(_parse_user_state(value))
    return states


def query_user_states(config: BridgeConfig, timeout: float = 2.0) -> list[int]:
    credentials = grpc.ssl_channel_credentials(config.certificate)
    options = (("grpc.ssl_target_name_override", "127.0.0.1"),)
    with grpc.secure_channel(config.target, credentials, options) as channel:
        call = channel.unary_unary(
            _METHOD,
            request_serializer=lambda request: b"",
            response_deserializer=parse_user_states,
        )
        return call(
            None,
            timeout=timeout,
            metadata=(("server-token", config.token),),
        )


def wait_ready(
    config_path: Path,
    timeout: float = 180.0,
    poll_interval: float = 2.0,
    query: Callable[[BridgeConfig, float], list[int]] = query_user_states,
    loader: Callable[[Path], BridgeConfig] = load_config,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = monotonic()
    states: list[int] | None = None
    while True:
        remaining = timeout - (monotonic() - started)
        try:
            states = query(loader(config_path), max(0.05, min(2.0, remaining)))
        except OSError, TypeError, ValueError, UnicodeError, grpc.RpcError:
            states = None
        if states and all(state == CONNECTED for state in states):
            return
        if remaining <= 0:
            if states is None:
                detail = "Bridge status unavailable"
            elif not states:
                detail = "no accounts configured"
            else:
                names = {
                    0: "signed out",
                    1: "locked",
                    2: "connected",
                }
                detail = ", ".join(
                    names.get(state, f"unknown state {state}") for state in states
                )
            raise TimeoutError(f"Bridge not ready after {timeout:g}s: {detail}")
        sleep(min(poll_interval, max(0.0, timeout - (monotonic() - started))))


def valid_duration(value: float) -> bool:
    return math.isfinite(value) and value > 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    result.add_argument("--config", type=Path, default=default_config_path())
    result.add_argument("--timeout", type=float, default=180.0)
    result.add_argument("--poll-interval", type=float, default=2.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not valid_duration(args.timeout) or not valid_duration(args.poll_interval):
        parser().error("timeout and poll interval must be positive")
    try:
        wait_ready(args.config, args.timeout, args.poll_interval)
        return 0
    except TimeoutError as exc:
        print(f"proton-bridge-await-ready: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
