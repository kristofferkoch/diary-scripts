"""Security-boundary tests for the human-approved publication queue."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import agent_merge_queue as amq


def run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    run("git", "add", name, cwd=repo)
    run("git", "commit", "-m", f"add {name}", cwd=repo)


@pytest.fixture(autouse=True)
def skip_virtualenv_install(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(amq, "sync_venv", lambda paths: None)


@pytest.fixture
def paths(tmp_path: Path) -> amq.QueuePaths:
    integration = tmp_path / "diary" / "diary-scripts"
    integration.mkdir(parents=True)
    run("git", "init", "-b", "master", cwd=integration)
    run("git", "config", "user.name", "Queue Test", cwd=integration)
    run("git", "config", "user.email", "queue@example.invalid", cwd=integration)
    commit(integration, "README.md", "approved\n")

    approved = tmp_path / "diary-scripts-approved"
    run("git", "clone", "--no-hardlinks", str(integration), str(approved))
    remote = tmp_path / "origin.git"
    run("git", "init", "--bare", str(remote))
    run("git", "remote", "set-url", "origin", str(remote), cwd=approved)
    run("git", "push", "-u", "origin", "master", cwd=approved)

    return amq.QueuePaths(
        integration=integration,
        approved=approved,
        queue=tmp_path / "diary-scripts-automatic",
        worktrees=tmp_path / "diary-worktrees",
        lock=tmp_path / "queue.lock",
    )


def source_branch(paths: amq.QueuePaths, branch: str = "dev/change") -> Path:
    source = paths.worktrees / branch.split("/", 1)[1]
    run("git", "worktree", "add", str(source), "-b", branch, "master", cwd=paths.integration)
    commit(source, "queued.txt", "not yet approved\n")
    return source


def reviewed(paths: amq.QueuePaths) -> str:
    return run("git", "rev-parse", "HEAD", cwd=paths.queue)


def test_queue_does_not_touch_approved_tree_or_github(paths: amq.QueuePaths):
    source = source_branch(paths)
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)

    assert amq.queue_change("dev/change", paths) is True

    assert not source.exists(), "the cleanly queued source worktree is removed"
    assert (paths.queue / ".git").is_dir(), "queue metadata is standalone"
    assert (paths.queue / "queued.txt").read_text() == "not yet approved\n"
    assert not (paths.approved / "queued.txt").exists()
    assert run("git", "rev-parse", "master", cwd=paths.approved) == approved_head
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    assert run("git", "--git-dir", remote, "rev-parse", "master") == approved_head


def test_owner_can_approve_exact_reviewed_oid(paths: amq.QueuePaths):
    source_branch(paths)
    amq.queue_change("dev/change", paths)
    oid = reviewed(paths)

    with pytest.raises(amq.QueueError, match="OID"):
        amq.approve_queue("wrong", paths)
    assert not (paths.approved / "queued.txt").exists()

    assert amq.approve_queue(oid, paths) is True
    for repo in (paths.approved, paths.integration, paths.queue):
        assert (repo / "queued.txt").read_text() == "not yet approved\n"
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    assert run("git", "--git-dir", remote, "rev-parse", "master") == approved_head
    assert run("git", "rev-parse", "HEAD", cwd=paths.queue) == approved_head


def test_new_queue_commit_after_review_is_rejected(paths: amq.QueuePaths):
    source_branch(paths)
    amq.queue_change("dev/change", paths)
    oid = reviewed(paths)
    second = source_branch(paths, "dev/second")
    commit(second, "second.txt", "a different commit\n")
    amq.queue_change("dev/second", paths)
    assert reviewed(paths) != oid

    with pytest.raises(amq.QueueError, match="changed after review"):
        amq.approve_queue(oid, paths)
    assert not (paths.approved / "queued.txt").exists()


def test_client_uses_only_the_explicit_reviewed_oid(paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch):
    oid = "a" * 40
    seen: list[str] = []
    monkeypatch.setattr(amq, "default_paths", lambda: paths)
    monkeypatch.setattr(amq, "request", lambda payload: seen.append(payload) or "RESULT ok 0\n")

    assert amq.approve_main([oid]) == 0
    assert seen == [f"approve scripts {oid}"]


def test_failed_push_can_be_retried_and_synchronizes_queue(paths: amq.QueuePaths):
    source_branch(paths)
    amq.queue_change("dev/change", paths)
    oid = reviewed(paths)
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    run("git", "remote", "set-url", "origin", str(remote) + ".missing", cwd=paths.approved)

    with pytest.raises(amq.QueueError, match="push"):
        amq.approve_queue(oid, paths)
    assert (paths.approved / "queued.txt").exists(), "the approved local merge already happened"

    run("git", "remote", "set-url", "origin", remote, cwd=paths.approved)
    assert amq.approve_queue(oid, paths) is False
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    assert run("git", "--git-dir", remote, "rev-parse", "master") == approved_head
    assert run("git", "rev-parse", "HEAD", cwd=paths.queue) == approved_head


def test_dirty_approved_tree_cannot_be_published_over(paths: amq.QueuePaths):
    source_branch(paths)
    amq.queue_change("dev/change", paths)
    (paths.approved / "README.md").write_text("local work\n")

    with pytest.raises(amq.QueueError, match="uncommitted"):
        amq.approve_queue(reviewed(paths), paths)
    assert (paths.approved / "README.md").read_text() == "local work\n"


def test_source_must_be_in_the_agent_worktree_root(paths: amq.QueuePaths):
    outside = paths.integration.parent / "outside"
    run("git", "worktree", "add", str(outside), "-b", "dev/outside", "master", cwd=paths.integration)
    commit(outside, "queued.txt", "outside\n")

    with pytest.raises(amq.QueueError, match="outside"):
        amq.queue_change("dev/outside", paths)
    assert not (paths.queue / "queued.txt").exists()


def test_already_queued_retries_source_cleanup(paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch):
    source = source_branch(paths)
    original = amq.remove_source

    def fail_once(branch: str, source_path: Path, *, _paths=paths) -> None:
        raise amq.QueueError("simulated cleanup failure")

    monkeypatch.setattr(amq, "remove_source", lambda _paths, branch, source_path: fail_once(branch, source_path))
    with pytest.raises(amq.QueueError, match="cleanup"):
        amq.queue_change("dev/change", paths)
    assert source.exists()

    monkeypatch.setattr(amq, "remove_source", original)
    assert amq.queue_change("dev/change", paths) is False
    assert not source.exists()


def test_peer_uid_comes_from_the_kernel_socket_credentials():
    left, right = amq.socket.socketpair()
    try:
        assert amq.peer_uid(left.fileno()) == amq.os.geteuid()
    finally:
        left.close()
        right.close()
