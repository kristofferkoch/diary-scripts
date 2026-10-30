"""Authentication and token-file tests for the private mail web UI."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_reader import auth


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "mail-token"
    path.write_text("x" * 43 + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def client(token_file: Path) -> TestClient:
    app = FastAPI(root_path="/mail")
    app.add_middleware(auth.MailAuthMiddleware, path=token_file)

    @app.get("/")
    def private() -> dict[str, bool]:
        return {"private": True}

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, base_url="https://testserver")


def test_private_routes_require_auth_but_health_is_public(client: TestClient):
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    login = client.get("/login")
    assert login.status_code == 200
    assert "x" * 43 not in login.text
    assert 'name="token"' in login.text


def test_login_exchanges_fragment_token_for_secure_cookie(client: TestClient):
    assert client.post("/login", json={"token": "wrong"}).status_code == 401
    response = client.post("/login", json={"token": "x" * 43})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/mail" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    client.cookies.set(auth.COOKIE_NAME, "x" * 43)
    assert client.get("/").json() == {"private": True}


def test_bearer_token_is_accepted_for_api_clients(client: TestClient):
    response = client.get("/", headers={"Authorization": "Bearer " + "x" * 43})
    assert response.status_code == 200


def test_form_login_is_supported_without_json(client: TestClient):
    response = client.post("/login", data={"token": "x" * 43})
    assert response.status_code == 204
    client.cookies.set(auth.COOKIE_NAME, "x" * 43)
    assert client.get("/").status_code == 200


def test_missing_token_fails_closed(token_file: Path, client: TestClient):
    token_file.unlink()
    assert client.get("/").status_code == 503


def test_group_readable_token_is_rejected(token_file: Path):
    token_file.chmod(0o640)
    with pytest.raises(auth.AuthError):
        auth.token_for_notification(token_file)


def test_token_generation_is_private_persistent_and_rotatable(tmp_path: Path):
    path = tmp_path / "state" / "mail-token"
    first = auth.token_for_notification(path)
    second = auth.token_for_notification(path)
    rotated = auth.token_for_notification(path, rotate=True)

    assert first == second
    assert rotated != first
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
