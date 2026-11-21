from __future__ import annotations

import http.client
import os
import socket
import ssl
import urllib.parse

import pytest
from playwright.sync_api import CDPSession, Page, sync_playwright

ORIGIN = os.environ.get("DIARY_TEST_LOCK_ORIGIN", "")
ENROLLMENT_CODE = os.environ.get("DIARY_TEST_LOCK_ENROLLMENT_CODE", "")
CDP_URL = os.environ.get("DIARY_TEST_CDP_URL", "")
ALTERNATE_CDP_URL = os.environ.get("DIARY_TEST_ALTERNATE_CDP_URL", "")
SESSION_COOKIE = "diary_lock_session"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
pytestmark = pytest.mark.skipif(
    not ORIGIN or not ENROLLMENT_CODE or not CDP_URL or not ALTERNATE_CDP_URL,
    reason="local passkey integration is not running",
)


def _cookie(page: Page, name: str) -> str:
    return next(
        item["value"] for item in page.context.cookies() if item["name"] == name
    )


def _fetch(
    page: Page, method: str, path: str, headers: dict[str, str] | None = None
) -> dict[str, str | int]:
    if page.url.startswith(f"{ORIGIN}/lock/unlock/return"):
        page.goto(f"{ORIGIN}/lock/auth/")
    return page.evaluate(
        """async ({method, url, headers}) => {
            const response = await fetch(url, {method, headers, redirect: 'follow'});
            return {status: response.status, url: response.url, body: await response.text()};
        }""",
        {"method": method, "url": f"{ORIGIN}{path}", "headers": headers or {}},
    )


def _raw(
    path: str, cookie: str, method: str = "GET", headers: dict[str, str] | None = None
) -> int:
    parsed = urllib.parse.urlsplit(ORIGIN)
    hostname = parsed.hostname
    assert hostname
    port = parsed.port or 443
    tls_context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(
        hostname, port, context=tls_context, timeout=5
    )
    connection.sock = tls_context.wrap_socket(
        socket.create_connection(("127.0.0.1", port), timeout=5),
        server_hostname=hostname,
    )
    connection.request(method, path, headers={"Cookie": cookie, **(headers or {})})
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def _begin(page: Page) -> tuple[str, str]:
    old_session = next(
        (
            item["value"]
            for item in page.context.cookies()
            if item["name"] == SESSION_COOKIE
        ),
        "",
    )
    response = _fetch(
        page,
        "POST",
        "/lock/unlock/begin",
        {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-Diary-Unlock-Begin": "yes",
        },
    )
    assert response["status"] == 200
    assert "/lock/auth/?rd=" in str(response["url"])
    return str(response["url"]), old_session


def _complete(page: Page, location: str) -> None:
    page.goto(location)
    page.get_by_role("button", name="Tap passkey to authorize").click()
    page.wait_for_url("**/lock/unlock/return", timeout=15_000)
    assert '"status":"fresh"' in page.locator("body").inner_text()


def _consume(page: Page) -> None:
    headers = {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-Diary-Unlock-Confirm": "yes",
    }
    assert _fetch(page, "POST", "/lock/unlock/consume", headers)["status"] == 204
    assert _fetch(page, "POST", "/lock/unlock/consume", headers)["status"] == 403


def _sign_count(cdp: CDPSession, authenticator_id: str) -> int:
    result = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator_id})
    return sum(item["signCount"] for item in result["credentials"])


def _restore_session(cdp: CDPSession, value: str) -> None:
    assert cdp.send(
        "Network.setCookie",
        {
            "url": ORIGIN,
            "name": SESSION_COOKIE,
            "value": value,
            "path": "/lock",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Strict",
        },
    )["success"]


def _authenticator(page: Page) -> tuple[CDPSession, str]:
    cdp = page.context.new_cdp_session(page)
    cdp.send("Security.enable")
    cdp.send("Security.setIgnoreCertificateErrors", {"ignore": True})
    cdp.send("WebAuthn.enable")
    result = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
            },
        },
    )
    return cdp, result["authenticatorId"]


def test_real_passkey_enrollment_freshness_and_replay() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        context.set_default_timeout(10_000)
        page = context.pages[0]
        cdp, authenticator_id = _authenticator(page)
        page.goto(f"{ORIGIN}/lock/enroll/")
        page.get_by_label("One-time enrollment code").fill(ENROLLMENT_CODE)
        page.get_by_role("button", name="Create passkey").click()
        page.wait_for_url("**/lock/", timeout=15_000)
        registered_count = _sign_count(cdp, authenticator_id)

        location, old_session = _begin(page)
        transaction = _cookie(page, TRANSACTION_COOKIE)
        stale_cookie = (
            f"{SESSION_COOKIE}={old_session}; {TRANSACTION_COOKIE}={transaction}"
        )
        assert _raw("/lock/unlock/return", stale_cookie) not in {200, 204}
        _complete(page, location)
        assert _sign_count(cdp, authenticator_id) > registered_count
        assert _fetch(page, "GET", "/lock/unlock/return")["status"] == 403
        _consume(page)

        preexisting = _cookie(page, SESSION_COOKIE)
        cdp.send("Network.deleteCookies", {"name": SESSION_COOKIE, "url": ORIGIN})
        location, _ = _begin(page)
        omitted_transaction = _cookie(page, TRANSACTION_COOKIE)
        _restore_session(cdp, preexisting)
        before_replay = _sign_count(cdp, authenticator_id)
        omitted_cookie = f"{SESSION_COOKIE}={preexisting}; {TRANSACTION_COOKIE}={omitted_transaction}"
        assert _raw("/lock/unlock/return", omitted_cookie) not in {200, 204}
        assert _sign_count(cdp, authenticator_id) == before_replay
        _complete(page, location)
        _consume(page)

        other_browser = playwright.chromium.connect_over_cdp(ALTERNATE_CDP_URL)
        other_context = other_browser.contexts[0]
        other_page = other_context.pages[0]
        other_cdp, other_authenticator = _authenticator(other_page)
        credentials = cdp.send(
            "WebAuthn.getCredentials", {"authenticatorId": authenticator_id}
        )["credentials"]
        for credential in credentials:
            other_cdp.send(
                "WebAuthn.addCredential",
                {"authenticatorId": other_authenticator, "credential": credential},
            )
        other_page.goto(f"{ORIGIN}/lock/auth/")
        other_location, _ = _begin(other_page)
        _complete(other_page, other_location)
        _consume(other_page)
        alternate = _cookie(other_page, SESSION_COOKIE)

        location, _ = _begin(page)
        transaction = _cookie(page, TRANSACTION_COOKIE)
        before_alternate = _sign_count(cdp, authenticator_id)
        alternate_cookie = (
            f"{SESSION_COOKIE}={alternate}; {TRANSACTION_COOKIE}={transaction}"
        )
        assert _raw("/lock/unlock/return", alternate_cookie) not in {200, 204}
        assert _sign_count(cdp, authenticator_id) == before_alternate
        _complete(page, location)
        _consume(page)
        browser.close()
