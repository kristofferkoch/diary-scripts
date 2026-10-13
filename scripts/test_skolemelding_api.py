"""Tests for skolemelding_api.py. Run with `uv run pytest scripts/`.

The shapes pinned here are the API's as mapped by the upstream
skolemelding-mcp client — with fabricated ids and names. They exist so that
the day Skoleplattform changes something under this unofficial client, a test
fails instead of the pipeline quietly returning empty lists.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl

import httpx
import pytest

from scripts import skolemelding_api as sa
from scripts.skolemelding_api import (
    ApiError,
    AuthExpiredError,
    SkolemeldingApi,
    SkolemeldingError,
    TokenStore,
    app_version,
    authorize_url,
    code_from_redirect,
    file_lock,
    pkce_pair,
    token_lifetime,
    write_json_0600,
)


class Resp:
    """A canned non-200 answer: ``Resp(401, {"message": "denied"})``."""

    def __init__(self, status: int, payload: Any = None) -> None:
        self.status = status
        self.payload = payload


Route = Callable[[httpx.Request], httpx.Response] | Resp | dict[str, Any] | list[Any] | None


def transport(routes: dict[str, Route]) -> httpx.MockTransport:
    """MockTransport keyed by ``"METHOD /path"`` (query strings live on the request)."""

    def _handle(request: httpx.Request) -> httpx.Response:
        body = routes.get(f"{request.method} {request.url.path}")
        if isinstance(body, Resp):
            return httpx.Response(body.status, json=body.payload)
        if callable(body):
            stub = cast("Callable[[httpx.Request], httpx.Response]", body)
            return stub(request)
        if body is None:
            return httpx.Response(
                404, json={"message": f"no stub for {request.method} {request.url.path}"}
            )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(_handle)


def login_payload(expires_in: int = 1680, **extra: Any) -> dict:
    """The persisted token-file shape: camelCase, expiresAt in seconds."""
    return {
        "accessToken": "old-at",
        "refreshToken": "old-rt",
        "userName": "user-1",
        "deviceId": "device-1",
        "expiresAt": time.time() + expires_in,
        **extra,
    }


REFRESH_OK: Route = {"access_token": "new-at", "refresh_token": "rotated", "expires_in": 1680}


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "skolemelding-auth.json"
    monkeypatch.setenv("SKOLEMELDING_TOKEN_FILE", str(path))
    return path


@pytest.fixture
def store(token_file: Path) -> TokenStore:
    return TokenStore(token_file)


@pytest.fixture
def api_factory(store: TokenStore):
    """A SkolemeldingApi on a stub transport, plus its store with a live login."""

    def _make(routes: dict[str, Route], *, tokens: dict[str, Any] | None = None) -> SkolemeldingApi:
        t = transport({**routes, "POST /token": routes.get("POST /token", REFRESH_OK)})
        store.transport = t
        store.save(tokens if tokens is not None else login_payload())
        return SkolemeldingApi(store, transport=t)

    return _make


# --- PKCE + authorize URL ----------------------------------------------------


def test_pkce_pair_is_s256_of_the_verifier():
    verifier, challenge = pkce_pair()
    assert len(verifier) == 43 and len(challenge) == 43
    digest = hashlib.sha256(verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == challenge


def test_authorize_url_is_the_shape_the_app_sends():
    url = authorize_url("https://idporten.no/authorize", "state-123", "challenge-abc")
    assert url.startswith("https://idporten.no/authorize?")
    assert "response_type=code" in url
    assert "client_id=d12dad8a-f092-4a96-8a37-371590825d22" in url
    assert "redirect_uri=com.cgi.oslomelding.foresatte%3A%2F%2Foauth" in url
    assert "scope=openid%20profile" in url, "scope separator must be %20"
    assert "code_challenge=challenge-abc" in url
    assert "code_challenge_method=S256" in url
    assert "ui_locales=nb" in url
    assert "prompt=login" in url
    assert "state=state-123" in url


def test_code_from_redirect_takes_the_failed_url_or_a_bare_code():
    assert code_from_redirect("com.cgi.oslomelding.foresatte://oauth?code=XYZ&state=S") == "XYZ"
    assert code_from_redirect("XYZ") == "XYZ"
    assert code_from_redirect("  XYZ\n") == "XYZ"


def test_code_from_redirect_rejects_a_stale_state():
    with pytest.raises(ValueError, match="does not match"):
        code_from_redirect("com.cgi.oslomelding.foresatte://oauth?code=XYZ&state=OLD", "NEW")


def test_code_from_redirect_says_so_for_a_login_page():
    with pytest.raises(ValueError, match="no code"):
        code_from_redirect("https://idporten.no/login?signin=abc")


# --- Discovery ---------------------------------------------------------------


def test_discovery_pins_the_issuer():
    t = transport({"GET /.well-known/openid-configuration": {
        "issuer": "https://idporten.no",
        "authorization_endpoint": "https://idporten.no/authorize",
        "token_endpoint": "https://idporten.no/token",
    }})
    assert sa.idporten_metadata(transport=t)["issuer"] == "https://idporten.no"


def test_discovery_rejects_an_unexpected_issuer():
    t = transport({"GET /.well-known/openid-configuration": {"issuer": "https://evil.example"}})
    with pytest.raises(ApiError, match="issuer"):
        sa.idporten_metadata(transport=t)


# --- The login chain ---------------------------------------------------------


def test_login_from_code_runs_the_whole_chain():
    seen: dict[str, httpx.Request] = {}

    def _token(request: httpx.Request) -> httpx.Response:
        fields = dict(parse_qsl(request.content.decode()))
        if request.url.host == "idporten.no":
            seen["idporten"] = request
            assert fields["grant_type"] == "authorization_code"
            assert fields["code"] == "code-1"
            assert fields["code_verifier"] == "verifier-1"
            assert fields["client_id"] == sa.OIDC_CLIENT_ID
            assert "client_secret" not in fields, "public client: no secret anywhere"
            return httpx.Response(200, json={"access_token": "oidc-at"})
        seen["portal"] = request
        assert fields["grant_type"] == "password"
        assert fields["username"] == "authcode-1"
        assert fields["password"] == "code", "the authCode grant's password is literal"
        assert fields["client_id"] == "consoleAPP"
        assert fields["app_version"] == app_version()
        assert re.fullmatch(r"[0-9a-f-]{36}\|\d+\|Foresatte", fields["scope"]), \
            "scope is <deviceId>|<unix ts>|Foresatte"
        return httpx.Response(200, json={
            "access_token": "app-at", "refresh_token": "app-rt",
            "userName": "user-1", "expires_in": 1680,
        })

    def _exchange(request: httpx.Request) -> httpx.Response:
        seen["exchange"] = request
        assert json.loads(request.content) == {"accessToken": "oidc-at", "provider": "idporten"}
        return httpx.Response(200, json={"authCode": "authcode-1"})

    def _device(request: httpx.Request) -> httpx.Response:
        seen["device"] = request
        assert request.headers["authorization"] == "Bearer app-at"
        body = json.loads(request.content)
        assert body["AppVersion"] == app_version()
        assert "|" in body["OsVersion"]
        return httpx.Response(200, json={})

    t = transport({
        "POST /token": _token,
        "POST /api/exchange-app-token": _exchange,
        "POST /api/deviceinfo/save": _device,
    })
    stored = sa.login_from_code("code-1", "verifier-1", "https://idporten.no/token", transport=t)
    assert set(seen) == {"idporten", "exchange", "portal", "device"}
    assert stored["accessToken"] == "app-at"
    assert stored["refreshToken"] == "app-rt"
    assert stored["userName"] == "user-1"
    assert re.fullmatch(r"[0-9a-f-]{36}", stored["deviceId"])
    assert stored["expiresAt"] == pytest.approx(time.time() + 1680, abs=10)


def test_exchange_without_auth_code_fails_loudly():
    t = transport({"POST /api/exchange-app-token": {"something": "else"}})
    with pytest.raises(ApiError, match="authCode"):
        sa.exchange_app_token("oidc-at", transport=t)


def test_a_refused_code_exchange_is_an_expired_login():
    t = transport({"POST /token": Resp(400, {"error": "invalid_grant"})})
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        sa.exchange_idporten_code("https://idporten.no/token", "code-1", "v", transport=t)


# --- Token store: rotation, races, lifetime --------------------------------


def test_store_writes_0600_in_a_0700_dir(store: TokenStore):
    store.save(login_payload())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.load()["refreshToken"] == "old-rt"


def test_expired_access_token_rotates_and_persists_the_new_refresh(store: TokenStore):
    store.save(login_payload(expires_in=-10))
    store.transport = transport({"POST /token": REFRESH_OK})
    assert store.access_token() == "new-at"
    saved = json.loads(store.path.read_text())
    assert saved["refreshToken"] == "rotated", "a rotated refresh token must be persisted"
    assert saved["expiresAt"] == pytest.approx(time.time() + 1680, abs=10)


def test_refresh_keeps_the_old_refresh_token_when_none_is_returned(store: TokenStore):
    """The portal does not always rotate — a missing refresh_token is not a wipe."""
    store.save(login_payload(expires_in=-10))
    store.transport = transport({"POST /token": {"access_token": "new-at", "expires_in": 1680}})
    assert store.access_token() == "new-at"
    assert json.loads(store.path.read_text())["refreshToken"] == "old-rt"


def test_refresh_sends_the_app_shaped_form(store: TokenStore):
    store.save(login_payload(expires_in=-10))
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "new-at", "expires_in": 1680})

    store.transport = transport({"POST /token": _ok})
    store.access_token()
    fields = dict(parse_qsl(seen[0].content.decode()))
    assert fields == {
        "grant_type": "refresh_token",
        "refresh_token": "old-rt",
        "client_id": "consoleAPP",
        "app_version": app_version(),
        "username": "user-1",
    }


def test_valid_access_token_is_not_refreshed(store: TokenStore):
    store.save(login_payload())
    calls: list[str] = []

    def _spy(request: httpx.Request) -> httpx.Response:
        calls.append("hit")
        return httpx.Response(200, json={})

    store.transport = transport({"POST /token": _spy})
    assert store.access_token() == "old-at"
    assert calls == [], "a live token must not be burned on a refresh"
    assert json.loads(store.path.read_text())["refreshToken"] == "old-rt"


def test_a_re_login_landing_mid_refresh_is_not_clobbered(store: TokenStore):
    """We refresh from an old token; while we are on the wire a re-login writes
    a fresh pair; saving what *we* derived would invalidate the login that
    just happened. The file must win."""
    store.save(login_payload(expires_in=-10))

    def _refresh(request: httpx.Request) -> httpx.Response:
        store.path.write_text(json.dumps({
            "accessToken": "theirs-at", "refreshToken": "from-relogin",
            "userName": "user-1", "deviceId": "device-2",
            "expiresAt": time.time() + 1680,
        }))
        return httpx.Response(200, json={"access_token": "ours-at",
                                         "refresh_token": "from-our-stale-refresh",
                                         "expires_in": 1680})

    store.transport = transport({"POST /token": _refresh})
    assert store.access_token() == "theirs-at"
    saved = json.loads(store.path.read_text())
    assert saved["refreshToken"] == "from-relogin"
    assert saved["accessToken"] == "theirs-at"


def test_dead_refresh_token_raises_auth_expired(store: TokenStore):
    store.save(login_payload(expires_in=-10))
    store.transport = transport({"POST /token": Resp(400, {"error": "invalid_grant"})})
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        store.access_token()


def test_http_401_on_refresh_also_means_re_login(store: TokenStore):
    store.save(login_payload(expires_in=-10))
    store.transport = transport({"POST /token": Resp(401, {"error": "invalid_grant"})})
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        store.access_token()


def test_missing_token_file_points_at_the_login_command(store: TokenStore):
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        store.access_token()


def test_a_login_without_expires_at_is_treated_as_expired(store: TokenStore):
    assert store.needs_refresh({"accessToken": "x"}) is True
    assert store.needs_refresh({"expiresAt": time.time() + 3600}) is False
    assert store.needs_refresh({"expiresAt": time.time() + 30}) is True


def test_token_lifetime_floors_and_defaults():
    assert token_lifetime(1680) == 1680.0
    assert token_lifetime(None) == 1680.0
    assert token_lifetime("junk") == 1680.0
    assert token_lifetime(5) == 60.0


# --- File lock and atomic write ----------------------------------------------


def test_lock_is_exclusive_then_released(tmp_path: Path):
    lock = tmp_path / "auth.json.lock"
    with file_lock(lock):
        assert lock.exists()
        with pytest.raises(SkolemeldingError):
            with file_lock(lock, timeout=0.2):
                pass
    assert not lock.exists()


def test_a_stale_lock_is_broken(tmp_path: Path):
    lock = tmp_path / "auth.json.lock"
    lock.write_text("crashed")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with file_lock(lock, timeout=1.0, stale_after=90):
        assert lock.exists()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    target = tmp_path / "sub" / "thing.json"
    write_json_0600(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["thing.json"]


# --- Message search ----------------------------------------------------------


def _message(mid: int, header: str = "Melding") -> dict[str, Any]:
    return {"Id": mid, "Header": header, "From": "Skolen", "Timestamp": "2026-10-12T08:00:00",
            "IsMessageSeen": False, "Body": "<p>Hei</p>", "Attachments": []}


def test_search_sends_the_app_shaped_body(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"Result": [_message(1)], "TotalMatching": 1})

    api = api_factory({"POST /api/messagesearchmobile/": _ok})
    data = api.search_messages()
    assert data["TotalMatching"] == 1
    assert seen[0].headers["authorization"] == "Bearer old-at"
    body = json.loads(seen[0].content)
    assert body == {
        "Page": 1, "PageSize": 15, "SearchTerm": "", "SearchInbox": True,
        "FilterSender": None, "FilterUnreadMessages": False,
        "FilterFeedbackRequired": False, "FilterFeedbackIsQuestion": False,
        "FilterIsAbsentMessage": False, "OnBehalfOf": "", "AuthorUserCategory": None,
        "FilterFavouriteMessages": False, "SendHiddenMessages": False,
    }


def test_search_without_a_result_list_is_an_api_error(api_factory):
    api = api_factory({"POST /api/messagesearchmobile/": {"TotalMatching": 3}})
    with pytest.raises(ApiError, match="Result"):
        api.search_messages()


def test_find_message_pages_until_the_id_shows_up(api_factory):
    pages: list[int] = []

    def _paged(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["Page"]
        pages.append(page)
        if page == 1:
            return httpx.Response(200, json={"Result": [_message(1), _message(2)],
                                             "TotalMatching": 4})
        return httpx.Response(200, json={"Result": [_message(3), _message(4, "Funnet")],
                                         "TotalMatching": 4})

    api = api_factory({"POST /api/messagesearchmobile/": _paged})
    message = api.find_message(4, page_size=2)
    assert message["Header"] == "Funnet"
    assert pages == [1, 2]


def test_find_message_stops_at_total_matching(api_factory):
    pages: list[int] = []

    def _paged(request: httpx.Request) -> httpx.Response:
        pages.append(json.loads(request.content)["Page"])
        return httpx.Response(200, json={"Result": [_message(1)], "TotalMatching": 1})

    api = api_factory({"POST /api/messagesearchmobile/": _paged})
    with pytest.raises(ApiError, match="not found"):
        api.find_message(999, page_size=15)
    assert pages == [1], "TotalMatching=1 means page 2 can hold nothing new"


# --- Attachments -------------------------------------------------------------


def test_attachment_downloads_with_the_bearer(api_factory):
    seen: list[httpx.Request] = []

    def _file(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.4",
                              headers={"content-type": "application/pdf"})

    api = api_factory({"GET /globalassets/attachments/x/skjema.pdf": _file})
    content, mime = api.attachment("/globalassets/attachments/x/skjema.pdf")
    assert content == b"%PDF-1.4"
    assert mime == "application/pdf"
    assert seen[0].headers["authorization"] == "Bearer old-at"


def test_an_html_answer_is_a_dead_token_not_a_document(api_factory):
    """The portal answers an expired Bearer with its HTML login page."""
    def _html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>",
                              headers={"content-type": "text/html; charset=utf-8"})

    api = api_factory({"GET /globalassets/attachments/x/skjema.pdf": _html})
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        api.attachment("/globalassets/attachments/x/skjema.pdf")


def test_oversize_attachments_are_refused(api_factory):
    def _big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100,
                              headers={"content-type": "application/pdf",
                                       "content-length": str(sa.ATTACHMENT_MAX_BYTES + 1)})

    api = api_factory({"GET /globalassets/attachments/x/big.pdf": _big})
    with pytest.raises(ApiError, match="limit"):
        api.attachment("/globalassets/attachments/x/big.pdf")


# --- Failure handling on the API client -------------------------------------


def test_401_forces_exactly_one_refresh_then_succeeds(api_factory, store: TokenStore,
                                                      monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    hits = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(401, json={"message": "denied"})
        return httpx.Response(200, json={"Result": [], "TotalMatching": 0})

    api = api_factory({"POST /api/messagesearchmobile/": _flaky})
    assert api.search_messages()["Result"] == []
    assert hits["n"] == 2
    assert json.loads(store.path.read_text())["refreshToken"] == "rotated"


def test_a_second_401_is_terminal(api_factory, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    api = api_factory({"POST /api/messagesearchmobile/": Resp(401, {"message": "denied"})})
    with pytest.raises(AuthExpiredError, match="skolemelding-login"):
        api.search_messages()


def test_server_errors_are_retried_with_growing_backoff(api_factory,
                                                        monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    api = api_factory({"POST /api/messagesearchmobile/": _down})
    with pytest.raises(ApiError, match="503"):
        api.search_messages()
    assert len(slept) == sa.MAX_ATTEMPTS - 1
    assert slept == sorted(slept) and slept[0] < slept[-1]


def test_probe_shows_a_refusal_instead_of_raising(api_factory):
    """Diagnostics have to be able to look at a 401, not be rescued from it."""
    api = api_factory({"GET /api/ping": Resp(401, {"message": "denied"})})
    assert api.probe("/api/ping").status_code == 401
