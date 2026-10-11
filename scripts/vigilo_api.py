#!/usr/bin/env python3
"""Read-only client for Vigilo's unofficial parent API (skole/SFO/AKS).

Vigilo has no public API. Everything below is the API of Vigilo's *own*
parent applications, reverse-engineered by two upstream projects:

* `fredrsat/vigilo-connector <https://github.com/fredrsat/vigilo-connector>`_
  (MIT) — auth flow, endpoint inventory, header quirks. ``config``/
  ``token_request``/``VigiloApi`` here are adaptations of its ``config.py``,
  ``auth.py`` and ``client.py``, as that license allows.
* `brujoand/vigilo2smtp <https://github.com/brujoand/vigilo2smtp>`_ ships no
  license: **behavioural findings only, no code copied** — the exact
  authorize-request shape (no PKCE, ``%20``-encoded scope), the rotating
  refresh token, the re-read-before-write rule and the ~6 h SAS attachments.

Read-only by design: the only non-GET request in this module is the OAuth
token exchange. No reply, no read-receipt, no absence POST — answering AKS
stays a human act in the app.

Two gateways, one token (verified live; see PLAN-VIGILO.md Phase B2):

``API_BASE``      app gateway — Bearer **plus** a literal ``appVersion``
                  header **plus** ``userId=<jwt sub>`` as a query param.
``WEB_API_BASE``  web parent portal — Bearer only, no appVersion/userId.

Quirks that bite (all verified against the upstream repos, don't "fix"):

* **No PKCE.** This is a confidential client (Basic auth with a secret);
  sending ``code_challenge`` makes the token exchange fail ``invalid_grant``.
* **Refresh tokens rotate on every use.** Two refreshes with the same token
  burn the good one and force a re-login — hence the file lock around
  load → refresh → save, and the re-read under the lock.
* **Defensive shape parsing.** List endpoints answer with a bare array *or*
  ``{"items": [...]}`` (the app thread list with ``{"messageThreads": [...]}``);
  ``organizationalUnit`` is a dict-with-``name`` *or* a plain string.
* **SAS attachment URLs live ~6 h** — fetch the thread detail immediately
  before downloading, and never persist the URL.

Also used as a CLI, the instrument for probing still-unmapped endpoints::

    uv run vigilo-api /api/children
    uv run vigilo-api --web /api/consent-forms '{"childId":"…","organizationalUnitId":"…"}'
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from mail_reader.config import cfg, cfg_path

# --- Constants ---------------------------------------------------------------

AUTH_BASE = "https://auth.prod.vigilo-oas.no"
API_BASE = "https://api-gw-parent-app.prod.vigilo-oas.no"
WEB_API_BASE = "https://web-parent.prod.vigilo-oas.no"
SCOPE = "openid vigiloprofile offline_access"
# Registered redirect of the Android client. Custom scheme: no browser can
# follow it, which is what makes the paste-capture login work (see
# scripts/vigilo_login.py). Override with $VIGILO_REDIRECT_URI if Vigilo ever
# registers an https one — then the automatic callback flow becomes possible.
REDIRECT_URI = os.environ.get("VIGILO_REDIRECT_URI", "app://ch-parent-android.vigilo.no")

# Literal ``appVersion`` header the app gateway expects. The APK sends
# "Android " + versionName, so this tracks a shipping release: 3.3.0-4 is the
# current one (read out of its classes*.dex; PLAN-VIGILO.md Phase A). The
# gateway is known to reject requests without the header; whether it checks the
# value is unknown, so [vigilo].app_version overrides without a code change.
DEFAULT_APP_VERSION = "Android 3.3.0-4"

# Refresh the access token this many seconds before it expires. Never
# hardcode the access-token lifetime — read ``expires_in`` from the response.
EXPIRY_MARGIN = 60

# Effective floor between two live polls of the app gateway. Vigilo is a
# third-party production service and neither upstream repo throttles at all;
# we do (PLAN-VIGILO.md "Scope notes & rules").
POLL_FLOOR_SECONDS = 300

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5
BACKOFF_CAP = 30.0

DEFAULT_PASSWORD_CMD_ID = "pass show vigilo/client-id"
DEFAULT_PASSWORD_CMD_SECRET = "pass show vigilo/client-secret"


# --- Errors ------------------------------------------------------------------


class VigiloError(RuntimeError):
    """Base for everything this module raises."""


class ConfigError(VigiloError):
    """Our side is broken: missing/bad client credentials, bad config.

    Never retried and never reported as "auth expired" — the token endpoint
    answers 400 for a bad secret *and* an expired grant, so the two must not
    be conflated (branch on the ``error`` field, not the status).
    """


class AuthExpiredError(VigiloError):
    """The refresh token is gone (expired / rotated away / revoked).

    Callers print ``!! vigilo auth expired — run vigilo-login``, set
    ``auth_required`` in state and **exit 0** — a dead login must never break
    a sjekk run.
    """


class ApiError(VigiloError):
    """The API answered with something we could not use."""


# --- Config accessors --------------------------------------------------------


def token_path() -> Path:
    """Where the OAuth token pair lives. Outside the repo, 0600, always.

    ``$VIGILO_TOKEN_FILE`` > ``[vigilo].token_file`` >
    ``~/.config/diary/vigilo-tokens.json``.
    """
    env = os.environ.get("VIGILO_TOKEN_FILE")
    if env:
        return Path(env).expanduser()
    return cfg_path(
        "vigilo.token_file", Path("~/.config/diary/vigilo-tokens.json").expanduser()
    )


def app_version() -> str:
    return str(cfg("vigilo.app_version", DEFAULT_APP_VERSION))


def _secret(cmd: str, what: str) -> str:
    """Run a ``pass`` command and return its stdout (like spond_sync does)."""
    try:
        proc = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise ConfigError(f"could not read {what} ({cmd!r}): {e}") from e
    value = proc.stdout.strip()
    if not value:
        raise ConfigError(f"{what} command produced empty output: {cmd!r}")
    return value


def client_credentials() -> tuple[str, str]:
    """The OAuth client id/secret — Vigilo's own Android-app values.

    Env wins, then the ``pass`` entries (``vigilo/client-id``,
    ``vigilo/client-secret``), overridable through
    ``[vigilo].client_id_cmd`` / ``[vigilo].client_secret_cmd``. The secret is
    obfuscation-from-a-public-APK rather than a real credential, but it still
    never goes in a repo (diary-scripts is public).
    """
    client_id = os.environ.get("VIGILO_CLIENT_ID", "")
    client_secret = os.environ.get("VIGILO_CLIENT_SECRET", "")
    if client_id and client_secret:
        return client_id, client_secret
    client_id = client_id or _secret(
        str(cfg("vigilo.client_id_cmd", DEFAULT_PASSWORD_CMD_ID)), "vigilo client_id"
    )
    client_secret = client_secret or _secret(
        str(cfg("vigilo.client_secret_cmd", DEFAULT_PASSWORD_CMD_SECRET)),
        "vigilo client_secret",
    )
    return client_id, client_secret


# --- Authorize URL -----------------------------------------------------------


def authorize_url(
    state: str,
    *,
    client_id: str | None = None,
    redirect_uri: str = REDIRECT_URI,
    scope: str = SCOPE,
    prompt: str | None = "login",
) -> str:
    """Build the authorization URL. **No PKCE** — see the module docstring.

    ``quote_via=quote`` is load-bearing: the default encoder turns the scope's
    space into ``+``, and this client only accepts the ``%20`` form,
    byte-for-byte. ``prompt=login`` forces a fresh ID-porten login instead of
    silently reusing a browser session, ``display=touch`` is what the app asks
    for (and what the working upstream flow sends). Pass ``prompt=None`` when
    replaying an *already logged-in* browser session (vigilo-login
    ``--from-curl``): forcing a fresh login there makes authorize answer with
    a hop to the ID-porten login page instead of the ``app://`` code redirect.
    """
    if client_id is None:
        client_id, _ = client_credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "display": "touch",
    }
    if prompt is not None:
        params["prompt"] = prompt
    return f"{AUTH_BASE}/connect/authorize?{urlencode(params, quote_via=quote)}"


def code_from_redirect(target: str, expected_state: str | None = None) -> str:
    """Pull the authorization code out of a captured ``app://`` redirect.

    Accepts the whole failed-redirect URL from the address bar or a bare
    ``code`` value. ``urlsplit`` handles the custom scheme fine.

    >>> code_from_redirect("app://ch-parent-android.vigilo.no?code=ABC&state=XY", "XY")
    'ABC'
    >>> code_from_redirect("ABC123")
    'ABC123'
    >>> code_from_redirect("app://x?state=other", "XY")
    Traceback (most recent call last):
        ...
    ValueError: state in the redirect does not match — stale capture
    """
    target = target.strip()
    if "://" not in target and "?" not in target and "=" not in target:
        return target  # a bare code, pasted by hand
    params = parse_qs(urlsplit(target).query)
    state = (params.get("state") or [None])[0]
    if expected_state and state and state != expected_state:
        raise ValueError("state in the redirect does not match — stale capture")
    code = (params.get("code") or [None])[0]
    if not code:
        raise ValueError(f"no code= in {target[:120]!r}")
    return code


# --- Token endpoint ----------------------------------------------------------


def token_request(
    data: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST to the token endpoint with Basic client auth (nothing in the body).

    Raises :class:`ConfigError` on ``invalid_client`` (wrong id/secret) and
    :class:`AuthExpiredError` on ``invalid_grant`` (expired / used / already
    rotated) — both arrive as HTTP 400, so the ``error`` field decides. Any
    *unrecognised* error on a 400/401 is also :class:`ConfigError` (our bug,
    named loudly), never a silent auth expiry.
    """
    client_id, client_secret = client_credentials()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    with httpx.Client(transport=transport, timeout=timeout) as client:
        r = client.post(
            f"{AUTH_BASE}/connect/token",
            headers={"Authorization": f"Basic {basic}"},
            data=dict(data),
        )
    if r.status_code >= 400:
        error = ""
        with contextlib.suppress(ValueError):
            error = str(r.json().get("error", ""))
        detail = f"HTTP {r.status_code} {error or r.text[:200]}"
        # Branch on the ``error`` field, never the status alone — the token
        # endpoint answers 400 for a bad secret *and* an expired grant.
        if error == "invalid_client":
            raise ConfigError(
                "Vigilo rejected the client credentials (invalid_client) — check "
                "vigilo/client-id and vigilo/client-secret in pass"
            )
        if error in ("invalid_grant", "invalid_request"):
            # invalid_grant: expired / used / rotated-away grant — a dead
            # login. invalid_request lands in the same bucket here: Duende
            # uses it for grants it can no longer accept as given (e.g. an
            # already-consumed code), and the remedy is identical — re-login.
            raise AuthExpiredError(f"token request refused: {detail}")
        if r.status_code in (400, 401):
            # Any OTHER error value is our bug (bad scope, wrong request
            # shape), not an expired login — say its name, loudly, instead of
            # quietly flagging auth_required and waiting for a re-login that
            # fixes nothing.
            raise ConfigError(
                f"token endpoint answered an unrecognised error: {detail} — "
                "treat as a config/code bug, NOT an expired login"
            )
        raise ApiError(f"token request failed: {detail}")
    payload: dict[str, Any] = r.json()
    return payload


def exchange_code(code: str, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Trade a one-time authorization code for a token pair."""
    tokens = token_request(
        {"code": code, "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
        transport=transport,
    )
    if not tokens.get("refresh_token"):
        raise AuthExpiredError(
            "the token response had no refresh_token — the offline_access scope "
            "was probably declined. Check the scope string in the authorize URL."
        )
    return tokens


def refresh_tokens(
    refresh_token: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Refresh the pair. The response carries a **new** refresh token."""
    return token_request(
        {"refresh_token": refresh_token, "grant_type": "refresh_token"},
        transport=transport,
    )


def jwt_sub(access_token: str) -> str:
    """``userId`` for the app gateway = the ``sub`` claim of the access token.

    Decoded, never verified — we only echo it back as a query param.

    >>> import base64 as _b64, json as _json
    >>> _tok = "h." + _b64.urlsafe_b64encode(_json.dumps({"sub": "u-1"}).encode()).decode().rstrip("=") + ".s"
    >>> jwt_sub(_tok)
    'u-1'
    """
    payload = access_token.split(".")[1]
    payload += "==" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    sub = claims.get("sub")
    if not sub:
        raise ApiError("access token has no sub claim — cannot use the app gateway")
    return str(sub)


# --- File lock ---------------------------------------------------------------


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 90.0, stale_after: float = 300.0) -> Iterator[None]:
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
                raise VigiloError(f"timed out waiting for lock {path} — another vigilo process?")
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
    rename each other's file away (both upstream repos were bitten by this).
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
    """Persisted token pair with automatic, race-safe renewal.

    The whole load → refresh → save sequence runs under a file lock, and the
    file is re-read after the network round trip: if something else (a
    re-login, another sync) rotated the token while we were in flight, its
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
            raise AuthExpiredError(f"no tokens at {self.path} — run vigilo-login first")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AuthExpiredError(f"could not read {self.path}: {e}") from e
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AuthExpiredError(f"{self.path} holds no access_token — run vigilo-login")
        return data

    def save(self, tokens: dict[str, Any]) -> None:
        write_json_0600(self.path, tokens)

    def needs_refresh(self, tokens: Mapping[str, Any], *, force: bool = False) -> bool:
        """True when the access token is used up or within :data:`EXPIRY_MARGIN`.

        Lifetime comes from the response (``obtained_at`` + ``expires_in``),
        never from a hardcoded assumption about how long Vigilo grants.

        >>> store = TokenStore(path=Path("/nonexistent/tokens.json"))
        >>> t = {"obtained_at": 1000.0, "expires_in": 3600}
        >>> store._now = lambda: 4500.0     # 55 min in, > 60 s margin left
        >>> store.needs_refresh(t)
        False
        >>> store._now = lambda: 4550.0     # inside the 60 s margin
        >>> store.needs_refresh(t)
        True
        >>> store.needs_refresh(t, force=True)
        True
        """
        if force:
            return True
        obtained = float(tokens.get("obtained_at") or 0.0)
        expires_in = float(tokens.get("expires_in") or 0.0)
        if not expires_in:
            return True
        return self._now() > obtained + expires_in - EXPIRY_MARGIN

    def access_token(self, *, force_refresh: bool = False) -> str:
        with file_lock(self.lock_path):
            tokens = self.load()
            if not self.needs_refresh(tokens, force=force_refresh):
                return str(tokens["access_token"])
            old_refresh = tokens.get("refresh_token")
            if not old_refresh:
                raise AuthExpiredError("no refresh_token — run vigilo-login again")
            try:
                renewed = refresh_tokens(str(old_refresh), transport=self.transport)
            except ConfigError:
                raise
            except AuthExpiredError as e:
                raise AuthExpiredError(f"refresh token is no longer valid: {e}") from e
            if not renewed.get("refresh_token"):
                raise AuthExpiredError(
                    "refresh response had no refresh_token — nothing saved; "
                    "run vigilo-login again"
                )
            # Re-read under the lock: a re-login may have landed mid-flight.
            with contextlib.suppress(AuthExpiredError):
                current = self.load()
                if current.get("refresh_token") not in (None, "", old_refresh):
                    print(
                        "vigilo: newer tokens on disk than ours — keeping the file",
                        file=sys.stderr,
                    )
                    return str(current["access_token"])
            merged = dict(tokens)
            merged.update(renewed)
            merged["obtained_at"] = self._now()
            self.save(merged)
            return str(merged["access_token"])


# --- Defensive shape helpers -------------------------------------------------


def as_items(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Unwrap the list-or-wrapped-object answer every endpoint varies between.

    >>> as_items([{"a": 1}])
    [{'a': 1}]
    >>> as_items({"items": [{"a": 1}]})
    [{'a': 1}]
    >>> as_items({"messageThreads": [{"a": 1}]}, "messageThreads", "items")
    [{'a': 1}]
    >>> as_items(None), as_items({"items": None}), as_items("nei")
    ([], [], [])
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        wanted = keys or ("items",)
        for key in wanted:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def unit_name(value: Any) -> str:
    """``organizationalUnit`` is a dict-with-name or a bare string.

    >>> unit_name({"name": "AKS Eksempelby"})
    'AKS Eksempelby'
    >>> unit_name("AKS Eksempelby")
    'AKS Eksempelby'
    >>> unit_name(None)
    ''
    """
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("id") or "")
    if isinstance(value, str):
        return value
    return ""


def person_name(value: Any) -> str:
    """A sender is a ``{firstName, lastName}`` dict, or already a string.

    >>> person_name({"firstName": "Kari", "lastName": "Nordmann"})
    'Kari Nordmann'
    >>> person_name({"name": "SFO"})
    'SFO'
    >>> person_name("SFO")
    'SFO'
    >>> person_name(None)
    ''
    """
    if isinstance(value, Mapping):
        first = str(value.get("firstName") or "").strip()
        last = str(value.get("lastName") or "").strip()
        joined = " ".join(part for part in (first, last) if part)
        return joined or str(value.get("name") or value.get("fullName") or "")
    if isinstance(value, str):
        return value
    return ""


# Time / count fields on a thread-list item. Vigilo has never documented
# these and neither upstream repo reads them (they only track threadUid), so
# the exact spelling is discovered empirically — hence candidates, in order.
THREAD_TIME_KEYS = (
    "timeCreated",
    "lastMessageTimeCreated",
    "lastActivityTime",
    "modifiedTime",
    "updatedTime",
)
THREAD_COUNT_KEYS = (
    "messageCount",
    "messagesCount",
    "totalMessageCount",
    "numberOfMessages",
)


def thread_signature(thread: Mapping[str, Any]) -> str:
    """Change-detection key for a thread-list item: newest time + message count.

    The app gateway has no fromDate/toDate, no cursor and no ETag, so "new"
    and "answered" are client-side judgements. ``message_count`` matters even
    when the time does move: it catches a reply that lands inside the same
    second and it is what makes ``message_update`` re-emission reliable.

    >>> thread_signature({"timeCreated": "2026-10-09T08:00:00Z", "messageCount": 3})
    '2026-10-09T08:00:00Z|3'
    >>> thread_signature({"lastMessageTimeCreated": 1760000000000, "messagesCount": 1})
    '1760000000000|1'
    >>> thread_signature({"threadUid": "x"})        # no change signal at all
    ''
    """
    stamp = next((thread[k] for k in THREAD_TIME_KEYS if thread.get(k) not in (None, "")), "")
    count = next((thread[k] for k in THREAD_COUNT_KEYS if thread.get(k) is not None), "")
    if stamp == "" and count == "":
        return ""
    return f"{stamp}|{count}"


# --- The client --------------------------------------------------------------


class VigiloApi:
    """httpx client for both gateways. Read-only: GET and nothing else."""

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
        self._app = httpx.Client(base_url=API_BASE, transport=transport, timeout=timeout)
        self._web = httpx.Client(base_url=WEB_API_BASE, transport=transport, timeout=timeout)
        self._user_id: str | None = None

    # -- plumbing --

    def close(self) -> None:
        self._app.close()
        self._web.close()

    def __enter__(self) -> "VigiloApi":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = jwt_sub(self.store.access_token())
        return self._user_id

    def _headers(self, *, force_refresh: bool = False, app: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.store.access_token(force_refresh=force_refresh)}"}
        if app:
            # The app gateway rejects requests without this literal header.
            headers["appVersion"] = app_version()
        return headers

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        raw = r.headers.get("retry-after", "")
        try:
            return min(float(raw), BACKOFF_CAP)
        except ValueError:
            return None

    def _get(
        self,
        client: httpx.Client,
        path: str,
        params: Mapping[str, Any] | None,
        *,
        app: bool,
    ) -> httpx.Response:
        """GET with one forced-refresh retry on 401 and backoff on 429/5xx."""
        r: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            r = client.get(path, params=dict(params or {}), headers=self._headers(app=app))
            if r.status_code == 401:
                # Once only: a second 401 after a forced refresh is terminal.
                r = client.get(
                    path,
                    params=dict(params or {}),
                    headers=self._headers(app=app, force_refresh=True),
                )
                if r.status_code == 401:
                    raise AuthExpiredError(
                        "401 even after refreshing the token — run vigilo-login again"
                    )
                return r
            if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                delay = self._retry_after(r) or min(
                    BACKOFF_BASE * (2**attempt), BACKOFF_CAP
                )
                print(
                    f"vigilo: HTTP {r.status_code} on {path} — waiting {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            return r
        assert r is not None  # MAX_ATTEMPTS >= 1
        return r

    def app_get(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """Raw GET against the app gateway, ``userId`` injected."""
        merged: dict[str, Any] = dict(params or {})
        merged.setdefault("userId", self.user_id)
        return self._get(self._app, path, merged, app=True)

    def probe(self, path: str, params: Mapping[str, Any] | None = None, *, web: bool = False) -> httpx.Response:
        """Exactly one authenticated GET — no 401 retry, no exception, no backoff.

        For diagnostics only. A 401 from here is *information* (which gateway
        refuses this token?) rather than a state to react to.
        """
        client = self._web if web else self._app
        merged: dict[str, Any] = dict(params or {})
        if not web:
            merged.setdefault("userId", self.user_id)
        return client.get(path, params=merged, headers=self._headers(app=not web))

    def web_get(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """Raw GET against the web parent portal (Bearer only)."""
        return self._get(self._web, path, params or {}, app=False)

    def _json(self, r: httpx.Response, path: str) -> Any:
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ApiError(f"non-JSON response from {path}: {r.text[:120]}") from e

    def app_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._json(self.app_get(path, params), path)

    def web_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._json(self.web_get(path, params), path)

    # -- app gateway --

    def children(self) -> list[dict[str, Any]]:
        return as_items(self.app_json("/api/children"), "items", "children")

    def message_threads(
        self, child_id: str, *, page_size: int = 100, include_after_school: bool = True
    ) -> list[dict[str, Any]]:
        """Thread list for ONE child (``childIds`` takes a single uuid — loop).

        ``include_after_school_messages`` is the AKS flag: with the upstream
        default of False an AKS parent sees nothing at all. No date filter and
        no pagination exist here, so this is "newest ``page_size``" only.
        """
        data = self.app_json(
            "/api/message-threads",
            {
                "childIds": child_id,
                "pageSize": page_size,
                "unreadMessageThreadsCountOnly": "false",
                "includeAfterSchoolMessages": "true" if include_after_school else "false",
            },
        )
        return as_items(data, "messageThreads", "items")

    def thread(self, thread_uid: str, *, page_size: int = 100) -> dict[str, Any]:
        """Full thread with messages. Call this *immediately* before
        downloading attachments — the SAS URLs in it expire in ~6 h."""
        data = self.app_json(
            f"/api/messages/threads/{thread_uid}", {"pageSize": page_size}
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def download_attachment(url: str, *, timeout: float = 60.0) -> bytes:
        """Fetch a pre-signed Azure Blob URL — no auth headers, ever."""
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content

    # -- web parent portal --

    def web_children(self) -> list[dict[str, Any]]:
        return as_items(self.web_json("/api/children/my"), "items", "children")

    def news_feed(
        self, child_id: str, *, from_date: str, to_date: str
    ) -> list[dict[str, Any]]:
        """Oppslag / «Siste nytt» — ukeplan-PDF-er ligger her, ikke i meldinger."""
        return as_items(
            self.web_json(
                "/api/news-feed", {"childIds": child_id, "fromDate": from_date, "toDate": to_date}
            ),
            "items",
        )

    def post(self, post_id: str) -> dict[str, Any]:
        data = self.web_json(f"/api/posts/{post_id}")
        return data if isinstance(data, dict) else {}

    def post_attachment(self, post_id: str, attachment_id: str) -> bytes:
        """News-post attachments are Bearer-auth binary, *not* SAS URLs."""
        r = self.web_get(f"/api/posts/{post_id}/attachments/{attachment_id}")
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code} on attachment {attachment_id}")
        return r.content

    def consent_forms(self, child_id: str, organizational_unit_id: str) -> list[dict[str, Any]]:
        """Samtykker. Note the **singular** ``childId`` — most other endpoints
        take ``childIds``. The naming varies per endpoint; copy exactly.

        **404 with an empty body is how this API says "no forms"** (verified
        live 2026-10-11: the same route with a wrong param name answers 404
        *with* an ASP.NET "No HTTP resource was found" body, so the two are
        distinguishable). The app gateway mirror of this route,
        ``/api/consent-forms?childPersonId=…``, behaves the same way. Treat
        any 404 here as an empty list and let :meth:`consent_form_counts`
        carry the "are there forms at all" truth.
        """
        r = self.web_get(
            "/api/consent-forms",
            {"childId": child_id, "organizationalUnitId": organizational_unit_id},
        )
        if r.status_code == 404:
            return []
        return as_items(self._json(r, "/api/consent-forms"), "items")

    def consent_form_counts(self) -> dict[str, Any]:
        """``/api/consent-forms/counts`` on the app gateway — userId only.

        The one consent endpoint verified live to answer 200
        (``{"totalConsentFormsCount": N, "unreadConsentFormsCount": M,
        "consentForms": [...]}``, 2026-10-11), so it is the existence check
        the sync cross-references the per-unit lists against.
        """
        data = self.app_json("/api/consent-forms/counts")
        return data if isinstance(data, dict) else {}

    def absences(self, child_id: str, *, include_after_school: bool = True) -> list[dict[str, Any]]:
        return as_items(
            self.web_json(
                "/api/absences",
                {
                    "childIds": child_id,
                    "includeAfterSchoolAbsences": "true" if include_after_school else "false",
                },
            ),
            "items",
        )

    def lessons(
        self, child_id: str, organizational_unit_id: str, week: str | None = None
    ) -> list[dict[str, Any]]:
        return as_items(
            self.web_json(
                f"/api/students/{child_id}/lessons",
                {"organizationalUnitId": organizational_unit_id, "week": week or iso_week()},
            ),
            "items",
        )

    def scheduling_events(
        self, child_id: str, organizational_unit_id: str, week: str | None = None
    ) -> list[dict[str, Any]]:
        return as_items(
            self.web_json(
                f"/api/scheduling-events/{child_id}/student",
                {"organizationalUnitId": organizational_unit_id, "week": week or iso_week()},
            ),
            "items",
        )

    def school_years(self, organizational_unit_id: str) -> list[dict[str, Any]]:
        return as_items(
            self.web_json("/api/school-years", {"organizationalUnitId": organizational_unit_id}),
            "items",
        )


def iso_week(d: Any = None) -> str:
    """ISO week as Vigilo spells it, ``"2026-42"``.

    >>> from datetime import date
    >>> iso_week(date(2026, 10, 12))
    '2026-42'
    """
    y, w, _ = (d or date.today()).isocalendar()
    return f"{y}-{w:02d}"


# --- Login diagnostics -------------------------------------------------------


def api_describe_children(api: VigiloApi) -> str:
    """Report what the current token can see on each gateway, one line each.

    This is the Phase B2 experiment: the upstream README claims a single app
    token also unlocks the web parent portal, its own code hedges. The answer
    decides whether consents / news / timetables are in scope, so it gets
    printed rather than assumed. Child and unit ids go to the terminal only —
    they belong in ``[vigilo].unit_labels``, never in this repo.
    """
    lines: list[str] = []
    app = api.probe("/api/children")
    if app.status_code < 400:
        kids = as_items(app.json(), "items", "children")
        lines.append(f"app gateway  HTTP {app.status_code}  {len(kids)} child(ren)")
        for kid in kids:
            lines.append(f"             childId={kid.get('childId')} {person_name(kid)}")
    else:
        lines.append(f"app gateway  HTTP {app.status_code}  {app.text[:120]}")

    web = api.probe("/api/children/my", web=True)
    if web.status_code == 401:
        lines.append(
            "web gateway    HTTP 401 — the app token does NOT work here. "
            "Consents/news/timetable are out of reach until the web client is "
            "mapped; ship app-gateway-only (see PLAN-VIGILO.md Phase B2)."
        )
    elif web.status_code >= 400:
        lines.append(f"web gateway    HTTP {web.status_code}  {web.text[:120]}")
    else:
        kids = as_items(web.json(), "items", "children")
        lines.append(
            f"web gateway    HTTP {web.status_code}  {len(kids)} child(ren) — "
            "one token covers both gateways ✔"
        )
        for kid in kids:
            for unit in kid.get("organizationalUnits") or []:
                if isinstance(unit, dict):
                    groups = ", ".join(
                        person_name(g) for g in (unit.get("groups") or []) if isinstance(g, dict)
                    )
                    lines.append(
                        f"             unit id={unit.get('id')} name={unit_name(unit)!r}"
                        + (f" groups={groups}" if groups else "")
                    )
    return "\n".join(lines)


# --- Debug CLI ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Raw GET against the Vigilo API (debug probe). Prints 'HTTP <status>' "
            "and the body, even on 4xx/5xx. userId is injected automatically on "
            "the app gateway only."
        )
    )
    ap.add_argument("path", help="endpoint path, e.g. /api/children")
    ap.add_argument("params", nargs="?", default="{}", help='JSON object of query params')
    ap.add_argument("--web", action="store_true", help="use the web parent portal gateway")
    ap.add_argument("--max-chars", type=int, default=20000)
    args = ap.parse_args(argv)

    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except ValueError as e:
        ap.error(f"invalid params JSON: {e}")
        return 2

    try:
        with VigiloApi() as api:
            r = api.web_get(args.path, params) if args.web else api.app_get(args.path, params)
    except VigiloError as e:
        print(f"!! vigilo: {e}", file=sys.stderr)
        return 1

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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
