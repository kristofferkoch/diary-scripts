"""Persistent token authentication for the private mail web UI.

The token lives outside the workspace in a caller-owned ``0600`` file. Login
links carry it in the URL fragment, never in an HTTP request or access log. A
login POST exchanges it for a secure, HttpOnly cookie; API clients may instead
send ``Authorization: Bearer``.
"""

from __future__ import annotations

import html
import hmac
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "mail_reader_auth"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60
DEFAULT_TOKEN_FILE = Path("~/.config/diary/mail-reader-token")


class AuthError(RuntimeError):
    """The token file is missing or unsafe."""


def token_path() -> Path:
    return Path(os.environ.get("MAIL_READER_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()


def write_token(path: Path) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd, temporary = tempfile.mkstemp(prefix=".mail-token-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return token


def read_token(path: Path | None = None) -> str:
    path = path or token_path()
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
        raise AuthError(f"token file must be a regular 0600 file owned by the caller: {path}")
    token = path.read_text().strip()
    if len(token) < 32:
        raise AuthError("token file is invalid; rotate it")
    return token


def token_for_notification(path: Path | None = None, *, rotate: bool = False) -> str:
    path = path or token_path()
    if rotate or not path.exists():
        return write_token(path)
    return read_token(path)


def _valid(candidate: str, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def _headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _cookie(response: Response, token: str, root_path: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, secure=True,
        httponly=True, samesite="strict", path=root_path or "/",
    )


def _login_page(root_path: str, *, failed: bool = False) -> HTMLResponse:
    action = html.escape(root_path + "/login", quote=True)
    error = "<p>Token not accepted.</p>" if failed else ""
    body = f"""<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Mail login</title>
<h1>Mail login</h1>{error}
<form id="login" method="post" action="{action}">
  <label>Token <input id="token" name="token" type="password" required autofocus></label>
  <button type="submit">Log in</button>
</form>
<script>
const root = {json.dumps(root_path + "/")};
const action = {json.dumps(root_path + "/login")};
async function login(token) {{
  const response = await fetch(action, {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{token}}), credentials: "same-origin"
  }});
  if (response.ok) location.replace(root);
  else document.querySelector("form").hidden = false;
}}
document.querySelector("form").addEventListener("submit", event => {{
  event.preventDefault();
  login(document.querySelector("#token").value);
}});
if (location.hash.startsWith("#token=")) {{
  const token = decodeURIComponent(location.hash.slice(7));
  history.replaceState(null, "", location.pathname);
  document.querySelector("form").hidden = true;
  login(token);
}}
</script>"""
    return HTMLResponse(body, status_code=401 if failed else 200, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class MailAuthMiddleware:
    def __init__(self, app: ASGIApp, path: Path | None = None) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        if path == "/healthz" or path.startswith("/static/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            expected = read_token(self.path)
        except (AuthError, OSError):
            response = PlainTextResponse("mail authentication is not configured", status_code=503)
            await _headers(response)(scope, receive, send)
            return

        if path == "/login" and request.method == "GET":
            if _valid(request.cookies.get(COOKIE_NAME, ""), expected):
                response = PlainTextResponse("", status_code=303, headers={"Location": root_path + "/"})
            else:
                response = _login_page(root_path)
            await _headers(response)(scope, receive, send)
            return
        if path == "/login" and request.method == "POST":
            try:
                if request.headers.get("content-type", "").startswith("application/json"):
                    payload = await request.json()
                    candidate = payload.get("token", "") if isinstance(payload, dict) else ""
                else:
                    candidate = parse_qs((await request.body()).decode()).get("token", [""])[0]
            except (ValueError, UnicodeDecodeError):
                candidate = ""
            if not _valid(candidate, expected):
                response = JSONResponse({"ok": False}, status_code=401)
            else:
                response = Response(status_code=204)
                _cookie(response, candidate, root_path)
            await _headers(response)(scope, receive, send)
            return

        bearer = request.headers.get("authorization", "")
        candidate = bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else ""
        if not _valid(candidate, expected):
            candidate = request.cookies.get(COOKIE_NAME, "")
        if not _valid(candidate, expected):
            response = PlainTextResponse("authentication required", status_code=401)
            await _headers(response)(scope, receive, send)
            return
        await self.app(scope, receive, send)
