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
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
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

    worktrees = tmp_path / "diary-worktrees"
    source = worktrees / "_sources" / "diary-scripts"
    run("git", "clone", "--no-hardlinks", str(integration), str(source))
    run("git", "config", "user.name", "Queue Test", cwd=source)
    run("git", "config", "user.email", "queue@example.invalid", cwd=source)

    approved = tmp_path / "diary-scripts-approved"
    run("git", "clone", "--no-hardlinks", str(integration), str(approved))
    remote = tmp_path / "origin.git"
    run("git", "init", "--bare", str(remote))
    run("git", "remote", "set-url", "origin", str(remote), cwd=approved)
    run("git", "push", "-u", "origin", "master", cwd=approved)

    return amq.QueuePaths(
        integration=integration,
        source=source,
        approved=approved,
        queue=tmp_path / "diary-scripts-automatic",
        worktrees=worktrees,
        lock=tmp_path / "queue.lock",
    )


def source_branch(paths: amq.QueuePaths, branch: str = "dev/change") -> Path:
    source = paths.worktrees / branch.split("/", 1)[1]
    run("git", "worktree", "add", str(source), "-b", branch, "master", cwd=paths.source)
    commit(source, "queued.txt", "not yet approved\n")
    return source


def reviewed(paths: amq.QueuePaths) -> str:
    return run("git", "rev-parse", "HEAD", cwd=paths.queue)


def test_source_is_a_standalone_non_hardlinked_clone(paths: amq.QueuePaths):
    assert (paths.source / ".git").is_dir()
    source_objects = paths.source / ".git" / "objects"
    integration_objects = paths.integration / ".git" / "objects"
    source_object = next(path for path in source_objects.glob("[0-9a-f][0-9a-f]/*") if path.is_file())
    integration_object = integration_objects / source_object.relative_to(source_objects)
    assert source_object.stat().st_ino != integration_object.stat().st_ino


def test_queue_does_not_touch_approved_integration_or_github(paths: amq.QueuePaths):
    source = source_branch(paths)
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    integration_head = run("git", "rev-parse", "master", cwd=paths.integration)

    assert amq.queue_change("dev/change", paths) is True

    assert not source.exists(), "the cleanly queued source worktree is removed"
    branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/dev/change"],
        cwd=paths.source,
    )
    assert branch.returncode != 0, "queue cleanup deletes the source branch"
    assert (paths.queue / ".git").is_dir(), "queue metadata is standalone"
    assert (paths.queue / "queued.txt").read_text() == "not yet approved\n"
    for repo in (paths.approved, paths.integration):
        assert not (repo / "queued.txt").exists()
        assert run("git", "rev-parse", "master", cwd=repo) == approved_head
    assert integration_head == approved_head
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


def test_diary_client_names_the_repository_and_explicit_oid(paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch):
    oid = "b" * 40
    seen: list[str] = []
    monkeypatch.setattr(amq, "diary_paths", lambda: paths)
    monkeypatch.setattr(amq, "request", lambda payload: seen.append(payload) or "RESULT ok 0\n")

    assert amq.approve_main(["--repo", "diary", oid]) == 0
    assert seen == [f"approve diary {oid}"]


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
    run("git", "worktree", "add", str(outside), "-b", "dev/outside", "master", cwd=paths.source)
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


def empty_source(paths: amq.QueuePaths, branch: str = "dev/diary") -> Path:
    source = paths.worktrees / branch.split("/", 1)[1]
    run("git", "worktree", "add", str(source), "-b", branch, "master", cwd=paths.source)
    return source


def test_diary_regular_memory_change_publishes_without_owner_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "routine notes\n")

    assert amq.publish_diary_unattended("dev/diary", paths) is True

    assert not source.exists()
    branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/dev/diary"],
        cwd=paths.source,
    )
    assert branch.returncode != 0, "unattended cleanup deletes the source branch"
    for repo in (paths.approved, paths.integration):
        assert (repo / "memory/2026-10-31.md").read_text() == "routine notes\n"


@pytest.mark.parametrize("name", [
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/skills/example/SKILL.md",
    "dsh/agent-uid/agent-merge",
    "systemd/install-user",
    "sync/diary-sync-broker.service",
    "caddy/Caddyfile",
    "config/local.toml",
])
def test_diary_instruction_executable_and_service_paths_require_review(paths: amq.QueuePaths, name: str):
    source = empty_source(paths)
    commit(source, name, "agent-controlled change\n")
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(paths.source, oid) == [name]


def test_diary_sensitive_change_cannot_publish_without_exact_oid_approval(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "AGENTS.md", "unreviewed instructions\n")
    commit(source, "sync/diary-sync-broker.service", "unreviewed service control\n")
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)

    with pytest.raises(amq.ReviewRequired, match="AGENTS.md"):
        amq.publish_diary_unattended("dev/diary", paths)
    assert run("git", "rev-parse", "master", cwd=paths.approved) == approved_head
    assert source.exists(), "a change still needing review is not cleaned up"

    assert amq.queue_change("dev/diary", paths, repo="diary") is True
    oid = reviewed(paths)
    assert not (paths.approved / "AGENTS.md").exists()
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    assert run("git", "--git-dir", remote, "rev-parse", "master") == approved_head

    assert amq.approve_queue(oid, paths, sync_environment=False) is True
    assert (paths.approved / "AGENTS.md").read_text() == "unreviewed instructions\n"
    assert run("git", "--git-dir", remote, "rev-parse", "master") == oid


def test_diary_executable_memory_file_requires_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/run-me", "ordinary file\n")
    (source / "memory/run-me").chmod(0o755)
    run("git", "add", "memory/run-me", cwd=source)
    run("git", "commit", "-m", "make executable", cwd=source)
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(paths.source, oid) == ["memory/run-me"]


def test_diary_symlink_memory_file_requires_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    (source / "memory").mkdir()
    (source / "memory/link").symlink_to("../AGENTS.md")
    run("git", "add", "memory/link", cwd=source)
    run("git", "commit", "-m", "add symlink", cwd=source)
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(paths.source, oid) == ["memory/link"]


def test_diary_publication_pins_the_fetched_oid(paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "reviewed automatically\n")
    original = amq.diary_review_required

    def swap_branch_after_fetch(repo: Path, oid: str) -> list[str]:
        result = original(repo, oid)
        commit(source, "AGENTS.md", "too late to swap this in\n")
        return result

    monkeypatch.setattr(amq, "diary_review_required", swap_branch_after_fetch)
    assert amq.publish_diary_unattended("dev/diary", paths) is True

    assert not (paths.approved / "AGENTS.md").exists()
    assert (paths.approved / "memory/2026-10-31.md").exists()


def test_diary_agent_controlled_source_master_is_not_a_trust_base(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "safe on its own\n")
    attacker = empty_source(paths, "dev/attacker")
    commit(attacker, "AGENTS.md", "source refs are untrusted\n")
    attacker_oid = run("git", "rev-parse", "HEAD", cwd=attacker)
    integration_head = run("git", "rev-parse", "master", cwd=paths.integration)
    run("git", "update-ref", "refs/heads/master", attacker_oid, cwd=paths.source)

    assert amq.publish_diary_unattended("dev/diary", paths) is True

    assert run("git", "rev-parse", "master", cwd=paths.source) == attacker_oid
    assert not (paths.approved / "AGENTS.md").exists()
    assert not (paths.integration / "AGENTS.md").exists()
    assert run("git", "rev-parse", "master^", cwd=paths.integration) == integration_head


def test_source_change_cannot_bypass_a_dirty_integration_guard(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "must not overwrite local work\n")
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    (paths.integration / "README.md").write_text("owner work in progress\n")

    with pytest.raises(amq.QueueError, match="integration.*uncommitted"):
        amq.publish_diary_unattended("dev/diary", paths)

    assert source.exists()
    assert run("git", "rev-parse", "master", cwd=paths.approved) == approved_head
    assert (paths.integration / "README.md").read_text() == "owner work in progress\n"


@pytest.mark.parametrize("operation", ["queue", "unattended"])
def test_malicious_source_git_config_cannot_execute(paths: amq.QueuePaths, operation: str):
    marker = paths.source.parent / f"{operation}-config-executed"
    if operation == "queue":
        source_branch(paths)
    else:
        source = empty_source(paths)
        commit(source, "memory/2026-10-31.md", "safe memory change\n")
    run("git", "config", "uploadpack.packObjectsHook", f"touch {marker}", cwd=paths.source)
    run("git", "config", "core.fsmonitor", f"touch {marker}", cwd=paths.source)

    if operation == "queue":
        assert amq.queue_change("dev/change", paths) is True
    else:
        assert amq.publish_diary_unattended("dev/diary", paths) is True

    assert not marker.exists()
