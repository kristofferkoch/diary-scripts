"""Adversarial isolated tests for fixed-model event triage."""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts import pipeline_triage as triage
from scripts.event_spool import EventSpool, build_event

START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


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
            "attachments": [{"filename": "private-name.pdf", "url": "https://example.invalid/a"}],
            "childId": "private-child-id",
            "messages": [{
                "sender": "Sender",
                "body": "Nested detail",
                "threadUid": "private-thread-id",
                "url": "https://example.invalid/thread",
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


def model_config(endpoint: str, model: str = "local") -> triage.PipelineConfig:
    parsed = urllib.parse.urlsplit(endpoint)
    assert parsed.hostname is not None and parsed.port is not None
    return triage.PipelineConfig(model, parsed.hostname, parsed.port)


@contextmanager
def model_server(
    response: dict[str, Any], *, redirect_to: str | None = None, status: int = 200
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
            self.send_response(status)
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
        base_url = "http://" + str(server.server_address[0])
        yield f"{base_url}:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(triage, "ROOT_UID", os.geteuid())
    path = tmp_path / "pipeline.toml"
    path.write_text(
        '[pipeline]\nmodel = "trusted-local"\nmodel_ip = "127.0.0.1"\nmodel_port = 8080\n'
    )
    path.chmod(0o640)
    return path


def test_valid_event_sends_minimum_content_and_commits_exact_result(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    state_dir = tmp_path / "state"
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        config = model_config(endpoint, "local-test-model")
        assert triage.run_once(spool, state_dir, config=config) == {
            "completed": 1, "review": 0, "retry": 0, "deferred": 0, "unchanged": 0,
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
        "private-name.pdf", "private-child-id", "private-thread-id", "example.invalid",
    ):
        assert excluded not in encoded
    store = triage.TriageStore(state_dir)
    state = store.load(event["event_id"])
    assert state is not None
    assert state["status"] == "completed"
    assert state["attempts"] == 1
    assert state["next_eligible_at"] is None
    assert state["result"] == valid_result(event["event_id"])
    assert list(store.completed_results()) == [state["result"]]
    assert stat.S_IMODE(store.path_for(event["event_id"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750


def test_pipeline02_provisioned_0750_state_directory_is_accepted(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o750)
    with triage.TriageStore(state_dir).lock():
        pass
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750
    state_dir.chmod(0o700)
    with pytest.raises(triage.StateError):
        with triage.TriageStore(state_dir).lock():
            pass


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("summary"),
        lambda value: value.update(event_id="evt_" + "0" * 64),
        lambda value: value.update(review_reference="https://example.invalid/"),
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
        config = model_config(endpoint)
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
        {"choices": [{"message": {"content": "{}", "function_call": {"name": "notify"}}}]},
        {"choices": [
            {"message": {"content": "{}"}}, {"message": {"content": "{}"}},
        ]},
    ],
)
def test_completion_envelope_and_strict_json_fail_closed(response: dict[str, Any]):
    with pytest.raises(triage.ResultValidationError):
        triage.parse_model_response(response, "evt_" + "a" * 64)


def test_redirect_is_terminal_review_and_target_receives_no_request(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    with model_server(completion(valid_result(event["event_id"]))) as (target, target_requests):
        with model_server({}, redirect_to=target) as (redirect, redirect_requests):
            first = triage.run_once(spool, tmp_path / "state", config=model_config(redirect))
            second = triage.run_once(spool, tmp_path / "state", config=model_config(redirect))
    assert first["review"] == 1
    assert second["unchanged"] == 1
    assert len(redirect_requests) == 1
    assert target_requests == []
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "review"
    assert state["next_eligible_at"] is None
    assert list(triage.TriageStore(tmp_path / "state").completed_results()) == []


def test_http_400_is_terminal_review_without_retry(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    with model_server({}, status=400) as (endpoint, requests):
        first = triage.run_once(spool, tmp_path / "state", config=model_config(endpoint))
        second = triage.run_once(spool, tmp_path / "state", config=model_config(endpoint))
    assert first["review"] == 1
    assert second["unchanged"] == 1
    assert len(requests) == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "review"
    assert state["attempts"] == 1 and state["next_eligible_at"] is None


def test_transport_retry_defers_then_success_preserves_attempt_history(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    monkeypatch.setattr(triage, "_now", lambda: START)
    monkeypatch.setattr(
        triage, "call_model",
        lambda *args: (_ for _ in ()).throw(triage.ModelTransportError("fixed endpoint unavailable")),
    )
    config = model_config("http://example.com:9")
    assert triage.run_once(spool, tmp_path / "state", config=config)["retry"] == 1
    assert triage.run_once(spool, tmp_path / "state", config=config)["deferred"] == 1
    monkeypatch.undo()
    monkeypatch.setattr(triage, "_now", lambda: START + dt.timedelta(seconds=61))
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        counts = triage.run_once(spool, tmp_path / "state", config=model_config(endpoint))
    assert counts["completed"] == 1
    assert len(requests) == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "completed" and state["attempts"] == 2
    assert state["next_eligible_at"] is None


def test_slow_failure_backoff_starts_at_failure_commit_time(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    current = [START]

    def slow_failure(*args: object) -> dict[str, Any]:
        current[0] += dt.timedelta(seconds=300)
        raise triage.ModelTransportError("slow fixed endpoint failure")

    monkeypatch.setattr(triage, "_now", lambda: current[0])
    monkeypatch.setattr(triage, "call_model", slow_failure)
    counts = triage.run_once(
        spool, tmp_path / "state", config=model_config("http://example.com:9"),
    )
    assert counts["retry"] == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    failure_time = START + dt.timedelta(seconds=300)
    assert state is not None and state["updated_at"] == failure_time.isoformat()
    assert state["next_eligible_at"] == (failure_time + dt.timedelta(seconds=60)).isoformat()
    assert triage.run_once(
        spool, tmp_path / "state", config=model_config("http://example.com:9"),
    )["deferred"] == 1


def test_persistent_attempt_ceiling_and_backoff_survive_restarts(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    current = [START]
    calls = 0

    def fail(*args: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise triage.ModelTransportError("fixed endpoint unavailable")

    monkeypatch.setattr(triage, "_now", lambda: current[0])
    monkeypatch.setattr(triage, "call_model", fail)
    config = model_config("http://example.com:9")
    store = triage.TriageStore(tmp_path / "state")
    for attempt in range(1, triage.MAX_ATTEMPTS + 1):
        counts = triage.run_once(spool, tmp_path / "state", config=config)
        assert counts["retry" if attempt < triage.MAX_ATTEMPTS else "review"] == 1
        assert calls == attempt
        state = store.load(event["event_id"])
        assert state is not None and state["attempts"] == attempt
        if attempt < triage.MAX_ATTEMPTS:
            eligible = current[0] + triage._backoff(attempt)
            assert state["next_eligible_at"] == eligible.isoformat()
            assert triage.run_once(spool, tmp_path / "state", config=config)["deferred"] == 1
            assert calls == attempt
            current[0] = eligible
        else:
            assert state["status"] == "review" and state["next_eligible_at"] is None
    assert triage.run_once(spool, tmp_path / "state", config=config)["unchanged"] == 1
    assert calls == triage.MAX_ATTEMPTS
    assert list(store.completed_results()) == []


def test_crash_recovery_persists_backoff_before_completing(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)

    class SimulatedCrash(Exception):
        pass

    def crash(*args: object) -> dict[str, Any]:
        raise SimulatedCrash

    monkeypatch.setattr(triage, "_now", lambda: START)
    monkeypatch.setattr(triage, "call_model", crash)
    config = model_config("http://example.com:9")
    with pytest.raises(SimulatedCrash):
        triage.run_once(spool, tmp_path / "state", config=config)
    interrupted = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert interrupted is not None and interrupted["status"] == "processing"

    monkeypatch.undo()
    monkeypatch.setattr(triage, "_now", lambda: START)
    with model_server(completion(valid_result(event["event_id"]))) as (endpoint, requests):
        config = model_config(endpoint)
        assert triage.run_once(spool, tmp_path / "state", config=config)["deferred"] == 1
        deferred = triage.TriageStore(tmp_path / "state").load(event["event_id"])
        assert deferred is not None and deferred["status"] == "retry"
        assert deferred["next_eligible_at"] == (START + dt.timedelta(seconds=60)).isoformat()
        monkeypatch.setattr(triage, "_now", lambda: START + dt.timedelta(seconds=60))
        counts = triage.run_once(spool, tmp_path / "state", config=config)
    assert counts["completed"] == 1
    assert len(requests) == 1
    recovered = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert recovered is not None and recovered["status"] == "completed"
    assert recovered["attempts"] == 2


def test_empty_allowlisted_content_stays_in_review_without_model_call(tmp_path: Path, monkeypatch):
    event = make_event({"message_id": "native", "childId": "private", "url": "https://example.invalid"})
    spool = tmp_path / "events"
    write_event(spool, event)

    def unexpected(*args: object) -> dict[str, Any]:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(triage, "call_model", unexpected)
    counts = triage.run_once(
        spool, tmp_path / "state", config=model_config("http://example.com:9"),
    )
    assert counts["review"] == 1
    state = triage.TriageStore(tmp_path / "state").load(event["event_id"])
    assert state is not None and state["status"] == "review" and state["attempts"] == 0


def test_corrupt_state_fails_closed_without_calling_model(tmp_path: Path, monkeypatch):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o750)
    triage.TriageStore(state_dir).path_for(event["event_id"]).write_text("{}")

    def unexpected(*args: object) -> dict[str, Any]:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(triage, "call_model", unexpected)
    with pytest.raises(triage.StateError):
        triage.run_once(spool, state_dir, config=model_config("http://example.com:9"))


def test_state_validation_rejects_unknown_status_type_and_missing_retry_time():
    event_id = "evt_" + "a" * 64
    state = triage._new_state(event_id, "b" * 64)
    state["status"] = []
    with pytest.raises(triage.StateError):
        triage.validate_state(state, event_id)
    state = triage._new_state(event_id, "b" * 64)
    state.update(status="retry", error="transient")
    with pytest.raises(triage.StateError):
        triage.validate_state(state, event_id)


def test_pipe04_consumer_hands_off_exact_validated_values(tmp_path: Path):
    event = make_event()
    spool = tmp_path / "events"
    write_event(spool, event)
    expected = valid_result(
        event["event_id"],
        urgency="low",
        summary="Read https://example.test/local-review before rendering",
        deadline="2026-11-06T12:30:00Z",
    )
    with model_server(completion(expected)) as (endpoint, _):
        triage.run_once(spool, tmp_path / "state", config=model_config(endpoint))
    handoff = list(triage.TriageStore(tmp_path / "state").completed_results())
    assert handoff == [expected]
    assert set(handoff[0]) == triage.RESULT_FIELDS


def test_fixed_config_and_state_paths_match_pipeline_contract(config_path: Path):
    assert triage.PIPELINE_CONFIG_PATH == Path("/etc/diary/pipeline.toml")
    assert triage.DEFAULT_STATE_DIR == Path("/var/lib/diary/triage-state")
    assert triage.DEFAULT_SPOOL == Path("/var/lib/diary/events")
    assert triage.load_config(config_path) == triage.PipelineConfig(
        "trusted-local", "127.0.0.1", 8080,
    )


def test_config_has_no_environment_or_collector_config_fallback(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PIPELINE_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("DIARY_CONFIG", "/attacker/config.toml")
    config = triage.load_config(config_path)
    assert (config.model, config.model_ip, config.model_port) == ("trusted-local", "127.0.0.1", 8080)


@pytest.mark.parametrize(
    "extra",
    [
        'endpoint = "http://example.com:8080"\n',
        'alternate_endpoint = "http://example.com:8081"\n',
        '[collector]\ntoken = "must-not-be-here"\n',
    ],
)
def test_config_rejects_unknown_or_collector_settings(config_path: Path, extra: str):
    config_path.write_text(config_path.read_text() + extra)
    with pytest.raises(triage.ConfigError):
        triage.load_config(config_path)


@pytest.mark.parametrize(
    "model",
    ["", "bad/model", "-leading", ".leading", "white space", "unicode-æ", "a" * 129, "line\nbreak", True],
)
def test_config_rejects_model_identifier_outside_pipeline02_grammar(model: Any):
    document = {"pipeline": {"model": model, "model_ip": "127.0.0.1", "model_port": 8080}}
    with pytest.raises(triage.ConfigError):
        triage._parse_document(document)


def test_config_accepts_model_identifier_ascii_boundaries():
    for model in ("A", "a0._-Z", "a" * 128):
        document = {"pipeline": {"model": model, "model_ip": "127.0.0.1", "model_port": 8080}}
        assert triage._parse_document(document).model == model


@pytest.mark.parametrize("model_ip", [None, "model.local", "http://example.com", "::1", "0.0.0.0"])
def test_config_rejects_non_numeric_or_ambiguous_model_addresses(model_ip: Any):
    document = {"pipeline": {"model": "local", "model_ip": model_ip, "model_port": 8080}}
    with pytest.raises(triage.ConfigError):
        triage._parse_document(document)


def test_config_rejects_multicast_model_address():
    document = {"pipeline": {
        "model": "local", "model_ip": ".".join(["224", "0", "0", "1"]), "model_port": 8080,
    }}
    with pytest.raises(triage.ConfigError):
        triage._parse_document(document)


@pytest.mark.parametrize("port", [True, 0, 65536, "8080"])
def test_config_rejects_invalid_model_port(port: Any):
    document = {"pipeline": {"model": "local", "model_ip": "127.0.0.1", "model_port": port}}
    with pytest.raises(triage.ConfigError):
        triage._parse_document(document)


def test_config_rejects_symlink_hardlink_writable_and_wrong_owner(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    link = tmp_path / "link.toml"
    link.symlink_to(config_path)
    with pytest.raises(OSError):
        triage.load_config(link)
    os.link(config_path, tmp_path / "hardlink.toml")
    with pytest.raises(triage.ConfigError):
        triage.load_config(config_path)
    os.unlink(tmp_path / "hardlink.toml")
    config_path.chmod(0o666)
    with pytest.raises(triage.ConfigError):
        triage.load_config(config_path)
    config_path.chmod(0o640)
    monkeypatch.setattr(triage, "ROOT_UID", os.geteuid() + 1)
    with pytest.raises(triage.ConfigError):
        triage.load_config(config_path)
