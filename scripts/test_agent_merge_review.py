from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from scripts import agent_merge_review as review


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit_file(repo: Path, name: str, content: str, message: str = "change") -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def make_queue(tmp_path: Path, content: str = "VALUE = 1\n") -> tuple[Path, str]:
    queue = tmp_path / "queue"
    queue.mkdir()
    git(queue, "init", "-qb", "master")
    git(queue, "config", "user.name", "Reviewer Test")
    git(queue, "config", "user.email", "reviewer@example.invalid")
    commit_file(queue, "README.md", "base\n", "base")
    base = git(queue, "rev-parse", "HEAD")
    git(queue, "checkout", "-qb", "queue/automatic")
    commit_file(queue, "example.py", content)
    return queue, base


def config(tmp_path: Path, *, max_diff: int = 120_000) -> Path:
    path = tmp_path / "review.toml"
    path.write_text(
        "[review]\n"
        'model = "local-test-model"\n'
        'allowed_identities = ["Fake Person", ".example"]\n'
        f"max_diff_chars = {max_diff}\n"
    )
    return path


def completion(summary: str = "Looks reasonable", findings: list[dict] | None = None) -> dict:
    return {
        "choices": [{
            "message": {
                "content": json.dumps({"summary": summary, "findings": findings or []})
            }
        }]
    }


@contextmanager
def model_server(
    response: dict | None = None, redirect_to: str | None = None
) -> Iterator[tuple[str, list[dict]]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(length)))
            if redirect_to is not None:
                self.send_response(307)
                self.send_header("Location", redirect_to + self.path)
                self.end_headers()
                return
            raw = json.dumps(response or completion()).encode()
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


def test_model_request_does_not_follow_redirects():
    with model_server() as (target, target_requests):
        with model_server(redirect_to=target) as (redirect, _):
            settings = review.ReviewConfig("local", (), 120_000)
            with pytest.raises(review.ReviewError):
                review.call_model(redirect, settings, "{}")
    assert target_requests == []


def test_prescan_allowlists_fake_values_and_redacts_unapproved_values():
    current = review.Snapshot(
        "a" * 40,
        "b" * 40,
        """--- a/example.py
+++ b/example.py
-old = \"person@private.invalid\"
+host = \"private-host\"
+email = \"person@private-mail.no\"
+fake_email = \"person@example.test\"
+fake_name = \"Fake Person\"
+documentation = \"203.0.113.10\"
+private_ip = \"10.20.30.40\"
""",
        "Contact person@private-commit.no\n",
        ("example.py",),
    )
    findings, diff, messages = review.prescan(
        current,
        review.ReviewConfig("model", (*review.DEFAULT_FAKE_IDENTITIES, "Fake Person", ".example"), 120_000),
    )
    assert "private-host" not in diff
    assert "person@private-mail.no" not in diff
    assert "person@private.invalid" not in diff
    assert "10.20.30.40" not in diff
    assert "person@example.test" in diff
    assert "Fake Person" in diff
    assert "203.0.113.10" in diff
    assert "person@private-commit.no" not in messages
    assert {item.kind for item in findings} == {"identity", "email", "network-identity"}
    assert all("private-host" not in item.detail for item in findings)


def test_review_once_calls_local_http_model_and_binds_exact_oid(tmp_path: Path):
    queue, base = make_queue(
        tmp_path,
        'host = "private-host"\nrecipient = "person@private-mail.no"\n',
    )
    with model_server(completion(findings=[{
        "kind": "security", "path": "example.py", "detail": "Check outbound handling"
    }])) as (endpoint, requests):
        path = review.review_once(queue, tmp_path / "reports", config(tmp_path), endpoint=endpoint)
    assert path is not None
    report = review.load_report(path)
    assert report["queue_oid"] == git(queue, "rev-parse", "HEAD")
    assert report["base_oid"] == base
    assert report["model_status"] == "completed"
    assert report["approval"] == "owner-required"
    assert report["stale"] is False
    assert report["model_findings"][0]["kind"] == "security"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.dumps(requests[0])
    assert "private-host" not in payload
    assert "person@private-mail.no" not in payload
    assert requests[0]["model"] == "local-test-model"
    assert requests[0]["cache_prompt"] is False
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    rendered = review.render_report(report)
    assert "Owner review and exact-OID approval are still required" in rendered
    assert "Check outbound handling" in rendered


def test_existing_report_is_not_recomputed_without_force(tmp_path: Path):
    queue, _ = make_queue(tmp_path)
    with model_server() as (endpoint, requests):
        first = review.review_once(queue, tmp_path / "reports", config(tmp_path), endpoint=endpoint)
        second = review.review_once(queue, tmp_path / "reports", config(tmp_path), endpoint=endpoint)
    assert first == second
    assert len(requests) == 1


def test_invalid_model_response_becomes_advisory_error(tmp_path: Path):
    queue, _ = make_queue(tmp_path)
    response = {"choices": [{"message": {"content": "not JSON"}}]}
    with model_server(response) as (endpoint, _):
        path = review.review_once(queue, tmp_path / "reports", config(tmp_path), endpoint=endpoint)
    assert path is not None
    report = review.load_report(path)
    assert report["model_status"] == "error"
    assert report["approval"] == "owner-required"


def test_queue_change_during_review_marks_report_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue, _ = make_queue(tmp_path)

    def change_queue(*args: object, **kwargs: object) -> dict:
        commit_file(queue, "later.py", "LATER = True\n")
        return completion()

    monkeypatch.setattr(review, "call_model", change_queue)
    path = review.review_once(queue, tmp_path / "reports", config(tmp_path))
    assert path is not None
    report = review.load_report(path)
    assert report["stale"] is True
    assert report["queue_oid"] != git(queue, "rev-parse", "HEAD")


def test_run_follows_queue_change_until_head_is_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue, _ = make_queue(tmp_path)
    calls = 0

    def change_once(*args: object, **kwargs: object) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            commit_file(queue, "later.py", "LATER = True\n")
        return completion()

    monkeypatch.setattr(review, "call_model", change_once)
    result = review.main([
        "run", "--queue", str(queue), "--report-dir", str(tmp_path / "reports"),
        "--config", str(config(tmp_path)),
    ])
    assert result == 0
    assert calls == 2
    current = git(queue, "rev-parse", "HEAD")
    report = review.load_report(tmp_path / "reports" / f"{current}.json")
    assert report["stale"] is False


def test_oversized_review_skips_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue, _ = make_queue(tmp_path, "VALUE = \"" + "x" * 2_000 + "\"\n")

    def unexpected(*args: object, **kwargs: object) -> dict:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(review, "call_model", unexpected)
    path = review.review_once(queue, tmp_path / "reports", config(tmp_path, max_diff=1_000))
    assert path is not None
    assert review.load_report(path)["model_status"] == "skipped"


def test_empty_queue_does_not_call_model(tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir()
    git(queue, "init", "-qb", "master")
    git(queue, "config", "user.name", "Reviewer Test")
    git(queue, "config", "user.email", "reviewer@example.invalid")
    commit_file(queue, "README.md", "base\n")
    git(queue, "checkout", "-qb", "queue/automatic")
    assert review.review_once(queue, tmp_path / "reports", config(tmp_path)) is None


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra=True),
    lambda value: value["findings"].append({"kind": "approve", "path": "", "detail": "bad"}),
    lambda value: value.update(summary="bad\u202eoutput"),
])
def test_model_schema_fails_closed(mutation):
    value = {"summary": "summary", "findings": []}
    mutation(value)
    response = {"choices": [{"message": {"content": json.dumps(value)}}]}
    with pytest.raises(review.ReviewError):
        review.parse_model_response(response)


def test_config_rejects_unknown_keys_and_symlinks(tmp_path: Path):
    path = config(tmp_path)
    path.write_text(path.read_text() + "endpoint = \"https://example.com\"\n")
    with pytest.raises(review.ReviewError):
        review.read_config(path)
    (tmp_path / "valid").mkdir()
    valid = config(tmp_path / "valid")
    valid.write_text("[review]\nmodel = \"local\"\n")
    link = tmp_path / "link.toml"
    link.symlink_to(valid)
    with pytest.raises(OSError):
        review.read_config(link)


def test_render_does_not_emit_terminal_controls_from_tampered_report(tmp_path: Path):
    queue, _ = make_queue(tmp_path)
    with model_server() as (endpoint, _):
        path = review.review_once(queue, tmp_path / "reports", config(tmp_path), endpoint=endpoint)
    assert path is not None
    report = review.load_report(path)
    report["model_summary"] = "safe\x1b[2J\u202e"
    rendered = review.render_report(report)
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered
