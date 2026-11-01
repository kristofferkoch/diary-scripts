"""Security-boundary tests for the human-approved publication queue."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

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
    review_config = tmp_path / "review.toml"
    review_config.write_text('[review]\nmodel = "local-test"\n')

    return amq.QueuePaths(
        integration=integration,
        approved=approved,
        queue=tmp_path / "diary-scripts-automatic",
        worktrees=worktrees,
        lock=tmp_path / "queue.lock",
        review_config=review_config,
    )


def source_repo(paths: amq.QueuePaths) -> Path:
    return paths.worktrees / "_sources" / "diary-scripts"


def source_branch(paths: amq.QueuePaths, branch: str = "dev/change") -> Path:
    source = paths.worktrees / branch.split("/", 1)[1]
    run("git", "worktree", "add", str(source), "-b", branch, "master", cwd=source_repo(paths))
    commit(source, "queued.txt", "not yet approved\n")
    return source


def make_bundle(paths: amq.QueuePaths, branch: str, repo: str = "scripts"):
    return amq.create_agent_bundle(source_repo(paths), paths.worktrees, repo, branch)


def queue_source(paths: amq.QueuePaths, branch: str = "dev/change", repo: str = "scripts") -> bool:
    source, expected, bundle = make_bundle(paths, branch, repo)
    try:
        result = amq.queue_change(branch, expected, paths, repo=repo)
        amq.cleanup_agent_source(source_repo(paths), branch, source)
        return result
    finally:
        bundle.unlink(missing_ok=True)


def publish_source(paths: amq.QueuePaths, branch: str = "dev/diary") -> bool:
    source, expected, bundle = make_bundle(paths, branch, "diary")
    try:
        result = amq.publish_diary_unattended(branch, expected, paths)
        amq.cleanup_agent_source(source_repo(paths), branch, source)
        return result
    finally:
        bundle.unlink(missing_ok=True)


def reviewed(paths: amq.QueuePaths) -> str:
    return run("git", "rev-parse", "HEAD", cwd=paths.queue)


def test_source_is_a_standalone_non_hardlinked_clone(paths: amq.QueuePaths):
    assert (source_repo(paths) / ".git").is_dir()
    source_objects = source_repo(paths) / ".git" / "objects"
    integration_objects = paths.integration / ".git" / "objects"
    source_object = next(path for path in source_objects.glob("[0-9a-f][0-9a-f]/*") if path.is_file())
    integration_object = integration_objects / source_object.relative_to(source_objects)
    assert source_object.stat().st_ino != integration_object.stat().st_ino


def test_bundle_queue_and_agent_cleanup_do_not_touch_integration(paths: amq.QueuePaths):
    source_branch(paths)
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    integration_head = run("git", "rev-parse", "master", cwd=paths.integration)
    run("git", "branch", "dev/change", "master", cwd=paths.integration)
    source, expected, bundle = make_bundle(paths, "dev/change")

    assert amq.queue_change("dev/change", expected, paths) is True

    assert source.exists(), "the privileged service never cleans the source worktree"
    assert run("git", "rev-parse", "dev/change", cwd=source_repo(paths)) == expected
    assert run("git", "rev-parse", "dev/change", cwd=paths.integration) == integration_head
    assert (paths.queue / ".git").is_dir(), "queue metadata is standalone"
    assert (paths.queue / "queued.txt").read_text() == "not yet approved\n"
    trigger = paths.lock.parent / "review-request"
    assert trigger.read_text() == reviewed(paths) + "\n"
    assert trigger.stat().st_mode & 0o777 == 0o600
    for repo in (paths.approved, paths.integration):
        assert not (repo / "queued.txt").exists()
        assert run("git", "rev-parse", "master", cwd=repo) == approved_head
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    assert run("git", "--git-dir", remote, "rev-parse", "master") == approved_head

    amq.cleanup_agent_source(source_repo(paths), "dev/change", source)
    bundle.unlink()
    assert not source.exists()
    branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/dev/change"],
        cwd=source_repo(paths),
    )
    assert branch.returncode != 0, "agent cleanup deletes only the source branch"
    assert run("git", "rev-parse", "dev/change", cwd=paths.integration) == integration_head


def test_pii_rejection_preserves_producer_work_and_allows_fix_and_retry(paths: amq.QueuePaths):
    source = source_branch(paths)
    commit(source, "private.py", 'host = "private-host"\n')
    _, expected, bundle = make_bundle(paths, "dev/change")

    with pytest.raises(amq.PiiRejected) as error:
        amq.queue_change("dev/change", expected, paths)
    assert "private-host" not in str(error.value)
    assert "<diff>:identity" in str(error.value)
    assert source.exists(), "rejection leaves the producing agent's worktree intact"
    assert run("git", "rev-parse", "HEAD", cwd=paths.queue) == run("git", "rev-parse", "master", cwd=paths.queue)
    assert not (paths.queue / "private.py").exists()
    assert not (paths.lock.parent / "review-request").exists()
    bundle.unlink()

    commit(source, "private.py", 'host = "gpu-host"\n')
    _, fixed, fixed_bundle = make_bundle(paths, "dev/change")
    assert amq.queue_change("dev/change", fixed, paths) is True
    assert (paths.queue / "private.py").read_text() == 'host = "gpu-host"\n'
    assert (paths.lock.parent / "review-request").read_text() == reviewed(paths) + "\n"

    amq.cleanup_agent_source(source_repo(paths), "dev/change", source)
    fixed_bundle.unlink()


def test_already_queued_request_reemits_review_event(paths: amq.QueuePaths):
    source_branch(paths)
    source, expected, bundle = make_bundle(paths, "dev/change")
    assert amq.queue_change("dev/change", expected, paths) is True
    trigger = paths.lock.parent / "review-request"
    trigger.unlink()

    assert amq.queue_change("dev/change", expected, paths) is False
    assert trigger.read_text() == reviewed(paths) + "\n"

    amq.cleanup_agent_source(source_repo(paths), "dev/change", source)
    bundle.unlink()


def test_owner_can_approve_exact_reviewed_oid(paths: amq.QueuePaths):
    source_branch(paths)
    queue_source(paths)
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
    queue_source(paths)
    oid = reviewed(paths)
    second = source_branch(paths, "dev/second")
    commit(second, "second.txt", "a different commit\n")
    queue_source(paths, "dev/second")
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
    queue_source(paths)
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
    queue_source(paths)
    (paths.approved / "README.md").write_text("local work\n")

    with pytest.raises(amq.QueueError, match="uncommitted"):
        amq.approve_queue(reviewed(paths), paths)
    assert (paths.approved / "README.md").read_text() == "local work\n"


def test_source_must_be_in_the_agent_worktree_root(paths: amq.QueuePaths):
    outside = paths.integration.parent / "outside"
    run("git", "worktree", "add", str(outside), "-b", "dev/outside", "master", cwd=source_repo(paths))
    commit(outside, "queued.txt", "outside\n")

    with pytest.raises(amq.QueueError, match="outside"):
        make_bundle(paths, "dev/outside")
    assert not (paths.queue / "queued.txt").exists()


def test_already_queued_request_still_cleans_source(paths: amq.QueuePaths):
    source_branch(paths)
    queue_source(paths)
    oid = reviewed(paths)
    retry = paths.worktrees / "change"
    run("git", "worktree", "add", str(retry), "-b", "dev/change", oid, cwd=source_repo(paths))

    assert queue_source(paths) is False
    assert not retry.exists()


def test_peer_uid_comes_from_the_kernel_socket_credentials():
    left, right = amq.socket.socketpair()
    try:
        assert amq.peer_uid(left.fileno()) == amq.os.geteuid()
    finally:
        left.close()
        right.close()


def empty_source(paths: amq.QueuePaths, branch: str = "dev/diary") -> Path:
    source = paths.worktrees / branch.split("/", 1)[1]
    run("git", "worktree", "add", str(source), "-b", branch, "master", cwd=source_repo(paths))
    return source


def test_diary_regular_memory_change_publishes_without_owner_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "routine notes\n")

    assert publish_source(paths) is True

    assert not source.exists()
    branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/dev/diary"],
        cwd=source_repo(paths),
    )
    assert branch.returncode != 0, "agent cleanup deletes the source branch"
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

    assert amq.diary_review_required(source_repo(paths), oid) == [name]


def test_diary_sensitive_change_cannot_publish_without_exact_oid_approval(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "AGENTS.md", "unreviewed instructions\n")
    commit(source, "sync/diary-sync-broker.service", "unreviewed service control\n")
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    _, expected, bundle = make_bundle(paths, "dev/diary", "diary")

    with pytest.raises(amq.ReviewRequired, match="AGENTS.md"):
        amq.publish_diary_unattended("dev/diary", expected, paths)
    bundle.unlink()
    assert run("git", "rev-parse", "master", cwd=paths.approved) == approved_head
    assert source.exists(), "a change still needing review is not cleaned up"

    assert queue_source(paths, "dev/diary", "diary") is True
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

    assert amq.diary_review_required(source_repo(paths), oid) == ["memory/run-me"]


def test_diary_symlink_memory_file_requires_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    (source / "memory").mkdir()
    (source / "memory/link").symlink_to("../AGENTS.md")
    run("git", "add", "memory/link", cwd=source)
    run("git", "commit", "-m", "add symlink", cwd=source)
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(source_repo(paths), oid) == ["memory/link"]


def test_diary_publication_pins_the_fetched_oid(paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "reviewed automatically\n")
    original = amq.diary_review_required

    def swap_branch_after_fetch(repo: Path, oid: str) -> list[str]:
        result = original(repo, oid)
        commit(source, "AGENTS.md", "too late to swap this in\n")
        return result

    monkeypatch.setattr(amq, "diary_review_required", swap_branch_after_fetch)
    assert publish_source(paths) is True

    assert not (paths.approved / "AGENTS.md").exists()
    assert (paths.approved / "memory/2026-10-31.md").exists()


def test_diary_agent_controlled_source_master_is_not_a_trust_base(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "safe on its own\n")
    attacker = empty_source(paths, "dev/attacker")
    commit(attacker, "AGENTS.md", "source refs are untrusted\n")
    attacker_oid = run("git", "rev-parse", "HEAD", cwd=attacker)
    integration_head = run("git", "rev-parse", "master", cwd=paths.integration)
    run("git", "update-ref", "refs/heads/master", attacker_oid, cwd=source_repo(paths))

    assert publish_source(paths) is True

    assert run("git", "rev-parse", "master", cwd=source_repo(paths)) == attacker_oid
    assert not (paths.approved / "AGENTS.md").exists()
    assert not (paths.integration / "AGENTS.md").exists()
    assert run("git", "rev-parse", "master^", cwd=paths.integration) == integration_head


def test_source_change_cannot_bypass_a_dirty_integration_guard(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "memory/2026-10-31.md", "must not overwrite local work\n")
    approved_head = run("git", "rev-parse", "master", cwd=paths.approved)
    (paths.integration / "README.md").write_text("owner work in progress\n")

    with pytest.raises(amq.QueueError, match="integration.*uncommitted"):
        publish_source(paths)

    assert source.exists()
    assert run("git", "rev-parse", "master", cwd=paths.approved) == approved_head
    assert (paths.integration / "README.md").read_text() == "owner work in progress\n"


def test_substituted_expected_oid_is_rejected(paths: amq.QueuePaths):
    source_branch(paths)
    _, expected, bundle = make_bundle(paths, "dev/change")
    wrong = "0" * 40
    assert wrong != expected
    wrong_bundle = amq.bundle_path(paths.worktrees, "scripts", "dev/change", wrong)
    wrong_bundle.write_bytes(bundle.read_bytes())

    with pytest.raises(amq.QueueError, match="does not match expected"):
        amq.queue_change("dev/change", wrong, paths)

    assert not (paths.queue / "queued.txt").exists()
    bundle.unlink()
    wrong_bundle.unlink()


def test_bundle_must_be_a_regular_file(paths: amq.QueuePaths):
    source_branch(paths)
    _, expected, bundle = make_bundle(paths, "dev/change")
    real = bundle.with_suffix(".real")
    bundle.rename(real)
    bundle.symlink_to(real)

    with pytest.raises(amq.QueueError, match="regular file"):
        amq.queue_change("dev/change", expected, paths)

    bundle.unlink()
    real.unlink()


@pytest.mark.parametrize("operation", ["queue", "unattended"])
def test_malicious_source_git_config_cannot_execute_in_service(paths: amq.QueuePaths, operation: str):
    marker = source_repo(paths).parent / f"{operation}-config-executed"
    if operation == "queue":
        branch = "dev/change"
        worktree = source_branch(paths)
    else:
        branch = "dev/diary"
        worktree = empty_source(paths)
        commit(worktree, "memory/2026-10-31.md", "safe memory change\n")
    expected = run("git", "rev-parse", "HEAD", cwd=worktree)
    bundle = amq.bundle_path(paths.worktrees, "scripts" if operation == "queue" else "diary", branch, expected)
    bundle.parent.mkdir(parents=True)
    run("git", "bundle", "create", str(bundle), f"refs/heads/{branch}", cwd=source_repo(paths))
    hooks = source_repo(paths) / ".git" / "evil-hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    for key in ("filter.evil.clean", "filter.evil.process", "uploadpack.packObjectsHook", "core.fsmonitor"):
        run("git", "config", key, f"touch {marker}", cwd=source_repo(paths))
    run("git", "config", "core.hooksPath", str(hooks), cwd=source_repo(paths))

    if operation == "queue":
        assert amq.queue_change(branch, expected, paths) is True
    else:
        assert amq.publish_diary_unattended(branch, expected, paths) is True

    assert not marker.exists()
    assert worktree.exists(), "the privileged service performs no source cleanup"
    bundle.unlink()


def test_agent_request_bundles_cleans_up_and_removes_bundle(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch
):
    source = source_branch(paths)
    expected = run("git", "rev-parse", "HEAD", cwd=source)
    bundle = amq.bundle_path(paths.worktrees, "scripts", "dev/change", expected)
    monkeypatch.setattr(amq, "AGENT_WORKTREE_ROOT", paths.worktrees)
    monkeypatch.setattr(amq, "AGENT_SOURCES", {"scripts": source_repo(paths), "diary": source_repo(paths)})
    monkeypatch.setattr(amq.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="agent"))
    seen: list[str] = []

    def deliver(payload: str) -> str:
        seen.append(payload)
        repo, branch, oid = payload.split()
        amq.queue_change(branch, oid, paths, repo=repo)
        return "RESULT ok 0\n"

    monkeypatch.setattr(amq, "request", deliver)
    assert amq.agent_request("scripts", "dev/change") == 0

    assert seen == [f"scripts dev/change {expected}"]
    assert not source.exists()
    assert not bundle.exists()
    assert (paths.queue / "queued.txt").exists()


def test_agent_request_refuses_non_agent_uid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(amq.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="kek"))
    monkeypatch.setattr(
        amq,
        "create_agent_bundle",
        lambda *args: pytest.fail("non-agent attempted a source operation"),
    )

    assert amq.agent_request("scripts", "dev/change") == 1
