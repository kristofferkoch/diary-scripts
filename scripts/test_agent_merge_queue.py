"""Security-boundary tests for the human-approved publication queue."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import agent_merge_queue as amq

#: Captured before the autouse fixture below replaces the module attribute.
REAL_SYNC_VENV = amq.sync_venv


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


def test_pii_prescan_uses_approved_master_with_stale_queue_master(paths: amq.QueuePaths):
    historical = "host" + ' = "' + "private" + '-host"\n'
    commit(paths.approved, "historical.py", historical)
    run("git", "fetch", str(paths.approved), "master", cwd=source_repo(paths))
    run("git", "merge", "--ff-only", "FETCH_HEAD", cwd=source_repo(paths))
    run("git", "clone", "--no-hardlinks", str(paths.approved), str(paths.queue))
    run("git", "checkout", "-b", amq.QUEUE_BRANCH, "master", cwd=paths.queue)
    run("git", "update-ref", "refs/heads/master", "master^", cwd=paths.queue)
    source_branch(paths)

    assert queue_source(paths) is True


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


def test_expected_base_change_before_publication_is_rejected(paths: amq.QueuePaths):
    source_branch(paths)
    queue_source(paths)
    oid = reviewed(paths)
    reviewed_base = run("git", "rev-parse", "master", cwd=paths.approved)
    integration = run("git", "rev-parse", "master", cwd=paths.integration)
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    published = run("git", "--git-dir", remote, "rev-parse", "master")
    commit(paths.approved, "owner.txt", "base advanced\n")
    advanced = run("git", "rev-parse", "master", cwd=paths.approved)

    with pytest.raises(amq.QueueError, match="approved master changed after review"):
        amq.approve_queue(oid, paths, expected_base=reviewed_base)

    assert run("git", "rev-parse", "master", cwd=paths.approved) == advanced
    assert run("git", "rev-parse", "master", cwd=paths.integration) == integration
    assert run("git", "--git-dir", remote, "rev-parse", "master") == published
    assert reviewed(paths) == oid
    assert not (paths.approved / "queued.txt").exists()


def test_base_race_at_locked_publication_recheck_is_rejected(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch,
):
    source_branch(paths)
    queue_source(paths)
    oid = reviewed(paths)
    reviewed_base = run("git", "rev-parse", "master", cwd=paths.approved)
    integration = run("git", "rev-parse", "master", cwd=paths.integration)
    remote = run("git", "config", "--get", "remote.origin.url", cwd=paths.approved)
    published = run("git", "--git-dir", remote, "rev-parse", "master")
    original_git = amq.git
    advanced: list[str] = []

    def racing_git(repo: Path, *args: str, safe: bool = False, check: bool = True):
        proc = original_git(repo, *args, safe=safe, check=check)
        if repo == paths.approved and args and args[0] == "fetch":
            commit(paths.approved, "owner.txt", "base raced after freshness check\n")
            advanced.append(run("git", "rev-parse", "master", cwd=paths.approved))
        return proc

    monkeypatch.setattr(amq, "git", racing_git)
    with pytest.raises(amq.QueueError, match="approved master changed after review"):
        amq.approve_queue(oid, paths, expected_base=reviewed_base)

    assert len(advanced) == 1
    assert run("git", "rev-parse", "master", cwd=paths.approved) == advanced[0]
    assert run("git", "rev-parse", "master", cwd=paths.integration) == integration
    assert run("git", "--git-dir", remote, "rev-parse", "master") == published
    assert reviewed(paths) == oid
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


@pytest.mark.parametrize("name", sorted(amq.DIARY_UNATTENDED_TOPICS) + ["FDV_EXAMPLE.md"])
def test_diary_data_topic_change_publishes_without_owner_review(paths: amq.QueuePaths, name: str):
    source = empty_source(paths)
    commit(source, name, "routine topic update\n")

    assert publish_source(paths) is True

    for repo in (paths.approved, paths.integration):
        assert (repo / name).read_text() == "routine topic update\n"


@pytest.mark.parametrize("name", [
    "AGENTS.md",
    "CLAUDE.md",
    "TOOLS.md",
    "CALENDAR-RULES.md",
    "SJEKK.md",
    "USER.md",
    "docs/CALENDAR.md",
    "nested/FDV_EXAMPLE.md",
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


def test_diary_mixed_topic_and_instruction_changes_require_review(paths: amq.QueuePaths):
    source = empty_source(paths)
    commit(source, "CALENDAR.md", "routine topic update\n")
    commit(source, "TOOLS.md", "agent-controlled instructions\n")
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(source_repo(paths), oid) == ["TOOLS.md"]


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


@pytest.mark.parametrize("name", ["memory/run-me", "CALENDAR.md"])
def test_diary_executable_unattended_file_requires_review(paths: amq.QueuePaths, name: str):
    source = empty_source(paths)
    commit(source, name, "ordinary file\n")
    (source / name).chmod(0o755)
    run("git", "add", name, cwd=source)
    run("git", "commit", "-m", "make executable", cwd=source)
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(source_repo(paths), oid) == [name]


@pytest.mark.parametrize("name", ["memory/link", "CALENDAR.md"])
def test_diary_symlink_unattended_file_requires_review(paths: amq.QueuePaths, name: str):
    source = empty_source(paths)
    link = source / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("../AGENTS.md" if name.startswith("memory/") else "AGENTS.md")
    run("git", "add", name, cwd=source)
    run("git", "commit", "-m", "add symlink", cwd=source)
    oid = run("git", "rev-parse", "HEAD", cwd=source)

    assert amq.diary_review_required(source_repo(paths), oid) == [name]


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


def _pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'example'\nversion = '0'\n", encoding="utf-8")


def test_sync_venv_also_refreshes_the_agent_checkout(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch
):
    _pyproject(paths.integration)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(amq.subprocess, "run", fake_run)
    REAL_SYNC_VENV(paths)

    projects = [c[c.index("--project") + 1] for c in calls]
    assert projects == [str(paths.approved), str(paths.integration)]


def test_sync_venv_only_warns_when_the_agent_checkout_fails(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch, capsys
):
    _pyproject(paths.integration)

    def fake_run(argv, **kwargs):
        approved = argv[argv.index("--project") + 1] == str(paths.approved)
        return subprocess.CompletedProcess(
            argv, 0 if approved else 1, "", "" if approved else "network down"
        )

    monkeypatch.setattr(amq.subprocess, "run", fake_run)
    REAL_SYNC_VENV(paths)  # must not raise

    assert "agent virtualenv sync failed" in capsys.readouterr().err


def test_sync_venv_still_fails_when_the_approved_checkout_fails(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch
):
    _pyproject(paths.integration)
    monkeypatch.setattr(
        amq.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    with pytest.raises(amq.QueueError):
        REAL_SYNC_VENV(paths)


# ---------- diary-scripts pointer: forward-only on approval ----------


def _gitlink(repo: Path) -> str | None:
    out = run("git", "ls-tree", "master", "--", "diary-scripts", cwd=repo)
    return out.split()[2] if out else None


def _set_gitlink(repo: Path, sha: str, message: str) -> str:
    run("git", "update-index", "--add", "--cacheinfo",
        f"160000,{sha},diary-scripts", cwd=repo)
    run("git", "commit", "-m", message, cwd=repo)
    return run("git", "rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def diary_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    scripts = tmp_path / "scripts-approved"
    scripts.mkdir()
    run("git", "init", "-b", "master", cwd=scripts)
    run("git", "config", "user.name", "Queue Test", cwd=scripts)
    run("git", "config", "user.email", "queue@example.invalid", cwd=scripts)
    commit(scripts, "s.txt", "s1\n")
    s1 = run("git", "rev-parse", "HEAD", cwd=scripts)
    commit(scripts, "s.txt", "s2\n")
    s2 = run("git", "rev-parse", "HEAD", cwd=scripts)
    commit(scripts, "s.txt", "s3\n")
    s3 = run("git", "rev-parse", "HEAD", cwd=scripts)

    approved = tmp_path / "diary-approved"
    approved.mkdir()
    run("git", "init", "-b", "master", cwd=approved)
    run("git", "config", "user.name", "Queue Test", cwd=approved)
    run("git", "config", "user.email", "queue@example.invalid", cwd=approved)
    commit(approved, "README.md", "diary\n")
    d1 = _set_gitlink(approved, s1, "pointer s1")
    d2 = _set_gitlink(approved, s2, "pointer s2")

    integration = tmp_path / "diary"
    source = tmp_path / "source"
    for repo in (integration, source):
        run("git", "clone", "--no-hardlinks", str(approved), str(repo))
        run("git", "config", "user.name", "Queue Test", cwd=repo)
        run("git", "config", "user.email", "queue@example.invalid", cwd=repo)

    review_config = tmp_path / "review.toml"
    review_config.write_text('[review]\nmodel = "local-test"\n')
    paths = amq.QueuePaths(
        integration=integration,
        approved=approved,
        queue=tmp_path / "diary-automatic",
        worktrees=tmp_path / "diary-worktrees",
        lock=tmp_path / "queue.lock",
        review_config=review_config,
    )
    monkeypatch.setattr(amq, "paths_for", lambda repo: amq.QueuePaths(
        integration=integration,
        approved=scripts if repo == "scripts" else approved,
        queue=paths.queue,
        worktrees=paths.worktrees,
        lock=paths.lock,
        review_config=review_config,
    ))
    return SimpleNamespace(paths=paths, scripts=scripts, source=source,
                           approved=approved, integration=integration,
                           s1=s1, s2=s2, s3=s3, d1=d1, d2=d2)


def _fetch(env: SimpleNamespace, branch: str) -> str:
    run("git", "fetch", str(env.source), branch, cwd=env.approved)
    return run("git", "rev-parse", "FETCH_HEAD", cwd=env.approved)


def test_stale_branch_never_reverts_the_pointer(diary_env: SimpleNamespace):
    """A branch forked before a bump and left alone must not ff the old pointer."""
    env = diary_env
    run("git", "checkout", "-b", "dev/stale", env.d1, cwd=env.source)
    commit(env.source, "note.txt", "keep\n")
    reviewed = _fetch(env, "dev/stale")

    assert amq.guard_diary_submodule(
        env.paths, reviewed, scripts_approved=env.scripts) is True
    assert amq.publish_fetched(
        reviewed, env.paths, push=False, sync_environment=False,
        synchronize_queue=False) is True
    assert _gitlink(env.approved) == env.s2
    assert (env.approved / "note.txt").read_text() == "keep\n"


def test_explicit_backwards_pointer_is_rejected(diary_env: SimpleNamespace):
    env = diary_env
    run("git", "checkout", "-b", "dev/backwards", "master", cwd=env.source)
    _set_gitlink(env.source, env.s1, "regress pointer")
    reviewed = _fetch(env, "dev/backwards")

    with pytest.raises(amq.QueueError, match="backwards"):
        amq.guard_diary_submodule(env.paths, reviewed, scripts_approved=env.scripts)
    with pytest.raises(amq.QueueError, match="backwards"):
        amq.publish_fetched(reviewed, env.paths, push=False,
                            sync_environment=False, synchronize_queue=False)
    assert _gitlink(env.approved) == env.s2


def test_explicit_forward_pointer_is_allowed(diary_env: SimpleNamespace):
    env = diary_env
    run("git", "checkout", "-b", "dev/forward", "master", cwd=env.source)
    _set_gitlink(env.source, env.s3, "advance pointer")
    reviewed = _fetch(env, "dev/forward")

    assert amq.guard_diary_submodule(
        env.paths, reviewed, scripts_approved=env.scripts) is False
    assert amq.publish_fetched(
        reviewed, env.paths, push=False, sync_environment=False,
        synchronize_queue=False) is True
    assert _gitlink(env.approved) == env.s3


def test_pointer_to_unapproved_scripts_commit_is_rejected(diary_env: SimpleNamespace):
    env = diary_env
    fake = "0" * 39 + "1"
    run("git", "checkout", "-b", "dev/unknown", "master", cwd=env.source)
    _set_gitlink(env.source, fake, "unknown pointer")
    reviewed = _fetch(env, "dev/unknown")

    with pytest.raises(amq.QueueError, match="not an approved commit"):
        amq.guard_diary_submodule(env.paths, reviewed, scripts_approved=env.scripts)


def test_repo_without_a_submodule_is_not_affected(paths: amq.QueuePaths):
    """The scripts repository has no diary-scripts entry: the guard is a no-op."""
    commit(paths.approved, "extra.txt", "x\n")
    head = run("git", "rev-parse", "HEAD", cwd=paths.approved)
    assert amq.guard_diary_submodule(paths, head) is False
def test_queue_merge_pins_its_own_committer_identity(
    paths: amq.QueuePaths, monkeypatch: pytest.MonkeyPatch
):
    """A merge commit must not depend on the account owner's git identity.

    Regression: the queue repo is a plain clone and inherited no identity, and
    `~/.gitconfig` has `user.name` but a commented-out `user.email`, so every
    non-fast-forward queue merge died with "Committer identity unknown" and
    surfaced as a bogus merge conflict.
    """
    amq.ensure_queue(paths)
    queue_repo = paths.queue

    # Both sides diverge, so the merge cannot be a fast-forward.
    commit(queue_repo, "queued.txt", "queued\n")
    run("git", "checkout", "-q", "-b", "topic", "master", cwd=queue_repo)
    commit(queue_repo, "topic.txt", "topic\n")
    run("git", "checkout", "-q", amq.QUEUE_BRANCH, cwd=queue_repo)

    for key in ("user.name", "user.email"):
        amq.git(queue_repo, "config", "--unset", key, check=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for key in ("EMAIL", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL",
                "AGENT_MERGE_IDENTITY_EMAIL"):
        monkeypatch.delenv(key, raising=False)

    proc = amq.git(queue_repo, *amq.merge_identity(), "merge", "--no-ff",
                   "--no-edit", "-m", "queue test", "topic", check=False)
    assert proc.returncode == 0, proc.stderr
    assert amq.git(queue_repo, "log", "-1", "--format=%ae").stdout.strip() \
        == amq.MERGE_IDENTITY_FALLBACK
    # The unit supplies the address that is actually used; it has to be the one
    # verified on the owner's account, so it never lives in this repository.
    monkeypatch.setenv("AGENT_MERGE_IDENTITY_EMAIL", "user@example.com")
    assert "user.email=user@example.com" in amq.merge_identity()
    monkeypatch.setenv("AGENT_MERGE_IDENTITY_EMAIL", "  ")
    assert "user.email=user@example.com" in amq.merge_identity()


def test_sync_does_not_require_historical_submodule_commits(
    paths: amq.QueuePaths, tmp_path: Path
) -> None:
    """A recorded submodule revision that no longer exists must not fail a sync.

    A branch whose submodule pointer was rebased keeps the discarded value in
    history. Fetch recursion used to demand that commit from the submodule
    remote, which failed after approved master had already advanced.
    """
    submodule = tmp_path / "submodule.git"
    run("git", "init", "--bare", str(submodule))
    seed = tmp_path / "submodule-seed"
    seed.mkdir()
    run("git", "init", "-b", "master", cwd=seed)
    run("git", "config", "user.name", "Queue Test", cwd=seed)
    run("git", "config", "user.email", "queue@example.invalid", cwd=seed)
    commit(seed, "file.txt", "submodule\n")
    run("git", "remote", "add", "origin", str(submodule), cwd=seed)
    run("git", "push", "origin", "master", cwd=seed)

    # Populate the submodule in the integration checkout, exactly like the real
    # one, so a recursive fetch has somewhere to recurse into.
    run("git", "-c", "protocol.file.allow=always", "submodule", "add",
        str(submodule), "diary-scripts", cwd=paths.integration)
    run("git", "commit", "-m", "add the submodule", cwd=paths.integration)
    run("git", "fetch", str(paths.integration), "master", cwd=paths.approved)
    run("git", "reset", "--hard", "FETCH_HEAD", cwd=paths.approved)

    approved = paths.approved
    vanished = "1" * 40
    run("git", "update-index", "--cacheinfo",
        f"160000,{vanished},diary-scripts", cwd=approved)
    run("git", "commit", "-m", "record a vanished submodule revision", cwd=approved)

    run("git", "config", "fetch.recurseSubmodules", "true", cwd=paths.integration)
    amq.sync_integration(paths)

    assert run("git", "rev-parse", "HEAD", cwd=paths.integration) == run(
        "git", "rev-parse", "HEAD", cwd=approved
    )
    assert run("git", "ls-tree", "HEAD", "diary-scripts", cwd=paths.integration).split()[2] == vanished


def test_doctor_flags_a_stale_installed_client(tmp_path: Path) -> None:
    """D6: approving a client change must not silently leave the old one running."""
    approved = tmp_path / "agent_merge_queue.py"
    approved.write_text("new client\n")
    installed = tmp_path / "installed-client"
    installed.write_text("old client\n")

    ok, message = amq.installed_client_status(installed, approved)

    assert not ok
    assert "install -o root -g root -m 0755" in message
    assert str(approved) in message


def test_doctor_accepts_a_matching_client(tmp_path: Path) -> None:
    approved = tmp_path / "agent_merge_queue.py"
    approved.write_text("same bytes\n")
    installed = tmp_path / "installed-client"
    installed.write_bytes(approved.read_bytes())

    ok, message = amq.installed_client_status(installed, approved)

    assert ok
    assert "matches" in message


def test_doctor_reports_a_missing_client(tmp_path: Path) -> None:
    approved = tmp_path / "agent_merge_queue.py"
    approved.write_text("new client\n")

    ok, message = amq.installed_client_status(tmp_path / "absent", approved)

    assert not ok
    assert "missing" in message
