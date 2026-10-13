#!/usr/bin/env python3
"""skolemelding-login — one interactive login for the Skolemelding pipeline.

Skolemelding's ID-porten client *is* its own parent app, and the redirect URI
registered for that client is the custom scheme
``com.cgi.oslomelding.foresatte://oauth``. No browser can follow it, so there
is no ordinary callback to listen on — capturing the authorization code is a
small manual act. If the Skolemelding app is installed on the machine you log
in from, the OS may hand the redirect to *it* and it redeems the code (codes
are single-use). Log in from a machine without the app — this server
qualifies, your phone does not.

The failure you are aiming for is the success:

1. run ``skolemelding-login``, open the printed URL in any browser;
2. log in via ID-porten / BankID;
3. the browser then fails to open
   ``com.cgi.oslomelding.foresatte://oauth?code=…&state=…`` — copy that whole
   URL from the address bar;
4. paste it back here. A bare ``code`` value works too, but then the state
   check is skipped, so prefer the full URL.

Codes are single-use and short-lived. Anything failing means a fresh URL,
never re-pasting the same code.

The PKCE verifier must survive between printing the URL and pasting the
redirect, so it is kept in a ``skolemelding-pending.json`` next to the token
file (0600, one hour TTL) — ``--print-url`` and ``--paste`` can be separate
invocations.

Result: ``{"accessToken", "refreshToken", "userName", "deviceId",
"expiresAt"}`` written 0600 to ``~/.config/diary/skolemelding-auth.json``
(dir 0700, mkstemp + rename). After saving, the login is verified by doing
one real refresh and listing the first message page. When the refresh token
eventually dies, every tool tells you to re-run this.

Examples::

    uv run skolemelding-login             # print URL, wait for the paste
    uv run skolemelding-login --print-url # just the URL (finish with --paste)
    uv run skolemelding-login --paste 'com.cgi.oslomelding.foresatte://oauth?code=…&state=…'
    uv run skolemelding-login --no-verify # skip the post-login refresh + probe
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from scripts.skolemelding_api import (
    ApiError,
    AuthExpiredError,
    SkolemeldingApi,
    SkolemeldingError,
    TokenStore,
    authorize_url,
    code_from_redirect,
    idporten_metadata,
    login_from_code,
    pkce_pair,
    write_json_0600,
)

# A captured redirect is only worth validating against a login we started, and
# only for as long as the login is plausibly still in progress.
PENDING_TTL = 3600.0


def pending_path(store: TokenStore) -> Path:
    return store.path.with_name("skolemelding-pending.json")


def remember_pending(store: TokenStore, state: str, code_verifier: str, token_endpoint: str) -> None:
    write_json_0600(
        pending_path(store),
        {
            "state": state,
            "code_verifier": code_verifier,
            "token_endpoint": token_endpoint,
            "created_at": time.time(),
        },
    )


def consume_pending(store: TokenStore) -> dict[str, Any] | None:
    """The pending login we last started, or None if there is none / it went stale."""
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
    if not data.get("state") or not data.get("code_verifier"):
        return None
    return data


def verify_login(store: TokenStore) -> str:
    """Prove the refresh grant works, then list what the token can see."""
    store.access_token(force_refresh=True)  # raises if the refresh grant is dead
    with SkolemeldingApi(store) as api:
        data = api.search_messages(page=1, page_size=5)
    total = data.get("TotalMatching")
    return f"refresh works ✔ — {total} message(s) visible in the inbox"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-time interactive login for Skolemelding (see module docstring).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--print-url", action="store_true",
                    help="print the authorization URL and exit; finish with --paste")
    ap.add_argument("--paste", metavar="URL_OR_CODE", default=None,
                    help="finish a login with a captured redirect URL or a bare code")
    ap.add_argument("--token-file", default=None,
                    help="override the token path (default: skolemelding.token_file / "
                         "~/.config/diary/skolemelding-auth.json)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-login refresh + inbox probe")
    args = ap.parse_args(argv)

    store = TokenStore(Path(args.token_file).expanduser()) if args.token_file else TokenStore()
    print(f"(token file: {store.path})", file=sys.stderr)

    try:
        if args.paste:
            pending = consume_pending(store)
            if not pending:
                raise SkolemeldingError(
                    "no pending login (or it went stale) — run skolemelding-login "
                    "again for a fresh URL"
                )
            code = code_from_redirect(args.paste, str(pending["state"]))
            code_verifier = str(pending["code_verifier"])
            token_endpoint = str(
                pending.get("token_endpoint")
                or idporten_metadata()["token_endpoint"]
            )
        else:
            metadata = idporten_metadata()
            code_verifier, challenge = pkce_pair()
            state = secrets.token_hex(16)
            url = authorize_url(metadata["authorization_endpoint"], state, challenge)
            token_endpoint = str(metadata["token_endpoint"])
            remember_pending(store, state, code_verifier, token_endpoint)
            print("\nOpen this URL in any browser:\n")
            print(url)
            print("\nAfter logging in the browser will fail on a "
                  "com.cgi.oslomelding.foresatte://oauth?code=… address — that "
                  "failure is the success.\nPaste the whole URL here "
                  "(a bare code works too).\n")
            if args.print_url:
                print("\n(finish with: skolemelding-login --paste "
                      "'<the com.cgi.oslomelding.foresatte:// URL>')", file=sys.stderr)
                return 0
            try:
                pasted = input("paste> ").strip()
            except EOFError as e:
                raise SkolemeldingError(
                    "no code on stdin — use --paste 'com.cgi.oslomelding.foresatte://…'"
                ) from e
            if not pasted:
                raise SkolemeldingError("nothing pasted")
            code = code_from_redirect(pasted, state)
            pending_path(store).unlink(missing_ok=True)

        tokens = login_from_code(code, code_verifier, token_endpoint)
        store.save(tokens)
    except (AuthExpiredError, ApiError, SkolemeldingError, ValueError, httpx.HTTPError) as e:
        print(f"!! skolemelding login failed: {e}", file=sys.stderr)
        print("   (codes are single-use — start over with a fresh URL)",
              file=sys.stderr)
        return 1

    print(f"logged in — userName={tokens.get('userName')!r} "
          f"deviceId={tokens.get('deviceId')}")
    if args.no_verify:
        return 0
    print("\nverifying the login (one refresh + one inbox page)…", file=sys.stderr)
    try:
        print(verify_login(store))
    except SkolemeldingError as e:
        print(f"!! post-login verification failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
