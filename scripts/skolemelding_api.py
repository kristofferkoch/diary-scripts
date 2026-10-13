#!/usr/bin/env python3
"""Read-only client for Skolemelding (Skoleplattform Oslo)'s unofficial parent API.

Skolemelding has no public API. Everything below is the API of its *own*
parent app, mapped by the upstream project
`kristoffervp/skolemelding-mcp <https://github.com/kristoffervp/skolemelding-mcp>`_
(0BSD) — the auth chain, endpoint shapes and search-body fields here follow
its ``src/mobile-auth.js`` / ``src/mobile-client.js``.

Read-only by design: the only non-GET requests are the token exchange, the
refresh, and the message *search* (which the API happens to spell as a POST).
No reply, no read-receipt, no absence POST.

The auth chain is longer than Vigilo's, but the client is public (no secret,
PKCE instead):

1. ID-porten discovery (``.well-known/openid-configuration``), issuer-checked.
2. PKCE authorize → the registered redirect is the custom scheme
   ``com.cgi.oslomelding.foresatte://oauth`` — no browser can follow it, which
   is what makes the paste-capture login work (see scripts/skolemelding_login.py).
3. Code → OIDC access token (ID-porten token endpoint).
4. OIDC token → ``authCode`` (portal ``/api/exchange-app-token``).
5. ``authCode`` → app token pair (portal ``/token``, ``grant_type=password``
   with the literal password ``code`` — it is a grant hand-off, not a secret).
6. ``/api/deviceinfo/save`` once, as the real app does.

Refresh is pure HTTP (``grant_type=refresh_token`` on the portal ``/token``);
HTTP 400/401 there means the login is dead — re-run ``skolemelding-login``.

Also used as a CLI, the instrument for probing the message API::

    uv run skolemelding-api list
    uv run skolemelding-api show 12345678
    uv run skolemelding-api attachment /globalassets/attachments/…/vedlegg.pdf
    uv run skolemelding-api probe /api/messagesearchmobile/
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import platform
import secrets
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from mail_reader.config import cfg, cfg_path

# --- Constants ---------------------------------------------------------------

PORTAL_ORIGIN = "https://portal.skoleplattform.no"
IDPORTEN_DISCOVERY = "https://idporten.no/.well-known/openid-configuration"
IDPORTEN_ISSUER = "https://idporten.no"
# The parent app's public ID-porten client (no secret — PKCE only).
OIDC_CLIENT_ID = "d12dad8a-f092-4a96-8a37-371590825d22"
OIDC_SCOPE = "openid profile"
# Registered redirect of the mobile app. Custom scheme: no browser can follow
# it, which is what makes the paste-capture login work.
REDIRECT_URI = "com.cgi.oslomelding.foresatte://oauth"
# Client id the portal's own /token endpoint expects, and the literal password
# of the authCode grant — both are fixed strings from the app, not secrets.
TOKEN_CLIENT_ID = "consoleAPP"
AUTHCODE_PASSWORD = "code"

# Version string the portal's /token and /api/deviceinfo/save see. Tracks a
# shipping release of the app; [skolemelding].app_version overrides without a
# code change.
DEFAULT_APP_VERSION = "6.0.6"

# Refresh the access token this many seconds before it expires. Lifetime comes
# from the token response (expires_in), never from a hardcoded assumption.
EXPIRY_MARGIN = 60

# Fallback access-token lifetime when the response carries no expires_in —
# the same default the upstream client uses.
DEFAULT_TOKEN_LIFETIME = 28 * 60

# Attachment ceiling (upstream uses the same): bigger than this is never a
# school note.
ATTACHMENT_MAX_BYTES = 10_000_000

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5
BACKOFF_CAP = 30.0

# The search body, verbatim from the app. SearchTerm/OnBehalfOf stay empty and
# every Filter* stays off — this is "the inbox, newest first".
SEARCH_PATH = "/api/messagesearchmobile/"


def _search_body(page: int, page_size: int) -> dict[str, Any]:
    return {
        "Page": page,
        "PageSize": page_size,
        "SearchTerm": "",
        "SearchInbox": True,
        "FilterSender": None,
        "FilterUnreadMessages": False,
        "FilterFeedbackRequired": False,
        "FilterFeedbackIsQuestion": False,
        "FilterIsAbsentMessage": False,
        "OnBehalfOf": "",
        "AuthorUserCategory": None,
        "FilterFavouriteMessages": False,
        "SendHiddenMessages": False,
    }


# --- Errors ------------------------------------------------------------------


class SkolemeldingError(RuntimeError):
    """Base for everything this module raises."""


class AuthExpiredError(SkolemeldingError):
    """The login is gone (no tokens, expired refresh, revoked).

    Callers print ``!! skolemelding auth expired — run skolemelding-login`` —
    a dead login must never break a sjekk run.
    """


class ApiError(SkolemeldingError):
    """The API answered with something we could not use."""


# --- Config accessors --------------------------------------------------------


def token_path() -> Path:
    """Where the persisted login lives. Outside the repo, 0600, always.

    ``$SKOLEMELDING_TOKEN_FILE`` > ``[skolemelding].token_file`` >
    ``~/.config/diary/skolemelding-auth.json``.
    """
    env = os.environ.get("SKOLEMELDING_TOKEN_FILE")
    if env:
        return Path(env).expanduser()
    return cfg_path(
        "skolemelding.token_file",
        Path("~/.config/diary/skolemelding-auth.json").expanduser(),
    )


def app_version() -> str:
    return str(cfg("skolemelding.app_version", DEFAULT_APP_VERSION))


# --- PKCE + authorize URL ----------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """A fresh (verifier, S256 challenge) pair — 32 random bytes, base64url.

    >>> v, c = pkce_pair()
    >>> len(v), len(c)
    (43, 43)
    >>> base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=") == c
    True
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def authorize_url(authorization_endpoint: str, state: str, code_challenge: str) -> str:
    """Build the ID-porten authorization URL for the parent app's public client.

    ``prompt=login`` forces a fresh ID-porten login instead of silently
    reusing a browser session; ``ui_locales=nb`` is what the app asks for.
    ``quote_via=quote`` keeps the scope separator as ``%20``.
    """
    params = {
        "response_type": "code",
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": OIDC_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "ui_locales": "nb",
        "prompt": "login",
    }
    return f"{authorization_endpoint}?{urlencode(params, quote_via=quote)}"


def code_from_redirect(target: str, expected_state: str | None = None) -> str:
    """Pull the authorization code out of a captured redirect URL.

    Accepts the whole failed-redirect URL from the address bar or a bare
    ``code`` value. ``urlsplit`` handles the custom scheme fine.

    >>> code_from_redirect("com.cgi.oslomelding.foresatte://oauth?code=ABC&state=XY", "XY")
    'ABC'
    >>> code_from_redirect("ABC123")
    'ABC123'
    >>> code_from_redirect("com.cgi.oslomelding.foresatte://oauth?state=other", "XY")
    Traceback (most recent call last):
        ...
    ValueError: state in the redirect does not match — stale capture
    >>> code_from_redirect("com.cgi.oslomelding.foresatte://oauth?code=ABC", "XY")
    Traceback (most recent call last):
        ...
    ValueError: state in the redirect does not match — stale capture
    """
    target = target.strip()
    if "://" not in target and "?" not in target and "=" not in target:
        return target  # a bare code, pasted by hand
    params = parse_qs(urlsplit(target).query)
    state = (params.get("state") or [None])[0]
    # A missing state is a mismatch too when we issued one — the redirect must
    # prove it answers *our* authorize request.
    if expected_state and state != expected_state:
        raise ValueError("state in the redirect does not match — stale capture")
    code = (params.get("code") or [None])[0]
    if not code:
        raise ValueError(f"no code= in {target[:120]!r}")
    return code


# --- HTTP plumbing for the auth chain ----------------------------------------


def idporten_metadata(
    *, transport: httpx.BaseTransport | None = None, timeout: float = 30.0
) -> dict[str, Any]:
    """Fetch ID-porten's discovery document and pin the issuer."""
    with httpx.Client(transport=transport, timeout=timeout) as client:
        r = client.get(IDPORTEN_DISCOVERY)
    if r.status_code >= 400:
        raise ApiError(f"HTTP {r.status_code} on {IDPORTEN_DISCOVERY}")
    try:
        data = r.json()
    except ValueError as e:
        raise ApiError(f"non-JSON discovery document from {IDPORTEN_DISCOVERY}") from e
    if data.get("issuer") != IDPORTEN_ISSUER:
        raise ApiError(
            f"unexpected ID-porten issuer {data.get('issuer')!r} — expected {IDPORTEN_ISSUER!r}"
        )
    return data


def token_request(
    url: str,
    data: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Form POST to a token endpoint (ID-porten's or the portal's).

    Public client: no Basic auth, no secret anywhere — the PKCE verifier is
    the proof. HTTP 400/401 on any of these grants means the grant is dead
    (used code, expired refresh) and the remedy is always the same:
    re-run ``skolemelding-login``.
    """
    with httpx.Client(transport=transport, timeout=timeout) as client:
        r = client.post(url, data=dict(data), headers={"Accept": "application/json"})
    error = ""
    with contextlib.suppress(ValueError):
        error = str(r.json().get("error", ""))
    if r.status_code >= 400:
        detail = f"HTTP {r.status_code} {error or r.text[:200]}"
        if r.status_code in (400, 401):
            raise AuthExpiredError(
                f"token request refused: {detail} — re-run skolemelding-login"
            )
        raise ApiError(f"token request failed: {detail}")
    payload: dict[str, Any] = r.json()
    return payload


def _json_post(
    url: str,
    payload: Mapping[str, Any],
    *,
    bearer: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        r = client.post(url, json=dict(payload), headers=headers)
    if r.status_code >= 400:
        raise ApiError(f"HTTP {r.status_code} on {url}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise ApiError(f"non-JSON response from {url}: {r.text[:120]}") from e
    return data if isinstance(data, dict) else {}


# --- The login chain ---------------------------------------------------------


def exchange_idporten_code(
    token_endpoint: str,
    code: str,
    code_verifier: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Step 3: trade the one-time authorization code for an OIDC token."""
    tokens = token_request(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": OIDC_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        transport=transport,
    )
    if not tokens.get("access_token"):
        raise AuthExpiredError(
            "ID-porten returned no access_token — start the login over"
        )
    return tokens


def exchange_app_token(
    oidc_access_token: str, *, transport: httpx.BaseTransport | None = None
) -> str:
    """Step 4: trade the OIDC token for the portal's one-time ``authCode``."""
    data = _json_post(
        f"{PORTAL_ORIGIN}/api/exchange-app-token",
        {"accessToken": oidc_access_token, "provider": "idporten"},
        transport=transport,
    )
    auth_code = data.get("authCode")
    if not auth_code:
        raise ApiError("the portal did not accept the ID-porten login (no authCode)")
    return str(auth_code)


def mobile_token(
    auth_code: str, device_id: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Step 5: trade the ``authCode`` for the renewable app token pair.

    This is ``grant_type=password`` only in spelling — the "username" is the
    one-time authCode and the "password" is the literal string ``code``. The
    scope is the device's identity: ``<deviceId>|<unix timestamp>|Foresatte``.
    """
    scope = f"{device_id}|{int(time.time())}|Foresatte"
    tokens = token_request(
        f"{PORTAL_ORIGIN}/token",
        {
            "grant_type": "password",
            "username": auth_code,
            "password": AUTHCODE_PASSWORD,
            "client_id": TOKEN_CLIENT_ID,
            "app_version": app_version(),
            "scope": scope,
        },
        transport=transport,
    )
    for key in ("access_token", "refresh_token", "userName"):
        if not tokens.get(key):
            raise AuthExpiredError(
                f"the portal returned no {key} — start the login over"
            )
    return tokens


def save_device_info(
    access_token: str, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Step 6: register the "device", as the real app does once per login."""
    os_version = f"{platform.system().lower()}|{platform.release()}"
    _json_post(
        f"{PORTAL_ORIGIN}/api/deviceinfo/save",
        {"OsVersion": os_version, "AppVersion": app_version()},
        bearer=access_token,
        transport=transport,
    )


def token_lifetime(expires_in: Any) -> float:
    """Seconds the access token lives: from the response, floored at 60,
    defaulting to :data:`DEFAULT_TOKEN_LIFETIME` when absent.

    >>> token_lifetime(100), token_lifetime(None), token_lifetime(5)
    (100.0, 1680.0, 60.0)
    """
    try:
        seconds = float(expires_in or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        seconds = float(DEFAULT_TOKEN_LIFETIME)
    return max(60.0, seconds)


def login_from_code(
    code: str,
    code_verifier: str,
    token_endpoint: str,
    *,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Steps 3–6 in one go: pasted code → persisted token-file payload.

    The persisted shape is the upstream client's — camelCase keys, plus
    ``expiresAt`` as a **seconds** epoch (theirs is ms; everything else in
    this repo thinks in seconds).
    """
    oidc = exchange_idporten_code(
        token_endpoint, code, code_verifier, transport=transport
    )
    auth_code = exchange_app_token(str(oidc["access_token"]), transport=transport)
    device_id = str(uuid.uuid4())
    tokens = mobile_token(auth_code, device_id, transport=transport)
    save_device_info(str(tokens["access_token"]), transport=transport)
    return {
        "accessToken": tokens["access_token"],
        "refreshToken": tokens["refresh_token"],
        "userName": tokens["userName"],
        "deviceId": device_id,
        "expiresAt": now() + token_lifetime(tokens.get("expires_in")),
    }


def refresh_tokens(
    refresh_token: str, user_name: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Pure-HTTP renewal. HTTP 400/401 here means the login is dead."""
    return token_request(
        f"{PORTAL_ORIGIN}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": TOKEN_CLIENT_ID,
            "app_version": app_version(),
            "username": user_name,
        },
        transport=transport,
    )


# --- File lock ---------------------------------------------------------------


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 90.0, stale_after: float = 90.0) -> Iterator[None]:
    """``O_CREAT|O_EXCL`` lock — works across processes, not just threads.

    Serialises load → refresh → save on the token file. A crashed holder
    leaves a lock behind, so a lock older than ``stale_after`` is broken.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            if time.monotonic() >= deadline:
                raise SkolemeldingError(
                    f"timed out waiting for lock {path} — another skolemelding process?"
                )
            time.sleep(0.2)
            continue
        os.close(fd)
        break
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# --- Token store -------------------------------------------------------------


def write_json_0600(path: Path, payload: Any) -> None:
    """Write JSON atomically with 0600 file / 0700 dir permissions.

    ``mkstemp`` rather than a fixed ``.tmp`` name: two writers on a fixed name
    rename each other's file away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class TokenStore:
    """Persisted login with automatic, race-safe renewal.

    The whole load → refresh → save sequence runs under a file lock, and the
    file is re-read after the network round trip: if something else (a
    re-login, another reader) rotated the token while we were in flight, its
    value is strictly newer than what we derived from the old refresh token,
    so we keep theirs instead of clobbering it.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = path or token_path()
        self.transport = transport
        self._now = now

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise AuthExpiredError(
                f"no tokens at {self.path} — run skolemelding-login first"
            )
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AuthExpiredError(f"could not read {self.path}: {e}") from e
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise AuthExpiredError(
                f"{self.path} holds no accessToken — run skolemelding-login"
            )
        return data

    def save(self, tokens: dict[str, Any]) -> None:
        write_json_0600(self.path, tokens)

    def needs_refresh(self, tokens: Mapping[str, Any], *, force: bool = False) -> bool:
        """True when the access token is used up or within :data:`EXPIRY_MARGIN`.

        >>> store = TokenStore(path=Path("/nonexistent/skolemelding-auth.json"))
        >>> t = {"expiresAt": 2000.0}
        >>> store._now = lambda: 1930.0     # 70 s left, outside the 60 s margin
        >>> store.needs_refresh(t)
        False
        >>> store._now = lambda: 1950.0     # inside the 60 s margin
        >>> store.needs_refresh(t)
        True
        >>> store.needs_refresh(t, force=True)
        True
        """
        if force:
            return True
        expires_at = float(tokens.get("expiresAt") or 0.0)
        if not expires_at:
            return True
        return self._now() > expires_at - EXPIRY_MARGIN

    def access_token(self, *, force_refresh: bool = False) -> str:
        with file_lock(self.lock_path):
            tokens = self.load()
            if not self.needs_refresh(tokens, force=force_refresh):
                return str(tokens["accessToken"])
            old_refresh = tokens.get("refreshToken")
            if not old_refresh:
                raise AuthExpiredError("no refreshToken — run skolemelding-login again")
            user_name = str(tokens.get("userName") or "")
            if not user_name:
                raise AuthExpiredError(
                    "no userName in the token file — run skolemelding-login again"
                )
            try:
                renewed = refresh_tokens(
                    str(old_refresh), user_name, transport=self.transport
                )
            except AuthExpiredError as e:
                raise AuthExpiredError(
                    f"refresh token is no longer valid: {e}"
                ) from e
            if not renewed.get("access_token"):
                raise AuthExpiredError(
                    "refresh response had no access_token — nothing saved; "
                    "run skolemelding-login again"
                )
            # Re-read under the lock: a re-login may have landed mid-flight.
            with contextlib.suppress(AuthExpiredError):
                current = self.load()
                if current.get("refreshToken") not in (None, "", old_refresh):
                    print(
                        "skolemelding: newer tokens on disk than ours — keeping the file",
                        file=sys.stderr,
                    )
                    return str(current["accessToken"])
            merged = dict(tokens)
            merged["accessToken"] = renewed["access_token"]
            # Rotate only when a new one is returned — the portal does not
            # always send one.
            if renewed.get("refresh_token"):
                merged["refreshToken"] = renewed["refresh_token"]
            if renewed.get("userName"):
                merged["userName"] = renewed["userName"]
            merged["expiresAt"] = self._now() + token_lifetime(renewed.get("expires_in"))
            self.save(merged)
            return str(merged["accessToken"])


# --- The client --------------------------------------------------------------


class SkolemeldingApi:
    """httpx client for the portal. Read-only: GET plus the search POST."""

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.store = store or TokenStore(transport=transport)
        self._transport = transport
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=PORTAL_ORIGIN, transport=transport, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SkolemeldingApi":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- plumbing --

    def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.store.access_token(force_refresh=force_refresh)}"
        }

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        raw = r.headers.get("retry-after", "")
        try:
            return min(float(raw), BACKOFF_CAP)
        except ValueError:
            return None

    def _request(
        self, method: str, path: str, json_body: Mapping[str, Any] | None = None
    ) -> httpx.Response:
        """One request with a forced-refresh retry on 401 and backoff on 429/5xx."""
        r: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            r = self._client.request(
                method, path, json=dict(json_body) if json_body is not None else None,
                headers=self._headers(),
            )
            if r.status_code == 401:
                # Once only: a second 401 after a forced refresh is terminal.
                r = self._client.request(
                    method, path,
                    json=dict(json_body) if json_body is not None else None,
                    headers=self._headers(force_refresh=True),
                )
                if r.status_code == 401:
                    raise AuthExpiredError(
                        "401 even after refreshing the token — run skolemelding-login again"
                    )
                return r
            if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                delay = self._retry_after(r) or min(
                    BACKOFF_BASE * (2**attempt), BACKOFF_CAP
                )
                print(
                    f"skolemelding: HTTP {r.status_code} on {path} — waiting {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            return r
        assert r is not None  # MAX_ATTEMPTS >= 1
        return r

    def probe(self, path: str) -> httpx.Response:
        """Exactly one authenticated GET — no 401 retry, no exception, no backoff.

        For diagnostics only. A 401 from here is *information*, not a state to
        react to.
        """
        return self._client.get(path, headers=self._headers())

    def _json(self, r: httpx.Response, path: str) -> Any:
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ApiError(f"non-JSON response from {path}: {r.text[:120]}") from e

    # -- messages --

    def search_messages(self, *, page: int = 1, page_size: int = 15) -> dict[str, Any]:
        """One page of the inbox, newest first: ``{"Result": [...], "TotalMatching": n}``.

        Message fields: ``Id`` (numeric), ``Header``, ``From``, ``To`` /
        ``Receiver``, ``Timestamp``, ``IsMessageSeen``, ``Body`` (HTML),
        ``Attachments`` (list of portal-relative paths), ``SearchText``,
        ``Threads``.
        """
        data = self._json(
            self._request("POST", SEARCH_PATH, json_body=_search_body(page, page_size)),
            SEARCH_PATH,
        )
        result = data.get("Result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            raise ApiError("unexpected messagesearchmobile shape (no Result list)")
        return data

    def find_message(
        self, message_id: Any, *, page_size: int = 50, max_pages: int = 20
    ) -> dict[str, Any]:
        """One full message by ``Id``. The search response already carries the
        full record (Body included) — there is no separate detail endpoint, so
        this pages the inbox until the Id shows up."""
        wanted = str(message_id)
        for page in range(1, max_pages + 1):
            data = self.search_messages(page=page, page_size=page_size)
            for message in data["Result"]:
                if str(message.get("Id")) == wanted:
                    return message
            total = int(data.get("TotalMatching") or 0)
            if page * page_size >= total:
                break
        raise ApiError(
            f"message Id={wanted} not found in the first {max_pages} pages of the inbox"
        )

    def attachment(
        self, path: str, *, max_bytes: int = ATTACHMENT_MAX_BYTES
    ) -> tuple[bytes, str]:
        """Download one attachment by its portal-relative path
        (``/globalassets/attachments/…``), Bearer-authed. Returns (bytes, mime).

        An HTML answer means the token died and the portal handed us a login
        page instead of the file — that is an auth failure, not a document.
        The size cap is enforced while streaming: the content-length pre-check
        is only an early exit, and a missing or malformed one just defers to
        the byte counter (the download aborts mid-body past ``max_bytes``).
        """
        for attempt in range(MAX_ATTEMPTS):
            with self._client.stream("GET", path, headers=self._headers()) as r:
                if r.status_code == 401:
                    # Once only: a second 401 after a forced refresh is terminal.
                    with self._client.stream(
                        "GET", path, headers=self._headers(force_refresh=True)
                    ) as r2:
                        if r2.status_code == 401:
                            raise AuthExpiredError(
                                "401 even after refreshing the token — "
                                "run skolemelding-login again"
                            )
                        return self._read_attachment(r2, path, max_bytes)
                if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                    delay = self._retry_after(r) or min(
                        BACKOFF_BASE * (2**attempt), BACKOFF_CAP
                    )
                    print(
                        f"skolemelding: HTTP {r.status_code} on {path} — "
                        f"waiting {delay:.0f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                return self._read_attachment(r, path, max_bytes)
        raise AssertionError("unreachable")  # MAX_ATTEMPTS >= 1

    @staticmethod
    def _read_attachment(
        r: httpx.Response, path: str, max_bytes: int
    ) -> tuple[bytes, str]:
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code} on attachment {path}")
        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        mime = mime or "application/octet-stream"
        if mime == "text/html":
            raise AuthExpiredError(
                "attachment answered an HTML login page — the token is dead; "
                "run skolemelding-login again"
            )
        declared = r.headers.get("content-length")
        if declared:
            # Early exit only; an unparseable value means "unknown", and the
            # streaming cap below stays the real guard.
            with contextlib.suppress(ValueError):
                if int(declared) > max_bytes:
                    raise ApiError(
                        f"attachment {path} exceeds the {max_bytes}-byte limit"
                    )
        chunks: list[bytes] = []
        size = 0
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ApiError(f"attachment {path} exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
        return b"".join(chunks), mime


# --- Debug CLI ---------------------------------------------------------------


def _print_page(data: Mapping[str, Any], page: int) -> None:
    for message in data["Result"]:
        seen = "seen" if message.get("IsMessageSeen") else "UNREAD"
        print(
            f"{str(message.get('Id')):>10}  {message.get('Timestamp', '')}  "
            f"{seen:6}  {message.get('From', '')} — {message.get('Header', '')}"
        )
    print(f"(page {page}, {data.get('TotalMatching')} total)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="skolemelding-api",
        description="Read-only probe of the Skolemelding (Skoleplattform Oslo) parent API.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent message headers")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=15)

    p_show = sub.add_parser("show", help="dump one full message by Id (JSON)")
    p_show.add_argument("id", help="the numeric message Id from `list`")
    p_show.add_argument("--page-size", type=int, default=50,
                        help="page size while scanning for the Id")

    p_att = sub.add_parser("attachment", help="download one attachment by its path")
    p_att.add_argument("path", help="portal-relative path, /globalassets/attachments/…")
    p_att.add_argument("-o", "--output", default=None,
                       help="output file (default: the path's basename)")

    p_probe = sub.add_parser("probe", help="raw authenticated GET; prints status + body")
    p_probe.add_argument("path")
    p_probe.add_argument("--max-chars", type=int, default=20000)

    args = ap.parse_args(argv)

    try:
        with SkolemeldingApi() as api:
            if args.cmd == "list":
                _print_page(api.search_messages(page=args.page, page_size=args.page_size),
                            args.page)
            elif args.cmd == "show":
                message = api.find_message(args.id, page_size=args.page_size)
                print(json.dumps(message, indent=2, ensure_ascii=False))
            elif args.cmd == "attachment":
                content, mime = api.attachment(args.path)
                out = Path(args.output or Path(urlsplit(args.path).path).name or "attachment.bin")
                out.write_bytes(content)
                print(f"{len(content)} bytes, {mime} → {out}", file=sys.stderr)
            elif args.cmd == "probe":
                r = api.probe(args.path)
                print(f"HTTP {r.status_code} {r.request.url}")
                body = r.text
                if args.max_chars and len(body) > args.max_chars:
                    body = body[: args.max_chars] + f"\n…[truncated at {args.max_chars} chars]"
                with contextlib.suppress(ValueError):
                    body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                    if args.max_chars and len(body) > args.max_chars:
                        body = body[: args.max_chars] + f"\n…[truncated at {args.max_chars} chars]"
                print(body)
                return 0 if r.status_code < 400 else 1
    except SkolemeldingError as e:
        print(f"!! skolemelding: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
