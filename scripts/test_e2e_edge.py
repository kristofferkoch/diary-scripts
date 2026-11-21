"""Layer 3: real Caddy and passkey authentication in front of the merge console."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.e2e import edge, harness


def head(paths) -> str:
    return harness.git(paths.approved, "rev-parse", "HEAD")

pytestmark = pytest.mark.skipif(
    not Path(edge.CADDY).is_file(),
    reason="the real caddy binary is not installed",
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


def test_anonymous_redirects_to_passkey_login(stack: edge.EdgeStack) -> None:
    status, headers, body = stack.request("GET", "/lock/merge/", headers=BROWSER)

    assert status in {302, 303}
    assert "/lock/auth/" in headers.get("Location", "")
    assert b"Queued commits" not in body


def test_invalid_session_is_still_denied(stack: edge.EdgeStack) -> None:
    status, headers, _ = stack.request(
        "GET", "/lock/merge/", cookie=f"{edge.SESSION_COOKIE}=invalid", headers=BROWSER,
    )

    assert status in {302, 303}, (status, headers)


def test_login_page_has_no_password_form(stack: edge.EdgeStack) -> None:
    status, _, body = stack.request("GET", "/lock/auth/")

    assert status == 200
    assert b"Use passkey" in body
    assert b"password-textfield" not in body


BROWSER_ENV = ("E2E_PLAYWRIGHT_CORE", "E2E_CHROMIUM")


def _browser_ready() -> bool:
    return all(os.environ.get(name) for name in BROWSER_ENV)


@pytest.mark.skipif(not _browser_ready(), reason="browser tooling is not provisioned")
def test_full_passkey_reaches_the_console(stack: edge.EdgeStack) -> None:
    """A real passkey session, driven through Caddy, opens the page."""
    result = edge.run_driver(stack)

    console = [step for step in result["steps"] if step["step"] == "console"][-1]
    assert console["url"].endswith("/lock/merge/"), result
    assert "Queued commits" in console["text"] or "Nothing queued" in console["text"]


@pytest.mark.skipif(not _browser_ready(), reason="browser tooling is not provisioned")
@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_browser_decision_through_the_real_edge_publishes_once(
    stack: edge.EdgeStack, repo: str,
) -> None:
    """Both queues cross Caddy, passkey auth, the console, grant, and publication."""
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
