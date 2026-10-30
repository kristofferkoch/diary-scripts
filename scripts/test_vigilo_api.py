"""Tests for vigilo_api.py. Run with `uv run pytest scripts/`.

The shapes pinned here are the real API's, as recorded in docs/vigilo-pipeline.md
Appendix A — with fabricated UUIDs and names. They exist so that the day
Vigilo changes something under this unofficial client, a test fails instead of
the pipeline quietly returning empty lists.
"""
from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from typing import Any, cast
from datetime import date
from pathlib import Path

import httpx
import pytest

from scripts import vigilo_api as va
from scripts.vigilo_api import (
    ApiError,
    AuthExpiredError,
    ConfigError,
    TokenStore,
    VigiloApi,
    VigiloError,
    app_version,
    as_items,
    authorize_url,
    code_from_redirect,
    file_lock,
    iso_week,
    jwt_sub,
    person_name,
    thread_signature,
    unit_name,
    write_json_0600,
)

CHILD_ID = "11111111-2222-3333-4444-555555555555"
USER_SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

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


def make_jwt(sub: str = USER_SUB, **claims: str) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    payload = {"sub": sub} if sub else {}
    payload.update(claims)
    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.sig"


def token_pair(refresh: str = "refresh-1", expires_in: int = 3600, **extra: Any) -> dict:
    return {
        "access_token": make_jwt(),
        "refresh_token": refresh,
        "expires_in": expires_in,
        "obtained_at": time.time(),
        **extra,
    }


REFRESH_OK: Route = {"access_token": make_jwt(), "refresh_token": "rotated", "expires_in": 3600}


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the two pass entries."""
    monkeypatch.setenv("VIGILO_CLIENT_ID", "client-id.ANDROID")
    monkeypatch.setenv("VIGILO_CLIENT_SECRET", "client-secret")


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vigilo-tokens.json"
    monkeypatch.setenv("VIGILO_TOKEN_FILE", str(path))
    return path


@pytest.fixture
def store(token_file: Path) -> TokenStore:
    return TokenStore(token_file)


@pytest.fixture
def api_factory(store: TokenStore, creds: None):
    """A VigiloApi whose two gateways share one stub transport, plus its store."""

    def _make(routes: dict[str, Route], *, tokens: dict[str, Any] | None = None) -> VigiloApi:
        t = transport({**routes, "POST /connect/token": routes.get("POST /connect/token", REFRESH_OK)})
        store.transport = t
        store.save(tokens if tokens is not None else token_pair())
        api = VigiloApi(store, transport=t)
        api._user_id = USER_SUB          # no need to decode a fresh JWT per test
        return api

    return _make


# --- Shape tolerance: the endpoints disagree with each other ----------------


def test_as_items_accepts_bare_list_and_every_wrapper():
    assert as_items([{"a": 1}]) == [{"a": 1}]
    assert as_items({"items": [{"a": 1}]}) == [{"a": 1}]
    assert as_items({"messageThreads": [{"a": 1}]}, "messageThreads", "items") == [{"a": 1}]
    assert as_items({"children": [{"a": 1}]}, "items", "children") == [{"a": 1}]
    assert as_items({"items": "no"}) == []
    assert as_items(None) == []
    assert as_items([{"a": 1}, "junk"]) == [{"a": 1}]   # junk entries dropped


def test_unit_name_is_dict_or_string_or_nothing():
    assert unit_name({"name": "AKS"}) == "AKS"
    assert unit_name({"id": "u-1"}) == "u-1"
    assert unit_name("Enhet") == "Enhet"
    assert unit_name(None) == ""
    assert unit_name(17) == ""


def test_person_name_is_dict_or_string_or_nothing():
    assert person_name({"firstName": "Kari", "lastName": "Nordmann"}) == "Kari Nordmann"
    assert person_name({"firstName": "Kari"}) == "Kari"
    assert person_name({"name": "AKS"}) == "AKS"
    assert person_name("AKS") == "AKS"
    assert person_name(None) == ""


def test_thread_signature_moves_on_a_reply():
    same_time = {"timeCreated": "2026-10-09T07:30:00Z"}
    before = thread_signature({**same_time, "messageCount": 1})
    after = thread_signature({**same_time, "messageCount": 2})
    assert before != after, "a reply that does not move the signature is invisible"
    assert thread_signature({"timeCreated": "T1", "messageCount": 1}) == "T1|1"


def test_thread_signature_falls_back_to_alternate_spellings():
    assert thread_signature({"lastMessageTimeCreated": "T", "messagesCount": 3}) == "T|3"


def test_thread_signature_is_empty_when_there_is_no_change_signal():
    assert thread_signature({"threadUid": "x"}) == ""


# --- Authorize URL and code capture ----------------------------------------


def test_authorize_url_is_the_shape_that_actually_works(creds: None):
    url = authorize_url("state-123")
    assert url.startswith("https://auth.prod.vigilo-oas.no/connect/authorize?")
    assert "code_challenge" not in url, "PKCE makes this confidential client fail invalid_grant"
    assert "code_verifier" not in url
    assert "prompt=login" in url
    assert "display=touch" in url
    assert "state=state-123" in url
    assert "scope=openid%20vigiloprofile%20offline_access" in url, "scope separator must be %20"
    assert "openid+vigiloprofile" not in url
    assert "redirect_uri=app%3A%2F%2Fch-parent-android.vigilo.no" in url


def test_authorize_url_omits_prompt_for_session_replays(creds: None):
    # --from-curl replays an already-logged-in browser session; prompt=login
    # would bounce it to the ID-porten login page instead of the code redirect.
    url = authorize_url("state-123", prompt=None)
    assert "prompt" not in url
    assert "state=state-123" in url


def test_code_from_redirect_takes_the_failed_url_or_a_bare_code():
    assert code_from_redirect("app://ch-parent-android.vigilo.no?code=XYZ&state=S") == "XYZ"
    assert code_from_redirect("XYZ") == "XYZ"
    assert code_from_redirect("  XYZ\n") == "XYZ"


def test_code_from_redirect_rejects_a_stale_state():
    with pytest.raises(ValueError, match="does not match"):
        code_from_redirect("app://x?code=XYZ&state=OLD", "NEW")


def test_code_from_redirect_says_so_for_a_login_page():
    with pytest.raises(ValueError, match="no code"):
        code_from_redirect("https://auth.prod.vigilo-oas.no/login?signin=abc")


# --- Token endpoint: the two 400s are different animals --------------------


def test_invalid_grant_is_an_expired_login(creds: None):
    t = transport({"POST /connect/token": Resp(400, {"error": "invalid_grant"})})
    with pytest.raises(AuthExpiredError):
        va.token_request({"grant_type": "refresh_token", "refresh_token": "x"}, transport=t)


def test_invalid_client_is_a_config_error_not_an_expired_login(creds: None):
    t = transport({"POST /connect/token": Resp(400, {"error": "invalid_client"})})
    with pytest.raises(ConfigError, match="credentials"):
        va.token_request({"grant_type": "refresh_token", "refresh_token": "x"}, transport=t)


def test_an_unrecognised_token_error_is_a_config_error_not_an_expired_login(creds: None):
    """Branch on `error`, not status: an unknown 400 (bad scope, changed
    request shape) is our bug — naming it beats a re-login that fixes nothing."""
    t = transport({"POST /connect/token": Resp(400, {"error": "invalid_scope"})})
    with pytest.raises(ConfigError, match="invalid_scope"):
        va.token_request({"grant_type": "refresh_token", "refresh_token": "x"}, transport=t)
    t = transport({"POST /connect/token": Resp(401, {"error": "some_future_error"})})
    with pytest.raises(ConfigError, match="some_future_error"):
        va.token_request({"grant_type": "refresh_token", "refresh_token": "x"}, transport=t)


def test_exchange_code_without_refresh_token_fails_loudly(creds: None):
    t = transport({"POST /connect/token": {"access_token": make_jwt(), "expires_in": 3600}})
    with pytest.raises(AuthExpiredError, match="offline_access"):
        va.exchange_code("code-1", transport=t)


def test_code_exchange_body_carries_only_the_three_fields(creds: None):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=token_pair())

    va.exchange_code("code-1", transport=transport({"POST /connect/token": _ok}))
    fields = {p.split("=")[0] for p in seen[0].content.decode().split("&")}
    assert fields == {"code", "redirect_uri", "grant_type"}, "client auth belongs in Basic"
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_missing_credentials_are_a_config_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VIGILO_CLIENT_ID", raising=False)
    monkeypatch.delenv("VIGILO_CLIENT_SECRET", raising=False)

    def _no_secret(cmd: str, what: str) -> str:
        raise ConfigError(f"no {what}")

    monkeypatch.setattr(va, "_secret", _no_secret)
    with pytest.raises(ConfigError):
        va.client_credentials()


# --- Token store: rotation, races, lifetime --------------------------------


def test_store_writes_0600_in_a_0700_dir(store: TokenStore):
    store.save(token_pair())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.load()["refresh_token"] == "refresh-1"


def test_expired_access_token_rotates_and_persists_the_new_refresh(store: TokenStore):
    store.save(token_pair(refresh="old", expires_in=1, obtained_at=time.time() - 999))
    store.transport = transport({"POST /connect/token": REFRESH_OK})
    assert store.access_token() == make_jwt()
    saved = json.loads(store.path.read_text())
    assert saved["refresh_token"] == "rotated", "a rotated refresh token must be persisted"
    assert saved["obtained_at"] == pytest.approx(time.time(), abs=10)


def test_valid_access_token_is_not_refreshed(store: TokenStore):
    store.save(token_pair(refresh="keep-me"))
    calls: list[str] = []

    def _spy(request: httpx.Request) -> httpx.Response:
        calls.append("hit")
        return httpx.Response(200, json={})

    store.transport = transport({"POST /connect/token": _spy})
    assert store.access_token() == make_jwt()
    assert calls == [], "a live token must not be burned on a refresh"
    assert json.loads(store.path.read_text())["refresh_token"] == "keep-me"


def test_a_re_login_landing_mid_refresh_is_not_clobbered(store: TokenStore):
    """The race that loses a refresh token outright, and the re-read that saves it.

    We refresh from an old token; while we are on the wire a re-login writes a
    fresh pair; saving what *we* derived would invalidate the login that just
    happened. The file must win.
    """
    store.save(token_pair(refresh="old", expires_in=1, obtained_at=time.time() - 999))
    ours = make_jwt(sub="ours")
    theirs = make_jwt(sub="theirs")

    def _refresh(request: httpx.Request) -> httpx.Response:
        store.path.write_text(json.dumps({
            "access_token": theirs, "refresh_token": "from-relogin", "expires_in": 3600,
            "obtained_at": time.time(),
        }))
        return httpx.Response(200, json={"access_token": ours,
                                         "refresh_token": "from-our-stale-refresh",
                                         "expires_in": 3600})

    store.transport = transport({"POST /connect/token": _refresh})
    assert store.access_token() == theirs
    saved = json.loads(store.path.read_text())
    assert saved["refresh_token"] == "from-relogin"
    assert saved["access_token"] == theirs


def test_dead_refresh_token_raises_auth_expired(store: TokenStore):
    store.save(token_pair(expires_in=1, obtained_at=time.time() - 999))
    store.transport = transport({"POST /connect/token": Resp(400, {"error": "invalid_grant"})})
    with pytest.raises(AuthExpiredError):
        store.access_token()


def test_missing_token_file_points_at_the_login_command(store: TokenStore):
    with pytest.raises(AuthExpiredError, match="vigilo-login"):
        store.access_token()


def test_a_response_without_expires_in_is_treated_as_expired(store: TokenStore):
    assert store.needs_refresh({"obtained_at": time.time()}) is True
    assert store.needs_refresh({"obtained_at": time.time(), "expires_in": 3600}) is False
    assert store.needs_refresh({"obtained_at": time.time(), "expires_in": 30}) is True


# --- File lock and atomic write --------------------------------------------


def test_lock_is_exclusive_then_released(tmp_path: Path):
    lock = tmp_path / "tokens.json.lock"
    with file_lock(lock):
        assert lock.exists()
        with pytest.raises(VigiloError):
            with file_lock(lock, timeout=0.2):
                pass
    assert not lock.exists()


def test_a_stale_lock_is_broken(tmp_path: Path):
    lock = tmp_path / "tokens.json.lock"
    lock.write_text("crashed")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with file_lock(lock, timeout=1.0, stale_after=60):
        assert lock.exists()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    target = tmp_path / "sub" / "thing.json"
    write_json_0600(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["thing.json"]


# --- Gateway headers and params --------------------------------------------


def test_app_gateway_wants_bearer_plus_appversion_plus_user_id(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"childId": CHILD_ID, "firstName": "Jente"}])

    api = api_factory({"GET /api/children": _ok})
    assert api.children() == [{"childId": CHILD_ID, "firstName": "Jente"}]
    r = seen[0]
    assert r.headers["authorization"].startswith("Bearer ")
    assert r.headers["appVersion"] == app_version()
    assert r.url.params["userId"] == USER_SUB, "userId is a query param on this gateway"


def test_web_gateway_takes_bearer_only(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"id": CHILD_ID}])

    api = api_factory({"GET /api/children/my": _ok})
    api.web_get("/api/children/my")
    r = seen[0]
    assert r.headers["authorization"].startswith("Bearer ")
    assert "appVersion" not in r.headers
    assert "userId" not in r.url.params


def test_thread_list_always_asks_for_after_school_messages(api_factory):
    """The AKS flag. With False an AKS parent gets an empty list and no error."""
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messageThreads": []})

    api = api_factory({"GET /api/message-threads": _ok})
    api.message_threads(CHILD_ID)
    params = seen[0].url.params
    assert params["includeAfterSchoolMessages"] == "true"
    assert params["childIds"] == CHILD_ID
    assert params["unreadMessageThreadsCountOnly"] == "false"


def test_thread_list_unwraps_message_threads_and_survives_a_bare_list(api_factory):
    api = api_factory({"GET /api/message-threads": {"messageThreads": [{"threadUid": "t1"}]}})
    assert api.message_threads(CHILD_ID) == [{"threadUid": "t1"}]
    api2 = api_factory({"GET /api/message-threads": [{"threadUid": "t2"}]})
    assert api2.message_threads(CHILD_ID) == [{"threadUid": "t2"}]


def test_consent_forms_sends_the_singular_child_id(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    api = api_factory({"GET /api/consent-forms": _ok})
    api.consent_forms(CHILD_ID, "unit-1")
    assert seen[0].url.params["childId"] == CHILD_ID
    assert "childIds" not in seen[0].url.params


def test_consent_forms_404_means_no_forms(api_factory):
    # Verified live 2026-10-11: this route answers 404 with an EMPTY body when
    # the child has no forms (a wrong param name gets 404 with an ASP.NET
    # "No HTTP resource was found" body instead).
    api = api_factory({"GET /api/consent-forms": lambda r: httpx.Response(404)})
    assert api.consent_forms(CHILD_ID, "unit-1") == []


def test_check_in_notes_sends_child_person_id_and_the_window(api_factory):
    # Verified live 2026-10-14: /api/check-in-notes wants childPersonId — a
    # third spelling beside childIds (news-feed) and childId (consent-forms).
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"id": "n-1", "noteText": "Hentes av bestemor"}]})

    api = api_factory({"GET /api/check-in-notes": _ok})
    items = api.check_in_notes(CHILD_ID, from_date="2026-10-14", to_date="2027-02-11")
    assert [i["id"] for i in items] == ["n-1"]
    assert seen[0].url.params["childPersonId"] == CHILD_ID
    assert seen[0].url.params["fromDate"] == "2026-10-14"
    assert seen[0].url.params["toDate"] == "2027-02-11"
    assert "childIds" not in seen[0].url.params


def test_check_in_notes_unknown_child_is_empty_not_404(api_factory):
    # Live 2026-10-14: an unknown childPersonId answers 200 with {"items": []}.
    api = api_factory({"GET /api/check-in-notes": {"items": []}})
    assert api.check_in_notes(CHILD_ID, from_date="2026-10-14", to_date="2026-12-31") == []


def test_consent_form_counts_reads_the_app_gateway(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"totalConsentFormsCount": 2,
                                         "unreadConsentFormsCount": 1,
                                         "consentForms": []})

    api = api_factory({"GET /api/consent-forms/counts": _ok})
    counts = api.consent_form_counts()
    assert counts["totalConsentFormsCount"] == 2
    assert counts["unreadConsentFormsCount"] == 1
    # userId injection is the app gateway's fingerprint — the web gateway
    # takes Bearer only.
    assert "userId" in seen[0].url.params


def test_lessons_asks_with_an_iso_week(api_factory):
    seen: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    api = api_factory({"GET /api/students/x/lessons": _ok})
    api.lessons("x", "unit-1", week="2026-42")
    assert seen[0].url.params["week"] == "2026-42"
    assert seen[0].url.params["organizationalUnitId"] == "unit-1"


# --- Failure handling -------------------------------------------------------


def test_401_forces_exactly_one_refresh_then_succeeds(api_factory, store: TokenStore,
                                                     monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    hits = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(401, json={"message": "Authorization has been denied"})
        return httpx.Response(200, json=[])

    api = api_factory({"GET /api/children": _flaky})
    assert api.children() == []
    assert hits["n"] == 2
    assert json.loads(store.path.read_text())["refresh_token"] == "rotated"


def test_a_second_401_is_terminal(api_factory, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    api = api_factory({"GET /api/children": Resp(401, {"message": "denied"})})
    with pytest.raises(AuthExpiredError, match="vigilo-login"):
        api.children()


def test_server_errors_are_retried_with_growing_backoff(api_factory,
                                                        monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    calls: list[str] = []

    def _down(request: httpx.Request) -> httpx.Response:
        calls.append("try")
        return httpx.Response(503, text="down")

    api = api_factory({"GET /api/children": _down})
    with pytest.raises(ApiError, match="503"):
        api.children()
    assert len(calls) == va.MAX_ATTEMPTS
    assert len(slept) == va.MAX_ATTEMPTS - 1
    assert slept == sorted(slept) and slept[0] < slept[-1]


def test_429_is_retried(api_factory, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    hits = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=[])

    api = api_factory({"GET /api/children": _flaky})
    assert api.children() == []


def test_non_json_body_is_reported_not_crashed(api_factory):
    def _html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>",
                              headers={"content-type": "text/html"})

    api = api_factory({"GET /api/children": _html})
    with pytest.raises(ApiError, match="non-JSON"):
        api.children()


def test_probe_shows_a_refusal_instead_of_raising(api_factory):
    """Diagnostics have to be able to look at a 401, not be rescued from it."""
    api = api_factory({"GET /api/children/my": Resp(401, {"message": "denied"})})
    assert api.probe("/api/children/my", web=True).status_code == 401


def test_describe_children_states_the_web_gateway_verdict(api_factory):
    api = api_factory({
        "GET /api/children": [{"childId": CHILD_ID, "firstName": "Jente", "lastName": "Prøve"}],
        "GET /api/children/my": Resp(401, {"message": "denied"}),
    })
    out = va.api_describe_children(api)
    assert f"childId={CHILD_ID}" in out
    assert "does NOT work here" in out


def test_describe_children_confirms_one_token_for_both(api_factory):
    api = api_factory({
        "GET /api/children": [{"childId": CHILD_ID, "firstName": "Jente"}],
        "GET /api/children/my": [{"id": CHILD_ID, "organizationalUnits": [
            {"id": "unit-1", "name": "Enhet 1", "groups": [{"name": "Gruppe A"}]}]}],
    })
    out = va.api_describe_children(api)
    assert "one token covers both gateways" in out
    assert "unit id=unit-1" in out and "Gruppe A" in out


# --- Small utilities --------------------------------------------------------


def test_jwt_sub_reads_the_claim_without_verifying():
    assert jwt_sub(make_jwt("sub-value")) == "sub-value"


def test_jwt_sub_without_a_sub_is_an_error():
    with pytest.raises(ApiError, match="sub"):
        jwt_sub(make_jwt(""))


def test_iso_week_is_zero_padded():
    assert iso_week(date(2026, 1, 4)) == "2026-01"
    assert iso_week(date(2026, 10, 12)) == "2026-42"


# --- Attachments ------------------------------------------------------------


def test_message_detail_shape_is_the_recorded_one(api_factory):
    """Pins the message/attachment field names the sync and reader read."""
    uid = "thread-uid-1"
    api = api_factory({f"GET /api/messages/threads/{uid}": {
        "messages": [{
            "body": "Husk å svare på samtykket",
            "timeCreated": "2026-10-09T07:30:00Z",
            "sender": {"firstName": "AKS", "lastName": "Eksempelby"},
            "attachments": [{"name": "skjema.pdf", "mimeType": "application/pdf",
                             "url": "https://blob.example/sas"}],
        }]}})
    msg = api.thread(uid)["messages"][0]
    assert person_name(msg["sender"]) == "AKS Eksempelby"
    assert msg["attachments"][0]["name"] == "skjema.pdf"


def test_sas_download_sends_no_auth_headers(monkeypatch: pytest.MonkeyPatch):
    """SAS URLs are pre-signed; adding an Authorization header to them is a bug."""
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def _blob(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.4")

    def _fake_client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport({"GET /sas": _blob})
        return real_client(**kwargs)

    monkeypatch.setattr(va.httpx, "Client", _fake_client)
    assert VigiloApi.download_attachment("https://blob.example/sas") == b"%PDF-1.4"
    assert "authorization" not in seen[0].headers
