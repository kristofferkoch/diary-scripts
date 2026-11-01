"""Adversarial isolated tests for fixed-model event triage."""
from __future__ import annotations

import json
import stat
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts import pipeline_triage as triage
from scripts.event_spool import EventSpool, build_event


def make_event(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_event(
        "mail",
        "message",
        "native-transport-id",
        payload or {
            "message_id": "transport-message-id",
            "date": "2026-11-02T10:00:00Z",
            "from": "Sender <sender@example.test>",
            "to": "private-recipient@example.test",
            "subject": "Permission form",
            "body": "Answer by Friday.",
            "attachments": [{"filename": "private-name.pdf", "url": "https://private.invalid/a"}],
            "childId": "private-child-id",
            "messages": [{
                "sender": "Sender",
                "body": "Nested detail",
                "threadUid": "private-thread-id",
                "url": "https://private.invalid/thread",
            }],
        },
        revision="notmuch-v1",
    )


def write_event(spool_path: Path, event: dict[str, Any]) -> None:
    spool_path.mkdir(mode=0o750)
    assert EventSpool(spool_path).write(event) == "written"


def valid_result(event_id: str, **overrides: Any) -> dict[str, Any]:
    result = {
        "event_id": event_id,
        "urgency": "high",
        "summary": "Permission form needs an answer.",
        "deadline": "2026-11-06",
        "review_reference": event_id,
    }
    result.update(overrides)
    return result


def completion(result: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(result)}}]}


@contextmanager
def model_server(
    response: dict[str, Any], *, redirect_to: str | None = None
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(length)))
            if redirect_to is not None:
                self.send_response(307)
                self.send_header("Location", redirect_to + self.path)
                self.end_headers()
                return
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_valid_event_sends_minimum_content_and_commits_exact_result(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    state_dir = tmp_path / "state"
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        config = triage.PipelineConfig("local-test-model", endpoint)
        assert triage.run_once(spool, state_dir, config=config) == {
            "completed": 1, "review": 0, "retry": 0, "unchanged": 0,
        }
        assert triage.run_once(spool, state_dir, config=config)["unchanged"] == 1
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == "local-test-model"
    assert request["temperature"] == 0
    assert request["cache_prompt"] is False
    assert "tools" not in request
    assert request["response_format"]["json_schema"]["strict"] is True
    schema = request["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == triage.RESULT_FIELDS
    sent = json.loads(request["messages"][1]["content"])
    assert set(sent) == {"event_id", "source", "kind", "content"}
    assert set(sent["content"]) == {"date", "from", "subject", "body", "messages"}
    assert sent["content"]["messages"] == [{"sender": "Sender", "body": "Nested detail"}]
    encoded = json.dumps(sent)
    for excluded in (
        "native-transport-id", "transport-message-id", "private-recipient@example.test",
        "private-name.pdf", "private-child-id", "private-thread-id", "private.invalid",
    ):
        assert excluded not in encoded
    store = triage.TriageStore(state_dir)
    state = store.load(event["event_id"])
    assert state is not None
    assert state["status"] == "completed"
    assert state["attempts"] == 1
    assert state["result"] == valid_result(event["event_id"])
    assert list(store.completed_results()) == [state["result"]]
    assert stat.S_IMODE(store.path_for(event["event_id"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("summary"),
        lambda value: value.update(event_id="evt_" + "0" * 64),
        lambda value: value.update(review_reference="https://attacker.invalid/"),
        lambda value: value.update(urgency="urgent"),
        lambda value: value.update(urgency=[]),
        lambda value: value.update(summary=""),
        lambda value: value.update(summary=[]),
        lambda value: value.update(summary="x" * 4001),
        lambda value: value.update(deadline="2026-02-30"),
        lambda value: value.update(deadline="2026-11-06T12:00:00"),
        lambda value: value.update(deadline=6),
    ],
)
def test_result_schema_rejects_adversarial_values(mutation):
    event_id = "evt_" + "a" * 64
    result = valid_result(event_id)
    mutation(result)
    with pytest.raises(triage.ResultValidationError):
        triage.validate_result(result, event_id)


@pytest.mark.parametrize("mutation", ["extra", "wrong_id"])
def test_invalid_model_result_is_terminal_local_review_without_retry(tmp_path: Path, mutation: str):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    result = valid_result(event["event_id"])
    if mutation == "extra":
        result["notify"] = True
    else:
        result["event_id"] = "evt_" + "0" * 64
    with model_server(completion(result)) as (endpoint, requests):
        config = triage.PipelineConfig("local", endpoint)
        first = triage.run_once(spool, tmp_path / "state", config=config)
        second = triage.run_once(spool, tmp_path / "state", config=config)
    assert first["review"] == 1
    assert second["unchanged"] == 1
    assert len(requests) == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "review"
    assert state["result"] is None
    assert list(triage.TriageStore(tmp_path / "state").completed_results()) == []


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [{"message": {"content": "```json\n{}\n```"}}]},
        {"choices": [{"message": {"content": '{"event_id":"a","event_id":"b"}'}}]},
        {"choices": []},
        {"choices": [{"message": {"content": "{}", "tool_calls": []}}]},
        {"choices": [
            {"message": {"content": "{}"}}, {"message": {"content": "{}"}},
        ]},
    ],
)
def test_completion_envelope_and_strict_json_fail_closed(response: dict[str, Any]):
    with pytest.raises(triage.ResultValidationError):
        triage.parse_model_response(response, "evt_" + "a" * 64)


def test_redirect_is_not_followed_and_has_no_alternate_fallback(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    with model_server(completion(valid_result(event["event_id"]))) as (target, target_requests):
        with model_server({}, redirect_to=target) as (redirect, redirect_requests):
            counts = triage.run_once(
                spool, tmp_path / "state", config=triage.PipelineConfig("local", redirect),
            )
    assert counts["retry"] == 1
    assert len(redirect_requests) == 1
    assert target_requests == []
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "retry"
    assert list(triage.TriageStore(tmp_path / "state").completed_results()) == []


def test_transport_retry_then_success_preserves_attempt_history(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    monkeypatch.setattr(
        triage, "call_model",
        lambda *args: (_ for _ in ()).throw(triage.ModelTransportError("fixed endpoint unavailable")),
    )
    config = triage.PipelineConfig("local", "http://127.0.0.1:9")
    assert triage.run_once(spool, tmp_path / "state", config=config)["retry"] == 1
    monkeypatch.undo()
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        counts = triage.run_once(
            spool, tmp_path / "state", config=triage.PipelineConfig("local", endpoint),
        )
    assert counts["completed"] == 1
    assert len(requests) == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "completed" and state["attempts"] == 2


def test_crash_during_model_call_recovers_processing_to_retry_then_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)

    class SimulatedCrash(Exception):
        pass

    def crash(*args: object) -> dict[str, Any]:
        raise SimulatedCrash

    monkeypatch.setattr(triage, "call_model", crash)
    config = triage.PipelineConfig("local", "http://127.0.0.1:9")
    with pytest.raises(SimulatedCrash):
        triage.run_once(spool, tmp_path / "state", config=config)
    interrupted = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert interrupted is not None and interrupted["status"] == "processing"

    monkeypatch.undo()
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        counts = triage.run_once(
            spool, tmp_path / "state", config=triage.PipelineConfig("local", endpoint),
        )
    assert counts["completed"] == 1
    assert len(requests) == 1
    recovered = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert recovered is not None and recovered["status"] == "completed"
    assert recovered["attempts"] == 2


def test_empty_allowlisted_content_stays_in_review_without_model_call(tmp_path: Path, monkeypatch):
    event = make_event({"message_id": "native", "childId": "private", "url": "https://private.invalid"})
    spool = tmp_path / "events"
    write_event(spool, event)

    def unexpected(*args: object) -> dict[str, Any]:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(triage, "call_model", unexpected)
    counts = triage.run_once(
        spool, tmp_path / "state", config=triage.PipelineConfig("local", "http://127.0.0.1:9"),
    )
    assert counts["review"] == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "review" and state["attempts"] == 0


def test_corrupt_state_fails_closed_without_calling_model(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    triage.TriageStore(state_dir).path_for(event["event_id"]).write_text("{}")

    def unexpected(*args: object) -> dict[str, Any]:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(triage, "call_model", unexpected)
    with pytest.raises(triage.StateError):
        triage.run_once(
            spool, state_dir, config=triage.PipelineConfig("local", "http://127.0.0.1:9"),
        )


def test_state_validation_rejects_unknown_status_type():
    event_id = "evt_" + "a" * 64
    state = triage._new_state(event_id, "b" * 64)
    state["status"] = []
    with pytest.raises(triage.StateError):
        triage.validate_state(state, event_id)


def test_config_comes_only_from_trusted_values(monkeypatch: pytest.MonkeyPatch):
    values = {"pipeline": {"model": "trusted-local", "endpoint": "http://127.0.0.1:8080"}}
    monkeypatch.setattr(triage, "trusted_cfg", lambda key, default: values.get(key, default))
    monkeypatch.setenv("PIPELINE_ENDPOINT", "https://attacker.invalid")
    monkeypatch.setenv("DIARY_CONFIG", "/attacker/config.toml")
    config = triage.load_config()
    assert config == triage.PipelineConfig("trusted-local", "http://127.0.0.1:8080")


def test_config_rejects_alternate_endpoint_keys(monkeypatch: pytest.MonkeyPatch):
    values = {"pipeline": {
        "model": "trusted-local",
        "endpoint": "http://127.0.0.1:8080",
        "alternate_endpoint": "http://127.0.0.1:8081",
    }}
    monkeypatch.setattr(triage, "trusted_cfg", lambda key, default: values.get(key, default))
    with pytest.raises(triage.ConfigError):
        triage.load_config()


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://model.local:8080",
        "https://127.0.0.1:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/other",
        "http://user@127.0.0.1:8080",
        "http://" + ".".join(["8"] * 4) + ":53",
        "http://0.0.0.0:8080",
        "http://" + ".".join(["224", "0", "0", "1"]) + ":8080",
        "http://[fe80::1%eth0]:8080",
    ],
)
def test_config_rejects_selectable_nonlocal_or_ambiguous_endpoints(endpoint: Any):
    with pytest.raises(triage.ConfigError):
        triage._validate_endpoint(endpoint)
