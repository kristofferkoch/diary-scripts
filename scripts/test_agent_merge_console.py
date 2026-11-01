"""Isolated security and behavior tests for the owner merge console."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import agent_merge_console as console
from scripts import agent_merge_queue as queue


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[console.DecisionRecord] = []

    def record(self, record: console.DecisionRecord) -> None:
        self.records.append(record)


class FailingAudit:
    def record(self, record: console.DecisionRecord) -> None:
        raise OSError("injected audit failure")


@pytest.fixture
def environment(tmp_path: Path) -> tuple[queue.QueuePaths, Path, Path]:
    approved = tmp_path / "approved"
    approved.mkdir()
    git(approved, "init", "-qb", "master")
    git(approved, "config", "user.name", "Console Test")
    git(approved, "config", "user.email", "console@example.invalid")
    base = commit(approved, "README.md", "approved\n", "base")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(approved, "remote", "add", "origin", str(remote))
    git(approved, "push", "-qu", "origin", "master")

    integration = tmp_path / "integration"
    subprocess.run(["git", "clone", "-q", str(approved), str(integration)], check=True)
    queued = tmp_path / "queue"
    subprocess.run(["git", "clone", "-q", str(approved), str(queued)], check=True)
    git(queued, "config", "user.name", "Console Test")
    git(queued, "config", "user.email", "console@example.invalid")
    git(queued, "checkout", "-qb", queue.QUEUE_BRANCH)
    commit(queued, "queued.py", "VALUE = 1\n", "queued change")
    paths = queue.QueuePaths(
        integration=integration,
        approved=approved,
        queue=queued,
        worktrees=tmp_path / "worktrees",
        lock=tmp_path / "queue.lock",
    )
    assert git(approved, "rev-parse", "master") == base
    return paths, remote, tmp_path / "reports"


def owner_grant(action: console.DecisionAction, oid: str) -> console.OwnerGrant:
    return console.OwnerGrant("owner", f"{action}:{oid}")


def make_console(
    paths: queue.QueuePaths,
    reports: Path,
    auditor: console.DecisionAuditor | None = None,
    publisher: console.Publisher = queue.approve_queue,
) -> console.MergeConsole:
    return console.MergeConsole(
        paths,
        owner_grant,
        auditor or FakeAudit(),
        report_dir=reports,
        publisher=publisher,
    )


def write_report(reports: Path, oid: str, base: str, **changes: object) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    value: dict[str, object] = {
        "schema_version": 1,
        "repository": "diary-scripts",
        "queue_oid": oid,
        "base_oid": base,
        "stale": False,
        "approval": "owner-required",
        "model": "local-test",
        "model_status": "completed",
        "model_summary": "Advisory <script>alert(1)</script>",
        "model_findings": [],
        "model_error": None,
        "prescan_findings": [],
    }
    value.update(changes)
    (reports / f"{oid}.json").write_text(json.dumps(value))


def remote_head(remote: Path) -> str:
    return subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "master"], text=True,
    ).strip()


def test_snapshot_shows_real_approved_master_diff_and_same_oid_report(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, _, reports = environment
    queued_oid = git(paths.queue, "rev-parse", "HEAD")
    approved_base = commit(paths.approved, "owner.py", "OWNER = True\n", "approved-only change")
    git(paths.queue, "fetch", "-q", str(paths.approved), "master")
    write_report(reports, queued_oid, approved_base)

    snapshot = make_console(paths, reports).snapshot()

    assert snapshot is not None
    assert snapshot.queue_oid == queued_oid
    assert snapshot.base_oid == approved_base
    assert snapshot.base_oid != git(paths.queue, "rev-parse", "master")
    assert [item.oid for item in snapshot.commits] == [queued_oid]
    assert "queued change" in snapshot.commits[0].subject
    assert "+VALUE = 1" in snapshot.diff
    assert "OWNER = True" not in snapshot.diff
    assert snapshot.advisory.status == "current"
    rendered = console.render_console(snapshot)
    assert queued_oid in rendered
    assert approved_base in rendered
    assert "+VALUE = 1" in rendered
    assert "&lt;script" in rendered
    assert "alert(1)" in rendered
    assert "<script>alert(1)</script>" not in rendered


def test_locked_owner_merge_publishes_exact_oid_and_audits(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, remote, reports = environment
    oid = git(paths.queue, "rev-parse", "HEAD")
    audit = FakeAudit()
    calls: list[tuple[console.DecisionAction, str]] = []

    def authorize(action: console.DecisionAction, reviewed: str) -> console.OwnerGrant:
        calls.append((action, reviewed))
        return owner_grant(action, reviewed)

    publisher_calls: list[str] = []

    def publish(reviewed: str, actual_paths: queue.QueuePaths) -> bool:
        publisher_calls.append(reviewed)
        return queue.approve_queue(reviewed, actual_paths, sync_environment=False)

    result = console.MergeConsole(
        paths, authorize, audit, report_dir=reports, publisher=publish, lock_state="locked",
    ).decide("merge", oid)

    assert result.outcome == "merged"
    assert calls == [("merge", oid)]
    assert publisher_calls == [oid]
    assert remote_head(remote) == oid
    assert git(paths.integration, "rev-parse", "master") == oid
    assert [item.outcome for item in audit.records] == ["owner_authorized", "merge_completed"]
    assert all(item.queue_oid == oid and item.lock_state == "locked" for item in audit.records)
    assert audit.records[-1].as_audit_event()["event_id"] == oid


def test_stale_oid_merge_fails_without_merge_or_push(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, remote, reports = environment
    reviewed = git(paths.queue, "rev-parse", "HEAD")
    approved = git(paths.approved, "rev-parse", "master")
    published = remote_head(remote)
    commit(paths.queue, "later.py", "LATER = True\n", "queue changed")
    audit = FakeAudit()

    with pytest.raises(console.QueueChanged, match="changed after review"):
        make_console(paths, reports, audit).decide("merge", reviewed)

    assert git(paths.approved, "rev-parse", "master") == approved
    assert remote_head(remote) == published
    assert not (paths.approved / "queued.py").exists()
    assert [item.outcome for item in audit.records] == ["queue_changed"]


def test_agent_session_and_supplied_oid_have_no_authority(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, remote, reports = environment
    oid = git(paths.queue, "rev-parse", "HEAD")
    published = remote_head(remote)

    def agent_session(action: console.DecisionAction, reviewed: str) -> console.OwnerGrant:
        raise console.OwnerAuthorizationRequired("agent session is not owner authorization")

    def unexpected_publish(reviewed: str, actual_paths: queue.QueuePaths) -> bool:
        pytest.fail("an agent-supplied OID reached publication")

    ui = console.MergeConsole(
        paths, agent_session, FakeAudit(), report_dir=reports, publisher=unexpected_publish,
    )
    with pytest.raises(console.OwnerAuthorizationRequired):
        ui.decide("merge", oid)

    assert remote_head(remote) == published
    assert not (paths.approved / "queued.py").exists()


def test_deny_only_records_decision_without_merge_or_push(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, remote, reports = environment
    oid = git(paths.queue, "rev-parse", "HEAD")
    before = {
        repo: git(repo, "rev-parse", "HEAD")
        for repo in (paths.queue, paths.approved, paths.integration)
    }
    published = remote_head(remote)
    audit = FakeAudit()

    def unexpected_publish(reviewed: str, actual_paths: queue.QueuePaths) -> bool:
        pytest.fail("Deny reached merge/push")

    result = console.MergeConsole(
        paths, owner_grant, audit, report_dir=reports, publisher=unexpected_publish,
    ).decide("deny", oid)

    assert result.outcome == "denied"
    assert remote_head(remote) == published
    assert before == {
        repo: git(repo, "rev-parse", "HEAD")
        for repo in (paths.queue, paths.approved, paths.integration)
    }
    assert len(audit.records) == 1
    assert audit.records[0].outcome == "owner_denied"
    event = audit.records[0].as_audit_event()
    assert event["operation"] == "deny"
    assert event["decision"] == "deny"
    assert event["event_id"] == oid


def test_audit_failure_prevents_merge(
    environment: tuple[queue.QueuePaths, Path, Path],
):
    paths, remote, reports = environment
    oid = git(paths.queue, "rev-parse", "HEAD")
    published = remote_head(remote)

    with pytest.raises(OSError, match="audit failure"):
        make_console(paths, reports, FailingAudit()).decide("merge", oid)

    assert remote_head(remote) == published
    assert not (paths.approved / "queued.py").exists()


@pytest.mark.parametrize("report_state", ["missing", "stale", "malformed", "failed"])
def test_advisory_report_never_controls_or_blocks_owner_approval(
    environment: tuple[queue.QueuePaths, Path, Path], report_state: str,
):
    paths, _, reports = environment
    oid = git(paths.queue, "rev-parse", "HEAD")
    base = git(paths.approved, "rev-parse", "master")
    if report_state != "missing":
        reports.mkdir(parents=True, exist_ok=True)
    if report_state == "malformed":
        (reports / f"{oid}.json").write_text("not JSON")
    elif report_state != "missing":
        write_report(
            reports, oid, base,
            stale=report_state == "stale",
            model_status="error" if report_state == "failed" else "completed",
        )
    published: list[str] = []

    def publish(reviewed: str, actual_paths: queue.QueuePaths) -> bool:
        published.append(reviewed)
        return True

    ui = make_console(paths, reports, publisher=publish)
    snapshot = ui.snapshot()
    assert snapshot is not None
    expected = {"missing": "missing", "stale": "stale", "malformed": "unavailable", "failed": "current"}
    assert snapshot.advisory.status == expected[report_state]

    assert ui.decide("merge", oid).outcome == "merged"
    assert published == [oid]
