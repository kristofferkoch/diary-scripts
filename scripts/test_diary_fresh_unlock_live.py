from __future__ import annotations

import http.client
import os
import re
import socket
import ssl
import time
import urllib.parse
from pathlib import Path

import pytest
from playwright.sync_api import CDPSession, Page, sync_playwright

ORIGIN = os.environ.get("DIARY_TEST_LOCK_ORIGIN", "")
PASSWORD = os.environ.get("DIARY_TEST_LOCK_PASSWORD", "")
USERNAME = os.environ.get("DIARY_TEST_LOCK_USERNAME", "")
CDP_URL = os.environ.get("DIARY_TEST_CDP_URL", "")
NOTIFICATIONS = Path(os.environ.get("DIARY_TEST_NOTIFICATIONS", "/nonexistent"))
SESSION_COOKIE = "diary_lock_authelia_session"
TRANSACTION_COOKIE = "diary_lock_fresh_unlock"
pytestmark = pytest.mark.skipif(
    not ORIGIN or not PASSWORD or not USERNAME or not CDP_URL,
    reason="local Authelia integration is not running",
)


def _cookie(page: Page, name: str) -> str:
    return next(item["value"] for item in page.context.cookies() if item["name"] == name)


def _fetch(page: Page, method: str, path: str, headers: dict[str, str] | None = None) -> dict[str, str | int]:
    if page.url.startswith(f"{ORIGIN}/lock/unlock/return"):
        page.goto(f"{ORIGIN}/authelia/settings")
    return page.evaluate(
        """async ({method, url, headers}) => {
            const response = await fetch(url, {method, headers, redirect: 'follow'});
            return {status: response.status, url: response.url, body: await response.text()};
        }""",
        {"method": method, "url": f"{ORIGIN}{path}", "headers": headers or {}},
    )


def _raw(path: str, cookie: str) -> int:
    hostname = urllib.parse.urlsplit(ORIGIN).hostname
    assert hostname
    tls_context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(hostname, 19443, context=tls_context, timeout=5)
    connection.sock = tls_context.wrap_socket(
        socket.create_connection(("127.0.0.1", 19443), timeout=5),
        server_hostname=hostname,
    )
    connection.request("GET", path, headers={"Cookie": cookie})
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def _begin(page: Page) -> tuple[str, str]:
    old_session = _cookie(page, SESSION_COOKIE)
    response = _fetch(
        page, "POST", "/lock/unlock/begin",
        {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-Diary-Unlock-Begin": "yes",
        },
    )
    assert response["status"] == 200
    assert "/authelia/?rd=" in str(response["url"])
    return str(response["url"]), old_session


def _complete(page: Page, location: str) -> None:
    page.goto(location)
    page.locator("#username-textfield").fill(USERNAME)
    page.locator("#password-textfield").fill(PASSWORD)
    page.locator("#sign-in-button").click()
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


def test_real_authelia_webauthn_flow() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        context.set_default_timeout(10_000)
        page = context.pages[0]
        cdp = context.new_cdp_session(page)
        cdp.send("Security.enable")
        cdp.send("Security.setIgnoreCertificateErrors", {"ignore": True})
        cdp.send("WebAuthn.enable")
        authenticator = cdp.send(
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
        authenticator_id = authenticator["authenticatorId"]
        page.goto(f"{ORIGIN}/authelia/")
        page.locator("#username-textfield").fill(USERNAME)
        page.locator("#password-textfield").fill(PASSWORD)
        page.locator("#sign-in-button").click()
        page.wait_for_url("**/2fa/webauthn", timeout=10_000)
        page.goto(f"{ORIGIN}/authelia/settings")
        page.locator('button[aria-label="open drawer"]').click()
        page.get_by_text("Two-Factor Authentication", exact=True).click()
        page.get_by_role("button", name="Add", exact=True).click()
        codes: list[str] = []
        for _ in range(100):
            if NOTIFICATIONS.exists():
                codes = re.findall(r"(?m)^([A-Z0-9]{8})$", NOTIFICATIONS.read_text())
            if codes:
                break
            time.sleep(0.1)
        assert codes
        page.get_by_label("One-Time Code").fill(codes[-1])
        page.get_by_role("button", name="Verify", exact=True).click()
        page.get_by_label("Description").fill("integration-test")
        page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_text("Successfully added the WebAuthn Credential").wait_for()
        registered_count = _sign_count(cdp, authenticator_id)

        location, old_session = _begin(page)
        transaction = _cookie(page, TRANSACTION_COOKIE)
        stale_cookie = f"{SESSION_COOKIE}={old_session}; {TRANSACTION_COOKIE}={transaction}"
        assert _raw("/lock/unlock/return", stale_cookie) not in {200, 204}
        _complete(page, location)
        assert _sign_count(cdp, authenticator_id) > registered_count
        first_count = _sign_count(cdp, authenticator_id)
        assert _fetch(page, "GET", "/lock/unlock/return")["status"] == 403
        _consume(page)

        location, old_session = _begin(page)
        transaction = _cookie(page, TRANSACTION_COOKIE)
        for cookie in (
            f"{SESSION_COOKIE}={old_session}; {TRANSACTION_COOKIE}={transaction}",
            f"dsh_session=stolen; kimi_session=stolen; {TRANSACTION_COOKIE}={transaction}",
            f"{SESSION_COOKIE}={old_session}",
        ):
            assert _raw("/lock/unlock/return", cookie) not in {200, 204}
        _complete(page, location)
        assert _sign_count(cdp, authenticator_id) > first_count
        _consume(page)
        browser.close()
