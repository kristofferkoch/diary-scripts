#!/usr/bin/env python3
"""Human-approved publication queues for code and private-workspace changes.

Only regular-file memory changes may publish unattended. Other requests fetch
agent source branches into an isolated ``queue/automatic`` repository. Only
``agent-merge-approve`` from the account owner's UID, naming the exact reviewed
commit OID, may merge that commit into the approved repository and push
``master``.

The module is intentionally stdlib-only apart from the workspace config
lookup. The service runs a root-owned copy installed from the approved tree.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

QUEUE_BRANCH = "queue/automatic"
QUEUE_REF = f"refs/heads/{QUEUE_BRANCH}"
BRANCH_RE = re.compile(r"^(dev|mem)/[a-z0-9][a-z0-9-]*$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
LOCK_PATH = Path("/var/lib/agent-merge/queue.lock")
SOCKET_PATH = Path("/run/agent-merge/socket")


class QueueError(RuntimeError):
    """A request cannot be completed without weakening the boundary."""


class ReviewRequired(QueueError):
    """A diary change contains paths that require explicit owner review."""


@dataclass(frozen=True)
class QueuePaths:
    integration: Path
    source: Path
    approved: Path
    queue: Path
    worktrees: Path
    lock: Path = LOCK_PATH


def default_paths() -> QueuePaths:
    integration = os.environ.get("AGENT_MERGE_INTEGRATION_ROOT")
    source = os.environ.get("AGENT_MERGE_SOURCE_ROOT")
    approved = os.environ.get("AGENT_MERGE_APPROVED_ROOT")
    queue = os.environ.get("AGENT_MERGE_QUEUE_ROOT")
    worktrees = os.environ.get("AGENT_MERGE_WORKTREE_ROOT")
    if integration and source and approved and queue and worktrees:
        return QueuePaths(
            Path(integration), Path(source), Path(approved), Path(queue), Path(worktrees)
        )

    from mail_reader.config import workspace_root

    workspace = workspace_root()
    worktrees = workspace.parent / "diary-worktrees"
    return QueuePaths(
        integration=workspace / "diary-scripts",
        source=worktrees / "_sources" / "diary-scripts",
        approved=workspace.parent / "diary-scripts-approved",
        queue=workspace.parent / "diary-scripts-automatic",
        worktrees=worktrees,
    )


def diary_paths() -> QueuePaths:
    integration = os.environ.get("AGENT_MERGE_DIARY_INTEGRATION_ROOT")
    source = os.environ.get("AGENT_MERGE_DIARY_SOURCE_ROOT")
    approved = os.environ.get("AGENT_MERGE_DIARY_APPROVED_ROOT")
    queue = os.environ.get("AGENT_MERGE_DIARY_QUEUE_ROOT")
    worktrees = os.environ.get("AGENT_MERGE_DIARY_WORKTREE_ROOT")
    if integration and source and approved and queue and worktrees:
        return QueuePaths(
            Path(integration), Path(source), Path(approved), Path(queue), Path(worktrees)
        )

    from mail_reader.config import workspace_root

    workspace = workspace_root()
    worktrees = workspace.parent / "diary-worktrees"
    return QueuePaths(
        integration=workspace,
        source=worktrees / "_sources" / "diary",
        approved=workspace.parent / "diary-approved",
        queue=workspace.parent / "diary-automatic",
        worktrees=worktrees,
    )


def paths_for(repo: str) -> QueuePaths:
    if repo == "scripts":
        return default_paths()
    if repo == "diary":
        return diary_paths()
    raise QueueError(f"unknown repository {repo}")


@contextmanager
def queue_lock(paths: QueuePaths) -> Iterator[None]:
    paths.lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with paths.lock.open("a+") as handle:
        os.chmod(paths.lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def git(repo: Path, *args: str, safe: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    if safe:
        cmd += ["-c", f"safe.directory={repo}"]
    cmd += ["-C", str(repo), *args]
    env = os.environ.copy()
    for key in list(env):
        if key in {"GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}:
            del env[key]
        elif key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del env[key]
    protected = (
        ("core.fsmonitor", "false"),
        ("core.hooksPath", "/dev/null"),
        ("diff.external", ""),
        ("uploadpack.packObjectsHook", ""),
        ("safe.directory", "*"),
    )
    env["GIT_CONFIG_COUNT"] = str(len(protected))
    for index, (key, value) in enumerate(protected):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise QueueError(f"git {' '.join(args)} failed: {detail}")
    return proc


def clean(repo: Path, *, safe: bool = False) -> bool:
    return not git(
        repo, "status", "--porcelain", "--untracked-files=no",
        "--ignore-submodules=all", safe=safe,
    ).stdout.strip()


def worktree_branch(repo: Path, branch: str) -> Path | None:
    out = git(repo, "worktree", "list", "--porcelain", safe=True).stdout
    path: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def ensure_queue(paths: QueuePaths) -> None:
    if not (paths.approved / ".git").is_dir():
        raise QueueError("approved checkout must have private, standalone Git metadata")
    if not paths.queue.exists():
        proc = subprocess.run(
            ["git", "clone", "--no-hardlinks", str(paths.approved), str(paths.queue)],
            capture_output=True, text=True,
        )
        if proc.returncode:
            raise QueueError(f"could not create isolated queue repository: {proc.stderr.strip()}")
        git(paths.queue, "checkout", "-b", QUEUE_BRANCH, "master")
    if not (paths.queue / ".git").is_dir():
        raise QueueError("queue checkout must have private, standalone Git metadata")
    actual = git(paths.queue, "symbolic-ref", "HEAD").stdout.strip()
    if actual != QUEUE_REF:
        raise QueueError(f"queue HEAD is {actual}, expected {QUEUE_REF}")
    if not clean(paths.queue):
        raise QueueError(f"queue checkout has uncommitted changes: {paths.queue}")


def remove_source(paths: QueuePaths, branch: str, source: Path) -> None:
    git(paths.source, "worktree", "remove", "--force", str(source), safe=True)
    # The branch is merged into the queue, not necessarily into approved master.
    git(paths.source, "branch", "-D", branch, safe=True)


def source_worktree(paths: QueuePaths, branch: str) -> Path:
    if not BRANCH_RE.fullmatch(branch):
        raise QueueError("expected branch dev/<slug> or mem/<slug>")
    source = worktree_branch(paths.source, branch)
    if source is None:
        raise QueueError(f"{branch} is not checked out in a worktree")
    try:
        source.relative_to(paths.worktrees)
    except ValueError as exc:
        raise QueueError(f"source worktree is outside {paths.worktrees}: {source}") from exc
    if not clean(source, safe=True):
        raise QueueError(f"source worktree has uncommitted changes: {source}")
    return source


def queue_change(branch: str, paths: QueuePaths, *, repo: str = "scripts") -> bool:
    """Copy and merge one agent branch into the isolated queue. Never push."""
    source = source_worktree(paths, branch)
    with queue_lock(paths):
        ensure_queue(paths)
        git(paths.queue, "fetch", "--no-tags", str(paths.source), branch)
        fetched = git(paths.queue, "rev-parse", "FETCH_HEAD").stdout.strip()
        if git(paths.queue, "merge-base", "--is-ancestor", fetched, QUEUE_BRANCH, check=False).returncode == 0:
            print(f"agent-merge: {branch} is already queued")
            remove_source(paths, branch, source)
            return False

        base = git(paths.queue, "rev-parse", "--short", "HEAD").stdout.strip()
        print(f"agent-merge: queueing {branch} as {fetched} (queue was {base})")
        log = git(paths.queue, "log", "--oneline", f"{QUEUE_BRANCH}..{fetched}").stdout
        print("".join(f"  {line}\n" for line in log.splitlines()[:20]), end="")
        if git(paths.queue, "merge-base", "--is-ancestor", QUEUE_BRANCH, fetched, check=False).returncode == 0:
            git(paths.queue, "merge", "--ff-only", fetched)
        else:
            proc = git(paths.queue, "merge", "--no-ff", "--no-edit", "-m", f"queue {branch}", fetched, check=False)
            if proc.returncode:
                git(paths.queue, "merge", "--abort", check=False)
                raise QueueError(f"queue merge conflict for {branch}")
        new = git(paths.queue, "rev-parse", "HEAD").stdout.strip()
        print(f"agent-merge: queued {branch} -> {new}; approved master is unchanged")
        print(f"review: git -C {shlex.quote(str(paths.queue))} diff master...{new}")
        print(f"approve: agent-merge-approve {repo} {new}")
        remove_source(paths, branch, source)
        return True


def sync_integration(paths: QueuePaths) -> None:
    git(paths.integration, "fetch", "--no-tags", str(paths.approved), "master")
    git(paths.integration, "merge", "--ff-only", "FETCH_HEAD")


def sync_queue(paths: QueuePaths) -> None:
    git(paths.queue, "fetch", "--no-tags", str(paths.approved), "master")
    git(paths.queue, "merge", "--ff-only", "FETCH_HEAD")


def sync_venv(paths: QueuePaths) -> None:
    proc = subprocess.run(
        ["uv", "sync", "--frozen", "--project", str(paths.approved)],
        capture_output=True, text=True,
    )
    if proc.returncode:
        raise QueueError(f"approved virtualenv sync failed: {(proc.stderr or proc.stdout).strip()}")


def publish_fetched(
    reviewed: str,
    paths: QueuePaths,
    *,
    push: bool,
    sync_environment: bool,
    synchronize_queue: bool,
) -> bool:
    if not (paths.approved / ".git").is_dir():
        raise QueueError("approved checkout must have private, standalone Git metadata")
    for label, repo in (("approved", paths.approved), ("integration", paths.integration)):
        if not clean(repo):
            raise QueueError(f"{label} checkout has uncommitted changes: {repo}")
    if git(paths.integration, "symbolic-ref", "HEAD").stdout.strip() != "refs/heads/master":
        raise QueueError("integration checkout is not on master")
    if git(paths.approved, "symbolic-ref", "HEAD").stdout.strip() != "refs/heads/master":
        raise QueueError("approved checkout is not on master")

    if git(paths.approved, "merge-base", "--is-ancestor", reviewed, "master", check=False).returncode == 0:
        sync_integration(paths)
        if sync_environment:
            sync_venv(paths)
        if push:
            git(paths.approved, "push", "origin", "master")
        if synchronize_queue:
            sync_queue(paths)
        print("agent-merge: no new changes; approved and integration are synchronized")
        return False

    base = git(paths.approved, "rev-parse", "--short", "master").stdout.strip()
    print(f"agent-merge: publishing {reviewed} into master (was {base})")
    print(git(paths.approved, "diff", "--stat", f"master...{reviewed}").stdout, end="")
    if git(paths.approved, "merge-base", "--is-ancestor", "master", reviewed, check=False).returncode == 0:
        git(paths.approved, "merge", "--ff-only", reviewed)
    else:
        proc = git(paths.approved, "merge", "--no-ff", "--no-edit", "-m", "approve queued changes", reviewed, check=False)
        if proc.returncode:
            git(paths.approved, "merge", "--abort", check=False)
            raise QueueError("approval merge conflict; review and reconcile the queue")
    sync_integration(paths)
    if sync_environment:
        sync_venv(paths)
    if push:
        git(paths.approved, "push", "origin", "master")
    if synchronize_queue:
        sync_queue(paths)
    new = git(paths.approved, "rev-parse", "--short", "master").stdout.strip()
    print(f"agent-merge: approved master is {new}" + (" and pushed" if push else ""))
    return True


def approve_queue(
    reviewed: str,
    paths: QueuePaths,
    *,
    push: bool = True,
    sync_environment: bool = True,
) -> bool:
    """Publish exactly the reviewed queue commit; caller UID is checked by the CLI."""
    if not OID_RE.fullmatch(reviewed):
        raise QueueError("approval requires the full reviewed queue commit OID")
    with queue_lock(paths):
        ensure_queue(paths)
        actual = git(paths.queue, "rev-parse", "HEAD").stdout.strip()
        if actual != reviewed:
            raise QueueError(f"queue changed after review: reviewed {reviewed}, current {actual}")
        git(paths.approved, "fetch", "--no-tags", str(paths.queue), reviewed)
        fetched = git(paths.approved, "rev-parse", "FETCH_HEAD").stdout.strip()
        if fetched != reviewed:
            raise QueueError("fetched commit does not match the reviewed OID")
        return publish_fetched(
            reviewed,
            paths,
            push=push,
            sync_environment=sync_environment,
            synchronize_queue=True,
        )


def diary_review_required(repo: Path, reviewed: str) -> list[str]:
    """Return changed paths outside the unattended regular-file memory boundary."""
    if not OID_RE.fullmatch(reviewed):
        raise QueueError("diary publication requires a full commit OID")
    raw = git(repo, "diff", "--raw", "-z", "--no-renames", f"master...{reviewed}").stdout
    fields = raw.split("\0")
    if fields[-1]:
        raise QueueError("unterminated Git raw-diff response")
    sensitive: list[str] = []
    for index in range(0, len(fields) - 1, 2):
        metadata, path = fields[index:index + 2]
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(":"):
            raise QueueError("unexpected Git raw-diff response")
        old_mode, new_mode, status = parts[0][1:], parts[1], parts[4]
        if not (
            path.startswith("memory/")
            and status in {"A", "M", "D"}
            and old_mode in {"100644", "000000"}
            and new_mode in {"100644", "000000"}
        ):
            sensitive.append(path)
    return sensitive


def publish_diary_unattended(branch: str, paths: QueuePaths, *, push: bool = True) -> bool:
    """Publish only regular-file memory changes; never trust source master."""
    source = source_worktree(paths, branch)
    with queue_lock(paths):
        git(paths.approved, "fetch", "--no-tags", str(paths.source), branch)
        fetched = git(paths.approved, "rev-parse", "FETCH_HEAD").stdout.strip()
        sensitive = diary_review_required(paths.approved, fetched)
        if sensitive:
            raise ReviewRequired("owner review required for: " + ", ".join(sensitive))
        changed = publish_fetched(
            fetched,
            paths,
            push=push,
            sync_environment=False,
            synchronize_queue=False,
        )
        remove_source(paths, branch, source)
        return changed


def request(payload: str, socket_path: Path = SOCKET_PATH, timeout: float = 300.0) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(payload.encode() + b"\n")
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks).decode(errors="replace")


def approve_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve one explicitly reviewed queue commit.")
    parser.add_argument("--repo", choices=("scripts", "diary"), default="scripts")
    parser.add_argument("reviewed", help="full queue commit OID copied from the reviewed diff")
    args = parser.parse_args(argv)
    if not OID_RE.fullmatch(args.reviewed):
        parser.error("reviewed must be the full 40-character commit OID")
    paths = paths_for(args.repo)
    reviewed = args.reviewed
    print(f"Review first: git -C {shlex.quote(str(paths.queue))} diff master...{reviewed}")
    print(f"Approving only {args.repo} queue commit: {reviewed}")
    try:
        output = request(f"approve {args.repo} {reviewed}")
    except OSError as exc:
        print(f"agent-merge-approve: cannot reach merge socket: {exc}", file=sys.stderr)
        return 2
    print(output, end="")
    results = [line for line in output.splitlines() if line.startswith("RESULT ")]
    return 0 if results and results[-1].startswith("RESULT ok") else 1


def peer_uid(fd: int) -> int:
    """Return the Unix socket caller's kernel-verified UID."""
    sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return uid
    finally:
        sock.close()


def queue_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate the reviewed publication queues.")
    sub = parser.add_subparsers(dest="command", required=True)
    queued = sub.add_parser("queue", help="merge an agent branch without publishing")
    queued.add_argument("--repo", choices=("scripts", "diary"), default="scripts")
    queued.add_argument("branch")
    approved = sub.add_parser("approve", help="publish one reviewed queue OID for the service UID only")
    approved.add_argument("--repo", choices=("scripts", "diary"), default="scripts")
    approved.add_argument("reviewed")
    approved.add_argument("--peer-fd", type=int, required=True)
    unattended = sub.add_parser("publish-diary", help="publish only regular-file memory changes")
    unattended.add_argument("branch")
    client = sub.add_parser("client", help="request approval as the account owner")
    client.add_argument("--repo", choices=("scripts", "diary"), default="scripts")
    client.add_argument("reviewed")
    args = parser.parse_args(argv)
    if args.command == "client":
        return approve_main(["--repo", args.repo, args.reviewed])
    try:
        if args.command == "queue":
            queue_change(args.branch, paths_for(args.repo), repo=args.repo)
        elif args.command == "publish-diary":
            publish_diary_unattended(args.branch, diary_paths())
        else:
            if peer_uid(args.peer_fd) != os.geteuid():
                raise QueueError("approval requires the account owner's UID")
            approve_queue(
                args.reviewed,
                paths_for(args.repo),
                sync_environment=args.repo == "scripts",
            )
    except ReviewRequired as exc:
        print(f"agent-merge: {exc}", file=sys.stderr)
        return 3
    except QueueError as exc:
        print(f"agent-merge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "agent-merge-approve":
        sys.exit(approve_main())
    sys.exit(queue_main())
