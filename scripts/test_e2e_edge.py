"""Layer 3: real Caddy and Authelia in front of the merge console."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.e2e import edge, harness


def head(paths) -> str:
    return harness.git(paths.approved, "rev-parse", "HEAD")

pytestmark = pytest.mark.skipif(
    not (Path(edge.AUTHELIA).is_file() and Path(edge.CADDY).is_file()),
    reason="real authelia and caddy binaries are not installed",
)


@pytest.fixture
def stack(tmp_path: Path):
    root = tmp_path / "e2e"
    root.mkdir()
    instance = edge.start(root)
    try:
        yield instance
    finally:
        instance.close()


BROWSER = {"Accept": "text/html,application/xhtml+xml"}


def test_anonymous_redirects_to_authelia(stack: edge.EdgeStack) -> None:
    status, headers, body = stack.request("GET", "/lock/merge/", headers=BROWSER)

    assert status in {302, 303}
    assert "/authelia/" in headers.get("Location", "")
    assert b"Queued commits" not in body


def test_first_factor_only_is_still_denied(stack: edge.EdgeStack) -> None:
    cookie = stack.login_password()

    status, headers, _ = stack.request("GET", "/lock/merge/", cookie=cookie, headers=BROWSER)

    assert status in {302, 303}, (status, headers)


def test_session_cookie_attributes(stack: edge.EdgeStack) -> None:
    body = f'{{"username": "{edge.OWNER}", "password": "{edge.PASSWORD}", "keepMeLoggedIn": false}}'
    status, headers, _ = stack.request(
        "POST", "/authelia/api/firstfactor", body=body.encode(),
        headers={"Content-Type": "application/json", "Origin": stack.origin},
    )

    assert status == 200
    cookie = headers.get("Set-Cookie", "")
    assert cookie.startswith(edge.SESSION_COOKIE + "=")
    lowered = cookie.lower()
    assert "secure" in lowered
    assert "httponly" in lowered
    assert "samesite=strict" in lowered


BROWSER_ENV = ("E2E_PLAYWRIGHT_CORE", "E2E_CHROMIUM")


def _browser_ready() -> bool:
    return all(os.environ.get(name) for name in BROWSER_ENV)


@pytest.mark.skipif(not _browser_ready(), reason="browser tooling is not provisioned")
def test_full_password_and_passkey_reaches_the_console(stack: edge.EdgeStack) -> None:
    """A real Authelia two-factor session, driven through Caddy, opens the page."""
    result = edge.run_driver(stack)

    console = [step for step in result["steps"] if step["step"] == "console"][-1]
    assert console["url"].endswith("/lock/merge/"), result
    assert "Queued commits" in console["text"] or "Nothing queued" in console["text"]


@pytest.mark.skipif(not _browser_ready(), reason="browser tooling is not provisioned")
@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_browser_decision_through_the_real_edge_publishes_once(
    stack: edge.EdgeStack, repo: str,
) -> None:
    """Both queues cross Caddy, Authelia, the console, grant, and publication."""
    paths = stack.harness_stack.paths_by_repo[repo]
    queued = harness.enqueue_change(paths)
    base = head(paths)
    transaction = stack.preauthorize(repo, "merge", queued, base)

    result = edge.run_driver(stack, transaction=transaction, repo=repo)

    decided = [step for step in result["steps"] if step["step"] == "decided"][-1]
    assert "Merged" in decided["text"], result
    assert head(paths) == queued
    completed = harness.wait_for(lambda: [
        event for event in stack.harness_stack.audit.events()
        if event.get("DIARY_AUDIT_REASON") == "merge_completed"
    ] or None)
    assert completed is not None
    assert completed[-1]["DIARY_AUDIT_REPOSITORY"] == repo
    assert completed[-1]["DIARY_AUDIT_EVENT_ID"] == queued
    assert completed[-1]["DIARY_AUDIT_BASE_OID"] == base
