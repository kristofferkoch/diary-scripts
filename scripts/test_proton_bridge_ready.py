"""Tests for the Proton Bridge account-readiness gate."""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
from typing import cast

import grpc
import pytest

from scripts import proton_bridge_ready as ready


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number: int, wire_type: int, value: int | bytes) -> bytes:
    encoded = _varint((number << 3) | wire_type)
    if wire_type == 0:
        assert isinstance(value, int)
        return encoded + _varint(value)
    assert wire_type == 2 and isinstance(value, bytes)
    return encoded + _varint(len(value)) + value


def _user(state: int, password: bytes = b"not-read") -> bytes:
    return b"".join(
        (
            _field(1, 2, b"user-id"),
            _field(2, 2, b"user-name"),
            _field(4, 0, state),
            _field(8, 2, password),
        )
    )


def _user_list(*states: int) -> bytes:
    return b"".join(_field(1, 2, _user(state)) for state in states)


def _config(path: Path) -> ready.BridgeConfig:
    return ready.BridgeConfig("unused", b"unused", "unused")


def test_parse_user_states_ignores_credentials_and_unknown_fields() -> None:
    payload = _user_list(1, 2) + _field(99, 2, b"unknown")
    assert ready.parse_user_states(payload) == [1, 2]


def test_parse_user_states_rejects_truncated_input() -> None:
    with pytest.raises(ValueError, match="truncated"):
        ready.parse_user_states(b"\x0a\x05abc")


def test_load_config_uses_file_socket(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "fileSocketPath": "/run/user/1000/bridge-test",
                "cert": "certificate",
                "token": "token",
            }
        )
    )
    config = ready.load_config(path)
    assert config.target == "unix:///run/user/1000/bridge-test"
    assert config.certificate == b"certificate"
    assert config.token == "token"


def test_load_config_rejects_empty_account_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 1042, "cert": "certificate"}))
    with pytest.raises(ValueError, match="token"):
        ready.load_config(path)


def test_wait_ready_returns_when_every_account_is_connected() -> None:
    calls = 0

    def query(config: ready.BridgeConfig, timeout: float) -> list[int]:
        nonlocal calls
        calls += 1
        return [2, 2]

    ready.wait_ready(Path("unused"), timeout=1, query=query, loader=_config)
    assert calls == 1


def test_wait_ready_survives_missing_config_and_locked_account() -> None:
    results: list[object] = [FileNotFoundError("missing"), [1], [2]]
    sleeps: list[float] = []

    def query(config: ready.BridgeConfig, timeout: float) -> list[int]:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        return cast(list[int], result)

    ready.wait_ready(
        Path("unused"),
        timeout=10,
        poll_interval=0.25,
        query=query,
        loader=_config,
        sleep=sleeps.append,
    )
    assert sleeps == [0.25, 0.25]
    assert not results


def test_wait_ready_times_out_without_accounts() -> None:
    def query(config: ready.BridgeConfig, timeout: float) -> list[int]:
        return []

    with pytest.raises(TimeoutError, match="no accounts"):
        ready.wait_ready(Path("unused"), timeout=0, query=query, loader=_config)


def test_query_user_states_makes_real_grpc_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_token = []

    def get_user_list(request: object, context: grpc.ServicerContext) -> bytes:
        seen_token.extend(
            value
            for key, value in context.invocation_metadata()
            if key == "server-token"
        )
        return _user_list(2)

    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=1))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "grpc.Bridge",
                {
                    "GetUserList": grpc.unary_unary_rpc_method_handler(
                        get_user_list,
                        request_deserializer=lambda data: data,
                        response_serializer=lambda response: response,
                    )
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    monkeypatch.setattr(
        ready.grpc,
        "secure_channel",
        lambda target, credentials, options: grpc.insecure_channel(target),
    )
    try:
        config = ready.BridgeConfig(f"127.0.0.1:{port}", b"unused", "test-token")
        assert ready.query_user_states(config) == [2]
        assert seen_token == ["test-token"]
    finally:
        server.stop(0).wait()


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_invalid_duration_is_rejected(value: float) -> None:
    assert not ready.valid_duration(value)


def test_positive_finite_duration_is_accepted() -> None:
    assert ready.valid_duration(0.1)
