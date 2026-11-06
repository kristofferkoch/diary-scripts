"""End-to-end protocol tests for the owner merge console, on scratch state.

These run the real audit daemon (handler, dedup store, native-journal writer) and
the real executor decide path against scratch Git repositories. No root, no
/etc, no system journal, no production queue: see ``scripts/e2e/harness.py`` for
the isolation contract.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from scripts.e2e import harness

QUEUE = "a" * 40
BASE = "b" * 40


def decision_event() -> dict[str, str]:
    return {
        "event": "merge_console.decision",
        "component": "diary-lock-ui",
        "operation": "merge",
        "decision": "allow",
        "lock_state": "locked",
        "reason": "owner_authorized",
        "event_id": QUEUE,
        "base_oid": BASE,
        "repository": "diary",
    }


@pytest.fixture
def stack(tmp_path: Path) -> Generator[harness.Stack]:
    built = harness.build_stack(tmp_path)
    try:
        yield built
    finally:
        built.close()
        built.scratch.cleanup()


def decision_events(handle: harness.AuditHandle) -> list[dict[str, str]]:
    return [
        event for event in handle.events()
        if event.get("DIARY_AUDIT_EVENT") == "merge_console.decision"
    ]


def wait_events(handle: harness.AuditHandle, count: int = 1) -> list[dict[str, str]]:
    def ready():
        events = decision_events(handle)
        return events if len(events) >= count else None

    return harness.wait_for(ready) or []


def base_oid(paths) -> str:
    return harness.git(paths.approved, "rev-parse", "master")


def test_scratch_root_refuses_to_escape(tmp_path: Path) -> None:
    scratch = harness.Scratch.create(tmp_path)
    try:
        with pytest.raises(harness.HarnessError):
            scratch.path("..", "escape")
        assert harness.root_escapes(Path("/home/kek/diary/AGENTS.md"), scratch.root)
        assert not harness.root_escapes(scratch.path("inside"), scratch.root)
    finally:
        scratch.cleanup()


def test_audit_daemon_journals_the_decision_repository(
    stack: harness.Stack,
) -> None:
    """The production crash: `repository` reached the validator but not the journal."""
    reply = stack.audit.write(decision_event())

    assert reply["ok"] is True
    recorded = wait_events(stack.audit)[-1]
    assert recorded["DIARY_AUDIT_REPOSITORY"] == "diary"
    assert recorded["DIARY_AUDIT_EVENT_ID"] == QUEUE
    assert recorded["DIARY_AUDIT_BASE_OID"] == BASE
    assert recorded["DIARY_AUDIT_REASON"] == "owner_authorized"


def repository_state(paths) -> dict[str, str]:
    origin = harness.git(paths.approved, "remote", "get-url", "origin")
    return {
        "queue": harness.git(paths.queue, "rev-parse", "HEAD"),
        "approved": harness.git(paths.approved, "rev-parse", "HEAD"),
        "integration": harness.git(paths.integration, "rev-parse", "HEAD"),
        "remote": harness.git(Path(origin), "rev-parse", "master"),
    }


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_snapshot_binds_the_repository_and_exact_reviewed_pair(
    stack: harness.Stack, repo: str,
) -> None:
    from scripts import diary_merge_console_service as executor

    paths = stack.paths_by_repo[repo]
    reviewed = harness.enqueue_change(paths)
    base = base_oid(paths)

    reply = executor.handle_request(
        {"cmd": "snapshot", "repo": repo},
        stack.paths_by_repo,
        stack.verifier,
        auditor=harness.decision_audit(stack.audit),
        publisher=harness.publish_without_venv,
    )

    snapshot = reply["snapshot"]
    assert snapshot["repo"] == repo
    assert snapshot["queue_oid"] == reviewed
    assert snapshot["base_oid"] == base
    assert "queued" in snapshot["diff"]


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_merge_publishes_the_scratch_queue(stack: harness.Stack, repo: str) -> None:
    paths = stack.paths_by_repo[repo]
    stack.verifier.repo = repo
    reviewed = harness.enqueue_change(paths)
    base = base_oid(paths)

    reply = stack.decide(repo, "merge", reviewed, base)

    assert reply == {"ok": True, "action": "merge", "outcome": "merged"}
    assert repository_state(paths) == {
        "queue": reviewed,
        "approved": reviewed,
        "integration": reviewed,
        "remote": reviewed,
    }

    events = wait_events(stack.audit, 2)
    reasons = [event["DIARY_AUDIT_REASON"] for event in events]
    assert reasons == ["owner_authorized", "merge_completed"]
    for event in events:
        assert event["DIARY_AUDIT_REPOSITORY"] == repo
        assert event["DIARY_AUDIT_EVENT_ID"] == reviewed
        assert event["DIARY_AUDIT_BASE_OID"] == base


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_stale_queue_oid_is_refused_without_publication(
    stack: harness.Stack, repo: str,
) -> None:
    paths = stack.paths_by_repo[repo]
    stack.verifier.repo = repo
    harness.enqueue_change(paths)
    base = base_oid(paths)
    before = repository_state(paths)

    reply = stack.decide(repo, "merge", base, base)

    assert reply["error"] == "queue_changed"
    assert repository_state(paths) == before
    events = wait_events(stack.audit)
    assert [event["DIARY_AUDIT_REASON"] for event in events] == ["queue_changed"]
    assert events[0]["DIARY_AUDIT_REPOSITORY"] == repo
    assert events[0]["DIARY_AUDIT_EVENT_ID"] == base
    assert events[0]["DIARY_AUDIT_BASE_OID"] == base


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_stale_base_oid_is_refused_without_publication(
    stack: harness.Stack, repo: str,
) -> None:
    paths = stack.paths_by_repo[repo]
    stack.verifier.repo = repo
    reviewed = harness.enqueue_change(paths)
    reviewed_base = base_oid(paths)
    harness.commit_file(paths.approved, "base-only.txt", "base moved\n")
    harness.git(paths.approved, "push", "origin", "master")
    harness.git(paths.queue, "fetch", "origin", "master")
    before = repository_state(paths)

    reply = stack.decide(repo, "merge", reviewed, reviewed_base)

    assert reply["error"] == "base_changed"
    assert repository_state(paths) == before
    events = wait_events(stack.audit)
    assert [event["DIARY_AUDIT_REASON"] for event in events] == ["base_changed"]
    assert events[0]["DIARY_AUDIT_REPOSITORY"] == repo
    assert events[0]["DIARY_AUDIT_EVENT_ID"] == reviewed
    assert events[0]["DIARY_AUDIT_BASE_OID"] == reviewed_base


def test_agent_supplied_oids_and_transaction_have_no_authority(
    stack: harness.Stack,
) -> None:
    reviewed = harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)
    before = repository_state(stack.scripts)
    stack.verifier.transaction = "owner-authorization"

    reply = stack.decide(
        "scripts", "merge", reviewed, base, transaction="agent-supplied",
    )

    assert reply["error"] == "unauthorized"
    assert repository_state(stack.scripts) == before
    assert decision_events(stack.audit) == []


def test_audit_outage_before_merge_prevents_publication(stack: harness.Stack) -> None:
    reviewed = harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)
    before = repository_state(stack.scripts)

    def unavailable(_value: object, _peer: dict[str, int]) -> dict:
        raise OSError("injected audit outage")

    stack.audit.service.write = unavailable  # ty: ignore[invalid-assignment]
    reply = stack.decide("scripts", "merge", reviewed, base)

    assert reply["error"] == "not_published"
    assert repository_state(stack.scripts) == before
    assert decision_events(stack.audit) == []


def test_completion_audit_outage_reports_published_without_retry(
    stack: harness.Stack,
) -> None:
    reviewed = harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)
    original_write = stack.audit.service.write
    writes = 0

    def fail_completion(value: object, peer: dict[str, int]) -> dict:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected completion audit outage")
        return original_write(value, peer)

    stack.audit.service.write = fail_completion  # ty: ignore[invalid-assignment]
    reply = stack.decide("scripts", "merge", reviewed, base)

    assert reply["error"] == "published_unconfirmed"
    assert writes == 2, "the console must not retry after an uncertain completion audit"
    assert repository_state(stack.scripts) == {
        "queue": reviewed,
        "approved": reviewed,
        "integration": reviewed,
        "remote": reviewed,
    }
    events = wait_events(stack.audit)
    assert [event["DIARY_AUDIT_REASON"] for event in events] == ["owner_authorized"]
    assert events[0]["DIARY_AUDIT_EVENT_ID"] == reviewed
    assert events[0]["DIARY_AUDIT_BASE_OID"] == base


def test_local_owner_approval_fallback_still_publishes_exact_oid(
    stack: harness.Stack,
) -> None:
    reviewed = harness.enqueue_change(stack.scripts)

    assert harness.publish_without_venv(reviewed, stack.scripts) is True

    assert repository_state(stack.scripts) == {
        "queue": reviewed,
        "approved": reviewed,
        "integration": reviewed,
        "remote": reviewed,
    }
    assert decision_events(stack.audit) == []


@pytest.mark.parametrize("repo", ["scripts", "diary"])
def test_deny_changes_no_repository_and_audits_the_exact_pair(
    stack: harness.Stack, repo: str,
) -> None:
    paths = stack.paths_by_repo[repo]
    stack.verifier.repo = repo
    reviewed = harness.enqueue_change(paths)
    base = base_oid(paths)
    before = repository_state(paths)

    reply = stack.decide(repo, "deny", reviewed, base)

    assert reply == {"ok": True, "action": "deny", "outcome": "denied"}
    assert repository_state(paths) == before
    events = wait_events(stack.audit)
    assert [event["DIARY_AUDIT_REASON"] for event in events] == ["owner_denied"]
    assert events[0]["DIARY_AUDIT_REPOSITORY"] == repo
    assert events[0]["DIARY_AUDIT_EVENT_ID"] == reviewed
    assert events[0]["DIARY_AUDIT_BASE_OID"] == base


@pytest.mark.parametrize(
    ("repo", "grant_repo"), [("scripts", "diary"), ("diary", "scripts")],
)
def test_grant_cannot_decide_the_other_queue(
    stack: harness.Stack, repo: str, grant_repo: str,
) -> None:
    paths = stack.paths_by_repo[repo]
    stack.verifier.repo = grant_repo
    reviewed = harness.enqueue_change(paths)
    base = base_oid(paths)
    before = repository_state(paths)

    reply = stack.decide(repo, "merge", reviewed, base)

    assert reply["error"] == "unauthorized"
    assert repository_state(paths) == before
    assert decision_events(stack.audit) == []


def test_audit_restart_keeps_state_and_accepts_writes(
    stack: harness.Stack,
) -> None:
    first = stack.audit.write(decision_event())
    assert first["ok"] is True

    state = stack.audit.state_path
    stack.audit.close()
    stack.audit = harness.start_audit(stack.scratch, state=state)

    second = stack.audit.write(decision_event())
    assert second["ok"] is True
    assert second["count"] == 2

    # A duplicate inside the suppression window updates counters without a new
    # journal datagram; a distinct event must still be emitted after restart.
    other = dict(decision_event(), event_id="c" * 40)
    third = stack.audit.write(other)

    assert third["ok"] is True
    assert wait_events(stack.audit)[-1]["DIARY_AUDIT_EVENT_ID"] == "c" * 40


def test_failed_publication_reports_the_repository_state(stack: harness.Stack) -> None:
    """A publisher failure must say whether anything actually got published."""

    reviewed = harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)

    def failing(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("injected publication failure")

    reply = stack.decide(
        "scripts", "merge", reviewed, base, publisher=failing,
    )

    assert reply["error"] == "not_published"
    assert harness.git(stack.scripts.approved, "rev-parse", "HEAD") == base
    reasons = [event["DIARY_AUDIT_REASON"] for event in wait_events(stack.audit, 2)]
    assert reasons == ["owner_authorized", "merge_failed_not_published"]
