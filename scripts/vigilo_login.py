#!/usr/bin/env python3
"""vigilo-login — one interactive login for the Vigilo pipeline.

Vigilo's OAuth client *is* its own Android app, and the redirect URI registered
for that client is the custom scheme ``app://ch-parent-android.vigilo.no``. No
browser can follow it, so there is no ordinary callback to listen on — capturing
the authorization code is a small manual act. Worse: if the Vigilo app is
installed on the machine you log in from, the OS hands the redirect to *it* and
it redeems the code (codes are single-use), so the exchange then fails with
``invalid_grant`` no matter how fast you are. Log in from a machine without the
app — this server qualifies, your phone does not.

The failure you are aiming for is the success:

1. run ``vigilo-login``, open the printed URL on a desktop with no Vigilo app;
2. log in via ID-porten / BankID;
3. the browser then fails to open ``app://…?code=…&state=…`` — copy that whole
   URL from the address bar. (Firefox keeps it; Chrome: DevTools → Network →
   *Preserve log* **before** you start, then copy from the log.);
4. paste it back here. A bare ``code`` value works too.

Codes are single-use and live ~60 s. Anything failing means a fresh URL, never
re-pasting the same code.

Headless alternative — ``--from-curl``: log in in any browser, then DevTools →
Network → the ``authorize`` request → *Copy as cURL*, and feed that to this tool.
Only the session cookies are used, to replay the authorize request ourselves and
read the code straight out of the ``302 Location`` header — the browser never
has to hand the redirect to anything.

Result: the raw OAuth response plus a local ``obtained_at``, written 0600 to
``~/.config/diary/vigilo-tokens.json`` (dir 0700, mkstemp + rename). Refresh
tokens rotate on use and expire after 30–90 days, so expect to rerun this
roughly once a semester; ``vigilo-sync`` sets ``auth_required`` and ``vigiloshown``
shouts about it until you do.

Examples::

    uv run vigilo-login                     # print URL, wait for the paste
    uv run vigilo-login --print-url         # just the URL (finish with --paste)
    uv run vigilo-login --paste 'app://…?code=…&state=…'
    pbpaste | uv run vigilo-login --from-curl -     # cookie replay
    uv run vigilo-login --no-verify            # skip the post-login gateway probe
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from scripts.vigilo_api import (
    AuthExpiredError,
    ConfigError,
    TokenStore,
    VigiloError,
    api_describe_children,
    authorize_url,
    code_from_redirect,
    exchange_code,
    token_path,
)

# A captured redirect is only worth validating against a state we issued, and
# only for as long as the login is plausibly still in progress.
PENDING_TTL = 3600.0


def pending_path(store: TokenStore) -> Path:
    return store.path.with_name("vigilo-pending.json")


def remember_state(store: TokenStore, state: str) -> None:
    from scripts.vigilo_api import write_json_0600

    write_json_0600(pending_path(store), {"state": state, "created_at": time.time()})


def consume_state(store: TokenStore) -> str | None:
    """The state we last issued, or None if there is none / it went stale."""
    path = pending_path(store)
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    if time.time() - float(data.get("created_at") or 0) > PENDING_TTL:
        return None
    state = data.get("state")
    return str(state) if state else None


# --- Cookie-replay capture ---------------------------------------------------

_COOKIE_HEADER_RE = re.compile(r"-H\s+['\"]cookie\s*:\s*([^'\"]+)['\"]", re.IGNORECASE)
_COOKIE_DATA_RE = re.compile(r"(?:^|\s)(?:-b|--cookie)\s+'([^']+)'")


def cookies_from_curl(curl_text: str) -> str:
    """Pull the cookie header out of a *Copy as cURL* blob.

    Chrome writes ``-b '…'``, Firefox writes ``-H 'cookie: …'``. Handles are
    quoted either way; anything else in the blob is ignored — we only ever need
    the session cookies and a plain user agent.

    >>> cookies_from_curl("curl 'https://x' -H 'cookie: a=1; b=2'")
    'a=1; b=2'
    >>> cookies_from_curl("curl 'https://x' -b 'c=3; d=4' --compressed")
    'c=3; d=4'
    >>> cookies_from_curl("curl https://example")
    Traceback (most recent call last):
        ...
    ValueError: no cookie header found in the pasted curl command
    """
    for pattern in (_COOKIE_HEADER_RE, _COOKIE_DATA_RE):
        m = pattern.search(curl_text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    raise ValueError("no cookie header found in the pasted curl command")


def code_via_cookies(cookie_header: str, state: str, *, timeout: float = 30.0) -> str:
    """Ask authorize ourselves, with a live ID-porten session, and read the code.

    The redirect is still to ``app://`` — but we are the client, so we just read
    the ``Location`` header instead of trying to follow it. A *missing* redirect
    means the session was not logged in (the registered redirect_uri always
    answers 302; an unregistered one answers HTTP 200 with an HTML error page),
    so this is the diagnostic to show.
    """
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        r = client.get(
            # prompt=None is load-bearing: the pasted cookies ARE the login,
            # so forcing prompt=login would bounce the replay to the ID-porten
            # login page instead of the app:// code redirect.
            authorize_url(state, prompt=None),
            headers={"cookie": cookie_header, "user-agent": "Mozilla/5.0"},
        )
    location = r.headers.get("location", "")
    if not location:
        raise AuthExpiredError(
            f"authorize gave HTTP {r.status_code} with no redirect — the pasted "
            "session was not logged in. Log in in the browser, then copy the "
            "cURL again (it must come from the /connect/authorize request)."
        )
    return code_from_redirect(location, state)


# --- Storing -----------------------------------------------------------------


def store_tokens(store: TokenStore, tokens: dict[str, Any]) -> None:
    """Persist the raw OAuth response + our own ``obtained_at`` clock."""
    if not tokens.get("access_token"):
        raise AuthExpiredError("token response had no access_token — nothing stored")
    stored = dict(tokens)
    stored["obtained_at"] = time.time()
    store.save(stored)


def verify_both_gateways() -> str:
    """Phase B2: does one app token also work on the web parent gateway?

    The upstream README claims yes, its own code hedges — this is where it gets
    settled. Prints both probes and returns a one-line verdict.
    """
    from scripts.vigilo_api import VigiloApi

    lines: list[str] = []
    with VigiloApi() as api:
        lines.append(api_describe_children(api))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-time interactive login for Vigilo (see module docstring).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--print-url", action="store_true",
                    help="print the authorization URL and exit; finish with --paste")
    ap.add_argument("--paste", metavar="URL_OR_CODE", default=None,
                    help="finish a login with a captured app:// redirect or a bare code")
    ap.add_argument("--from-curl", metavar="FILE_OR_DASH", default=None,
                    help="capture the code by replaying browser cookies from a "
                         "*Copy as cURL* blob (use - for stdin)")
    ap.add_argument("--token-file", default=None,
                    help="override the token path (default: vigilo.token_file / "
                         "~/.config/diary/vigilo-tokens.json)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-login probe of both gateways")
    args = ap.parse_args(argv)

    store = TokenStore(Path(args.token_file).expanduser()) if args.token_file else TokenStore()
    print(f"(token file: {store.path})", file=sys.stderr)

    try:
        if args.from_curl:
            raw = (sys.stdin.read() if args.from_curl == "-"
                   else Path(args.from_curl).read_text(encoding="utf-8"))
            cookie_header = cookies_from_curl(raw)
            state = secrets.token_urlsafe(24)
            print("(replaying your session cookies to fetch a fresh code…)",
                  file=sys.stderr)
            code = code_via_cookies(cookie_header, state)
        elif args.paste:
            code = code_from_redirect(args.paste, consume_state(store))
        else:
            state = secrets.token_urlsafe(24)
            url = authorize_url(state)
            remember_state(store, state)
            print("\nOpen this URL on a machine with NO Vigilo app installed:\n")
            print(url)
            print("\nAfter logging in the browser will fail on an "
                  "app://ch-parent-android.vigilo.no?code=… address — that "
                  "failure is the success.\nPaste the whole URL here "
                  "(a bare code works too).\n")
            if args.print_url:
                print("\n(finish with: vigilo-login --paste '<the app:// URL>')",
                      file=sys.stderr)
                return 0
            try:
                pasted = input("paste> ").strip()
            except EOFError as e:
                raise VigiloError(
                    "no code on stdin — use --paste 'app://…' or --from-curl -"
                ) from e
            if not pasted:
                raise VigiloError("nothing pasted")
            code = code_from_redirect(pasted, consume_state(store))

        tokens = exchange_code(code)
        store_tokens(store, tokens)
    except ConfigError as e:
        print(f"!! vigilo config error: {e}", file=sys.stderr)
        return 2
    except (AuthExpiredError, VigiloError, ValueError, httpx.HTTPError) as e:
        print(f"!! vigilo login failed: {e}", file=sys.stderr)
        print("   (codes are single-use — start over with a fresh URL)",
              file=sys.stderr)
        return 1

    scope = tokens.get("scope") or ""
    print(f"logged in — expires_in={tokens.get('expires_in')} scope={scope!r}")
    if "offline_access" not in scope:
        print("!! scope is missing offline_access — the refresh token will not "
              "survive the access-token expiry", file=sys.stderr)
    if args.no_verify:
        return 0
    print("\nprobing both gateways with the new token:", file=sys.stderr)
    try:
        print(verify_both_gateways())
    except VigiloError as e:
        print(f"!! post-login probe failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
