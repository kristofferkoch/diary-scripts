from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from scripts import diary_pipeline as pipeline


def config(tmp_path: Path, content: str = "") -> Path:
    path = tmp_path / "pipeline.toml"
    path.write_text(content or (
        '[pipeline]\nmodel = "local-test-model"\n'
        'model_ip = "127.0.0.1"\nmodel_port = 8080\n'
    ))
    path.chmod(0o640)
    return path


def test_fixed_config_accepts_numeric_ipv4_and_fixed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    endpoint = pipeline.read_pipeline_config(config(tmp_path))
    assert endpoint == pipeline.ModelEndpoint("127.0.0.1", 8080, "local-test-model")


@pytest.mark.parametrize("content", [
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "model-host"\nmodel_port = 8080\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "http://example.com"\nmodel_port = 8080\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "::1"\nmodel_port = 8080\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "0.0.0.0"\nmodel_port = 8080\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\nmodel_port = true\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\nmodel_port = 0\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\nmodel_port = 65536\n',
    '[pipeline]\nmodel_ip = "127.0.0.1"\nmodel_port = 8080\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\n',
    '[pipeline]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\nmodel_port = 8080\ncloud = "https://example.com"\n',
    '[other]\nmodel = "local-test-model"\nmodel_ip = "127.0.0.1"\nmodel_port = 8080\n',
])
def test_fixed_config_rejects_selectable_or_invalid_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
):
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    with pytest.raises(pipeline.PipelineError):
        pipeline.read_pipeline_config(config(tmp_path, content))


@pytest.mark.parametrize("model_value", [
    '""', '"local model"', '"local/model"', '"/models/local.gguf"',
    '"https://example.com/model"', '"-local"', '"local:8080"',
    'true', '42', '"' + "a" * 129 + '"',
])
def test_fixed_config_rejects_unsafe_model_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_value: str
):
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    content = (
        f'[pipeline]\nmodel = {model_value}\n'
        'model_ip = "127.0.0.1"\nmodel_port = 8080\n'
    )
    with pytest.raises(pipeline.PipelineError, match="model must be"):
        pipeline.read_pipeline_config(config(tmp_path, content))


def test_config_rejects_symlink_and_agent_writable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    valid = config(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(valid)
    with pytest.raises(OSError):
        pipeline.read_pipeline_config(link)
    valid.chmod(0o666)
    with pytest.raises(pipeline.PipelineError, match="root-owned"):
        pipeline.read_pipeline_config(valid)


def test_config_rejects_non_root_owner_and_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = config(tmp_path)
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid() + 1)
    with pytest.raises(pipeline.PipelineError, match="root-owned"):
        pipeline.read_pipeline_config(path)
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    link = tmp_path / "hardlink.toml"
    os.link(path, link)
    with pytest.raises(pipeline.PipelineError, match="single-link"):
        pipeline.read_pipeline_config(path)


def test_environment_cannot_select_config_or_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = config(tmp_path)
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    monkeypatch.setenv("DIARY_CONFIG", str(tmp_path / "other.toml"))
    monkeypatch.setenv("DIARY_PIPELINE_CONFIG", str(tmp_path / "other.toml"))
    assert pipeline.read_pipeline_config(path).address == "127.0.0.1"


@contextmanager
def health_server(status: int = 200, location: str | None = None) -> Iterator[tuple[str, int, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            body = json.dumps({"status": "ok"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_fixed_model_health_succeeds():
    with health_server() as (address, port, requests):
        pipeline.probe_model(pipeline.ModelEndpoint(address, port, "local-test-model"))
    assert requests == ["/health"]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500])
def test_health_does_not_follow_redirects_or_accept_other_endpoints(status: int):
    with health_server(status=status, location="http://example.com/health") as (address, port, requests):
        with pytest.raises(pipeline.PipelineError, match=f"HTTP {status}"):
            pipeline.probe_model(pipeline.ModelEndpoint(address, port, "local-test-model"))
    assert requests == ["/health"]


def test_main_has_no_endpoint_or_config_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = config(tmp_path)
    monkeypatch.setattr(pipeline, "ROOT_UID", os.getuid())
    monkeypatch.setattr(pipeline, "PIPELINE_CONFIG_PATH", path)
    with pytest.raises(SystemExit) as error:
        pipeline.main(["--model-ip", "192.0.2.1"])
    assert error.value.code == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
