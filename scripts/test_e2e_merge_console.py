"""End-to-end protocol tests for the owner merge console, on scratch state.

These run the real audit daemon (handler, dedup store, native-journal writer) and
the real executor decide path against scratch Git repositories. No root, no
/etc, no system journal, no production queue: see ``scripts/e2e/harness.py`` for
the isolation contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import agent_merge_console as console
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
def stack(tmp_path: Path) -> harness.Stack:
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


def test_merge_publishes_the_scratch_queue(stack: harness.Stack) -> None:
    reviewed = harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)

    reply = stack.decide("scripts", "merge", reviewed, base)

    assert reply == {"ok": True, "action": "merge", "outcome": "merged"}
    assert harness.git(stack.scripts.approved, "rev-parse", "HEAD") == reviewed
    origin = harness.git(stack.scripts.approved, "remote", "get-url", "origin")
    assert harness.git(Path(origin), "rev-parse", "master") == reviewed
    assert harness.git(stack.scripts.integration, "rev-parse", "HEAD") == reviewed

    events = wait_events(stack.audit, 2)
    reasons = [event["DIARY_AUDIT_REASON"] for event in events]
    assert reasons == ["owner_authorized", "merge_completed"]
    for event in events:
        assert event["DIARY_AUDIT_REPOSITORY"] == "scripts"
        assert event["DIARY_AUDIT_EVENT_ID"] == reviewed
        assert event["DIARY_AUDIT_BASE_OID"] == base


def test_stale_queue_oid_is_refused_without_publication(stack: harness.Stack) -> None:
    harness.enqueue_change(stack.scripts)
    base = base_oid(stack.scripts)

    reply = stack.decide("scripts", "merge", base, base)

    assert reply["error"] == "queue_changed"
    assert harness.git(stack.scripts.approved, "rev-parse", "HEAD") == base
    reasons = [event["DIARY_AUDIT_REASON"] for event in wait_events(stack.audit)]
    assert reasons == ["queue_changed"]


def test_scripts_grant_cannot_decide_the_diary_queue(stack: harness.Stack) -> None:
    stack.verifier.repo = "scripts"
    reviewed = harness.enqueue_change(stack.diary, "board.md", "record\n")
    base = base_oid(stack.diary)

    reply = stack.decide("diary", "merge", reviewed, base)

    assert reply["error"] == "unauthorized"
    assert harness.git(stack.diary.approved, "rev-parse", "HEAD") == base
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
