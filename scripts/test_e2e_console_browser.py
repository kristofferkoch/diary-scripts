"""Layer 2: the owner merge console driven by a real Chromium.

Layer 1 drives the executor; this layer puts the console, its executor socket,
and a ceremony service in the path and lets a browser click the rendered forms.
It exists because three production failures were invisible to hand-built
requests: the submit button's own field, an opaque or absent ``Origin``, and a
form rendered before the ceremony.

Provisioning (skipped when unset):

    E2E_PLAYWRIGHT_CORE=/path/to/playwright-core/index.mjs
    E2E_CHROMIUM=/path/to/chrome

No network is used; the browser talks to loopback only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from scripts.e2e import browser, harness

SESSION = browser.SESSION_COOKIE
TRANSACTION = browser.TRANSACTION_COOKIE
DRIVER = Path(__file__).with_name("e2e") / "browser" / "driver.mjs"


def _provisioned() -> tuple[str, str]:
    core = os.environ.get("E2E_PLAYWRIGHT_CORE", "")
    chromium = os.environ.get("E2E_CHROMIUM", "")
    if not core or not chromium or not Path(core).is_file() or not Path(chromium).is_file():
        pytest.skip(
            "browser layer needs E2E_PLAYWRIGHT_CORE and E2E_CHROMIUM (see scripts/docs/e2e-harness.md)"
        )
    return core, chromium


@pytest.fixture
def stack(tmp_path: Path) -> Generator[browser.BrowserStack]:
    built = browser.start(tmp_path)
    try:
        yield built
    finally:
        built.close()
        built.harness_stack.scratch.cleanup()


def run_browser(
    stack: browser.BrowserStack,
    session: str,
    steps: list[dict[str, object]],
    *,
    transaction: str | None = None,
) -> dict[str, str]:
    core, chromium = _provisioned()
    cookies: list[dict[str, str]] = [
        {"name": SESSION, "value": session, "url": stack.origin},
    ]
    if transaction:
        cookies.append({
            "name": TRANSACTION, "value": f"{transaction}.{transaction}",
            "domain": "127.0.0.1", "path": "/lock/merge",
        })
    job = {
        "origin": stack.origin,
        "cookies": cookies,
        "steps": steps,
        "timeoutMs": 8000,
    }
    # The suite's conftest points XDG_CONFIG_HOME at a nonexistent path for
    # hermetic Python config; Chromium needs a real one to start, so give the
    # browser its own scratch XDG tree.
    xdg = stack.harness_stack.scratch.directory("xdg")
    env = {
        **os.environ,
        "PLAYWRIGHT_CORE": core,
        "CHROMIUM_PATH": chromium,
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_DATA_HOME": str(xdg / "data"),
    }
    for name in ("config", "cache", "data"):
        (xdg / name).mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["node", str(DRIVER), json.dumps(job)],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def merge_events(stack: browser.BrowserStack) -> list[dict[str, str]]:
    return [
        event for event in stack.harness_stack.audit.events()
        if event.get("DIARY_AUDIT_EVENT") == "merge_console.decision"
    ]


def wait_reason(stack: browser.BrowserStack, reason: str) -> dict[str, str]:
    found = harness.wait_for(
        lambda: next(
            (event for event in merge_events(stack)
             if event.get("DIARY_AUDIT_REASON") == reason),
            None,
        ),
        timeout=5.0,
    )
    assert found is not None, [event.get("DIARY_AUDIT_REASON") for event in merge_events(stack)]
    return found


def head(paths) -> str:
    return harness.git(paths.approved, "rev-parse", "HEAD")


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_rendered_form_submits_the_button_field(
    stack: browser.BrowserStack, repo: str,
) -> None:
    """The browser submits the named queue, exact pair, and button field."""
    paths = stack.harness_stack.paths_by_repo[repo]
    queued = harness.enqueue_change(paths)
    base = head(paths)
    session = stack.session()
    transaction = stack.preauthorize(repo, "merge", queued, base)

    result = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/?repo=" + repo}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )

    assert "Merged" in result["text"]
    assert head(paths) == queued
    event = wait_reason(stack, "merge_completed")
    assert event["DIARY_AUDIT_REPOSITORY"] == repo
    assert event["DIARY_AUDIT_EVENT_ID"] == queued
    assert event["DIARY_AUDIT_BASE_OID"] == base


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_page_names_the_repository_and_exact_reviewed_pair(
    stack: browser.BrowserStack, repo: str,
) -> None:
    paths = stack.harness_stack.paths_by_repo[repo]
    queued = harness.enqueue_change(paths)
    base = head(paths)

    result = run_browser(
        stack, stack.session(), [{"goto": stack.origin + "/lock/merge/?repo=" + repo}],
    )

    assert f"Queue: {repo}" in result["text"]
    assert queued in result["text"]
    assert base in result["text"]


@pytest.mark.parametrize(
    ("repo", "grant_repo"), [("scripts", "diary"), ("diary", "scripts")],
)
def test_cross_queue_grant_is_refused_by_the_browser_path(
    stack: browser.BrowserStack, repo: str, grant_repo: str,
) -> None:
    paths = stack.harness_stack.paths_by_repo[repo]
    queued = harness.enqueue_change(paths)
    base = head(paths)
    transaction = stack.preauthorize(grant_repo, "merge", queued, base)

    result = run_browser(
        stack, stack.session(),
        [{"goto": stack.origin + "/lock/merge/?repo=" + repo}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )

    assert "Fresh authorization required" in result["text"]
    assert head(paths) == base
    assert merge_events(stack) == []


def test_opaque_origin_is_accepted(stack: browser.BrowserStack) -> None:
    """Firefox sends Origin: null on a same-origin form post."""
    queued = harness.enqueue_change(stack.harness_stack.scripts)
    session = stack.session()
    transaction = stack.preauthorize("scripts", "merge", queued, head(stack.harness_stack.scripts))
    stack.proxy.origin_mode = "null"

    result = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/"}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )

    assert "Merged" in result["text"]
    assert head(stack.harness_stack.scripts) == queued


def test_foreign_origin_is_refused(stack: browser.BrowserStack) -> None:
    """A concrete mismatching origin still fails closed."""
    queued = harness.enqueue_change(stack.harness_stack.scripts)
    before = head(stack.harness_stack.scripts)
    session = stack.session()
    transaction = stack.preauthorize("scripts", "merge", queued, before)
    stack.proxy.origin_mode = "evil"

    result = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/"}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )

    assert "Refused" in result["text"]
    assert head(stack.harness_stack.scripts) == before
    assert merge_events(stack) == []


def test_two_click_ceremony_publishes_once(stack: browser.BrowserStack) -> None:
    """Merge → sign-in form → back to the review → Merge executes once."""
    queued = harness.enqueue_change(stack.harness_stack.scripts)
    session = stack.session()

    result = run_browser(
        stack, session,
        [
            {"goto": stack.origin + "/lock/merge/"},
            {"wait": 2500},
            {"click": 'button[value="merge"]'},
            {"click": 'form button[type="submit"]'},
            {"click": 'button[value="merge"]'},
        ],
    )

    assert "Merged" in result["text"]
    assert head(stack.harness_stack.scripts) == queued
    events = harness.wait_for(lambda: merge_events(stack) or None, timeout=5.0) or []
    assert [event["DIARY_AUDIT_REASON"] for event in events].count("merge_completed") == 1


def test_stale_form_re_renders_instead_of_refusing(stack: browser.BrowserStack) -> None:
    """A form rendered before the ceremony is stale, not an attack."""
    harness.enqueue_change(stack.harness_stack.scripts)
    first = stack.session()
    second = stack.session()

    result = run_browser(
        stack, first,
        [
            {"goto": stack.origin + "/lock/merge/"},
            {"setCookie": {"name": SESSION, "value": second, "domain": "127.0.0.1", "path": "/"}},
            {"click": 'button[value="merge"]'},
        ],
    )

    assert "expired before it was submitted" in result["text"]
    assert "Submission" not in result["text"]
    assert merge_events(stack) == []


def test_replayed_grant_cannot_publish_twice(stack: browser.BrowserStack) -> None:
    """The grant is single use: a second decision needs a new ceremony."""
    first = harness.enqueue_change(stack.harness_stack.scripts, "one.txt", "one\n")
    session = stack.session()
    transaction = stack.preauthorize("scripts", "merge", first, head(stack.harness_stack.scripts))
    merged = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/"}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )
    assert "Merged" in merged["text"]

    second = harness.enqueue_change(stack.harness_stack.scripts, "two.txt", "two\n")
    replay = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/"}, {"click": 'button[value="merge"]'}],
        transaction=transaction,
    )

    assert "Fresh authorization required" in replay["text"]
    assert head(stack.harness_stack.scripts) == first
    assert harness.git(stack.harness_stack.scripts.queue, "rev-parse", "HEAD") == second


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_deny_publishes_nothing_and_audits(stack: browser.BrowserStack, repo: str) -> None:
    paths = stack.harness_stack.paths_by_repo[repo]
    queued = harness.enqueue_change(paths)
    before = head(paths)
    session = stack.session()
    transaction = stack.preauthorize(repo, "deny", queued, before)

    result = run_browser(
        stack, session,
        [{"goto": stack.origin + "/lock/merge/?repo=" + repo}, {"click": 'button[value="deny"]'}],
        transaction=transaction,
    )

    assert "Denied" in result["text"]
    assert head(paths) == before
    assert harness.git(paths.queue, "rev-parse", "HEAD") == queued
    assert harness.git(paths.integration, "rev-parse", "HEAD") == before
    event = wait_reason(stack, "owner_denied")
    assert event["DIARY_AUDIT_REPOSITORY"] == repo
    assert event["DIARY_AUDIT_EVENT_ID"] == queued
    assert event["DIARY_AUDIT_BASE_OID"] == before


def test_stale_pair_costs_a_re_review_not_a_new_ceremony(
    stack: browser.BrowserStack,
) -> None:
    """D3: the grant must not be spent when the queue moved after the review."""
    queued = harness.enqueue_change(stack.harness_stack.scripts)
    base = head(stack.harness_stack.scripts)
    session = stack.session()
    transaction = stack.preauthorize("scripts", "merge", queued, base)
    # The GET renders the reviewed pair; the decision POST changes the queue
    # first, so the form and the live queue disagree exactly as they would if
    # the owner left the page open.
    def change_queue() -> None:
        harness.enqueue_change(stack.harness_stack.scripts, "later.txt", "later\n")

    stack.proxy.before_decide = change_queue

    result = run_browser(
        stack, session,
        [
            {"goto": stack.origin + "/lock/merge/"},
            {"click": 'button[value="merge"]'},
        ],
        transaction=transaction,
    )

    assert "changed after you reviewed" in result["text"]
    assert stack.grants.consumed == [], "the one-time grant was spent on a refused decision"
    assert head(stack.harness_stack.scripts) == base


def test_expired_ceremony_renders_a_page_not_json(
    stack: browser.BrowserStack, tmp_path: Path,
) -> None:
    """The fresh service answers the browser with a page and a way back."""
    from scripts import diary_fresh_unlock as fresh

    config = fresh.Config(
        owner_username=browser.OWNER,
        public_origin=stack.origin,
        authelia_backend=stack.authelia.url,
        session_cookie=SESSION,
        transaction_ttl=300,
    )
    server = fresh.FreshUnlockServer(
        ("127.0.0.1", 0), config, stack.scratch / "fresh-return.sqlite3",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stack.proxy.return_port = server.server_port
        session = stack.session()
        result = run_browser(
            stack, session, [{"goto": stack.origin + "/lock/merge/return"}],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "Authorization not accepted" in result["text"]
    assert "Nothing was merged" in result["text"]
    assert "Return to the review" in result["text"]
