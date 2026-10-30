#!/usr/bin/env python3
"""Human-approved publication queue for the public helper-code repository.

Unattended requests fetch agent branches into an isolated ``queue/automatic``
repository. Only ``agent-merge-approve``, authenticated with the exact reviewed
commit OID and the token delivered to the account owner's Signal note-to-self,
may merge that commit into the approved repository and push ``master``.

The module is intentionally stdlib-only apart from the workspace config
lookup. The service runs a root-owned copy installed from the approved tree.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

QUEUE_BRANCH = "queue/automatic"
QUEUE_REF = f"refs/heads/{QUEUE_BRANCH}"
BRANCH_RE = re.compile(r"^(dev|mem)/[a-z0-9][a-z0-9-]*$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
TOKEN_PATH = Path("/var/lib/agent-merge/token")
LOCK_PATH = Path("/var/lib/agent-merge/queue.lock")
SOCKET_PATH = Path("/run/agent-merge/socket")
SIGNAL_SOCKET = Path("/run/signal-notify/socket")


class QueueError(RuntimeError):
    """A request cannot be completed without weakening the boundary."""


@dataclass(frozen=True)
class QueuePaths:
    integration: Path
    approved: Path
    queue: Path
    worktrees: Path
    token: Path = TOKEN_PATH
    lock: Path = LOCK_PATH


def default_paths() -> QueuePaths:
    integration = os.environ.get("AGENT_MERGE_INTEGRATION_ROOT")
    approved = os.environ.get("AGENT_MERGE_APPROVED_ROOT")
    queue = os.environ.get("AGENT_MERGE_QUEUE_ROOT")
    worktrees = os.environ.get("AGENT_MERGE_WORKTREE_ROOT")
    if integration and approved and queue and worktrees:
        return QueuePaths(Path(integration), Path(approved), Path(queue), Path(worktrees))

    from mail_reader.config import workspace_root

    workspace = workspace_root()
    return QueuePaths(
        integration=workspace / "diary-scripts",
        approved=workspace.parent / "diary-scripts-approved",
        queue=workspace.parent / "diary-scripts-automatic",
        worktrees=workspace.parent / "diary-worktrees",
    )


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
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
    out = git(repo, "worktree", "list", "--porcelain").stdout
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
    git(source, "submodule", "deinit", "--force", "--all", safe=True, check=False)
    git(paths.integration, "worktree", "remove", "--force", str(source))
    # The branch is merged into the queue, not necessarily into approved master.
    git(paths.integration, "branch", "-D", branch)


def queue_change(branch: str, paths: QueuePaths) -> bool:
    """Copy and merge one agent branch into the isolated queue. Never push."""
    if not BRANCH_RE.fullmatch(branch):
        raise QueueError("expected branch dev/<slug> or mem/<slug>")
    with queue_lock(paths):
        ensure_queue(paths)
        source = worktree_branch(paths.integration, branch)
        if source is None:
            raise QueueError(f"{branch} is not checked out in a worktree")
        try:
            source.relative_to(paths.worktrees)
        except ValueError as exc:
            raise QueueError(f"source worktree is outside {paths.worktrees}: {source}") from exc
        if not clean(source, safe=True):
            raise QueueError(f"source worktree has uncommitted changes: {source}")

        git(paths.queue, "fetch", "--no-tags", str(paths.integration), branch)
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
        print(f"approve: agent-merge-approve {new}")
        remove_source(paths, branch, source)
        return True


def authenticate(candidate: str, token_path: Path) -> bool:
    try:
        st = token_path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
            return False
        expected = token_path.read_text().strip()
    except OSError:
        return False
    return len(expected) >= 32 and hmac.compare_digest(candidate, expected)


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


def approve_queue(token: str, reviewed: str, paths: QueuePaths, *, push: bool = True) -> bool:
    """Publish exactly the reviewed queue commit. The token is never in argv."""
    if not OID_RE.fullmatch(reviewed):
        raise QueueError("approval requires the full reviewed queue commit OID")
    if not authenticate(token, paths.token):
        raise QueueError("approval token is missing or incorrect")
    with queue_lock(paths):
        ensure_queue(paths)
        actual = git(paths.queue, "rev-parse", "HEAD").stdout.strip()
        if actual != reviewed:
            raise QueueError(f"queue changed after review: reviewed {reviewed}, current {actual}")
        for label, repo in (("approved", paths.approved), ("integration", paths.integration)):
            if not clean(repo):
                raise QueueError(f"{label} checkout has uncommitted changes: {repo}")
        if git(paths.integration, "symbolic-ref", "HEAD").stdout.strip() != "refs/heads/master":
            raise QueueError("integration checkout is not on master")
        if git(paths.approved, "symbolic-ref", "HEAD").stdout.strip() != "refs/heads/master":
            raise QueueError("approved checkout is not on master")

        git(paths.approved, "fetch", "--no-tags", str(paths.queue), reviewed)
        fetched = git(paths.approved, "rev-parse", "FETCH_HEAD").stdout.strip()
        if fetched != reviewed:
            raise QueueError("fetched commit does not match the reviewed OID")
        if git(paths.approved, "merge-base", "--is-ancestor", reviewed, "master", check=False).returncode == 0:
            sync_integration(paths)
            sync_venv(paths)
            if push:
                git(paths.approved, "push", "origin", "master")
            sync_queue(paths)
            print("agent-merge: no new changes; approved, integration and queue are synchronized")
            return False

        base = git(paths.approved, "rev-parse", "--short", "master").stdout.strip()
        print(f"agent-merge: approving {reviewed} into master (was {base})")
        print(git(paths.approved, "diff", "--stat", f"master...{reviewed}").stdout, end="")
        if git(paths.approved, "merge-base", "--is-ancestor", "master", reviewed, check=False).returncode == 0:
            git(paths.approved, "merge", "--ff-only", reviewed)
        else:
            proc = git(paths.approved, "merge", "--no-ff", "--no-edit", "-m", "approve queued changes", reviewed, check=False)
            if proc.returncode:
                git(paths.approved, "merge", "--abort", check=False)
                raise QueueError("approval merge conflict; review and reconcile the queue")
        sync_integration(paths)
        sync_venv(paths)
        if push:
            git(paths.approved, "push", "origin", "master")
        sync_queue(paths)
        new = git(paths.approved, "rev-parse", "--short", "master").stdout.strip()
        print(f"agent-merge: approved master is {new}" + (" and pushed" if push else ""))
        return True


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
    parser.add_argument("reviewed", help="full queue commit OID copied from the reviewed diff")
    args = parser.parse_args(argv)
    if not OID_RE.fullmatch(args.reviewed):
        parser.error("reviewed must be the full 40-character commit OID")
    paths = default_paths()
    reviewed = args.reviewed
    print(f"Review first: git -C {shlex.quote(str(paths.queue))} diff master...{reviewed}")
    print(f"Approving only queue commit: {reviewed}")
    token = getpass.getpass("Approval token from Signal: ")
    if not token:
        print("agent-merge-approve: empty token", file=sys.stderr)
        return 1
    try:
        output = request(f"approve scripts {reviewed} {token}")
    except OSError as exc:
        print(f"agent-merge-approve: cannot reach merge socket: {exc}", file=sys.stderr)
        return 2
    print(output, end="")
    results = [line for line in output.splitlines() if line.startswith("RESULT ")]
    return 0 if results and results[-1].startswith("RESULT ok") else 1


def write_token(path: Path) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return token


def token_for_notification(path: Path, *, rotate: bool = False) -> str:
    if rotate or not path.exists():
        return write_token(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
        raise QueueError(f"token file must be a regular 0600 file owned by the caller: {path}")
    token = path.read_text().strip()
    if len(token) < 32:
        raise QueueError("token file is invalid; rotate the token")
    return token


def signal_note(message: str, socket_path: Path = SIGNAL_SOCKET, timeout: float = 15.0) -> None:
    request_obj = {
        "jsonrpc": "2.0", "id": 1, "method": "send",
        "params": {"noteToSelf": True, "message": message},
    }
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps(request_obj).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise QueueError("Signal proxy closed without a reply")
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0])
        if "error" in reply:
            raise QueueError(f"Signal proxy error: {reply['error']}")
    finally:
        sock.close()


def notify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deliver the merge-approval token to Signal note-to-self.")
    parser.add_argument("--rotate", action="store_true", help="replace the persistent token before sending")
    args = parser.parse_args(argv)
    try:
        token = token_for_notification(TOKEN_PATH, rotate=args.rotate)
        signal_note(
            "agent-merge approval token\n\n"
            "Review the queued diff in your terminal before using this token with "
            f"agent-merge-approve.\n\n{token}"
        )
    except (OSError, QueueError, json.JSONDecodeError) as exc:
        print(f"agent-merge-token-notify: {exc}", file=sys.stderr)
        return 1
    print("agent-merge-token-notify: sent approval token to Signal note-to-self")
    return 0


def queue_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate the helper-code merge queue.")
    sub = parser.add_subparsers(dest="command", required=True)
    queued = sub.add_parser("queue", help="merge an agent branch without publishing")
    queued.add_argument("branch")
    approved = sub.add_parser("approve", help="publish one reviewed queue OID; token comes from stdin")
    approved.add_argument("reviewed")
    approved.add_argument("--token-stdin", action="store_true", required=True)
    notify = sub.add_parser("notify", help="deliver the approval token to Signal")
    notify.add_argument("--rotate", action="store_true")
    client = sub.add_parser("client", help="prompt for a token and request approval")
    client.add_argument("reviewed")
    args = parser.parse_args(argv)
    if args.command == "notify":
        return notify_main(["--rotate"] if args.rotate else [])
    if args.command == "client":
        return approve_main([args.reviewed])
    try:
        if args.command == "queue":
            queue_change(args.branch, default_paths())
        else:
            approve_queue(sys.stdin.readline().strip(), args.reviewed, default_paths())
    except QueueError as exc:
        print(f"agent-merge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    invoked_as = Path(sys.argv[0]).name
    if invoked_as == "agent-merge-approve":
        sys.exit(approve_main())
    if invoked_as == "agent-merge-token-notify":
        sys.exit(notify_main())
    sys.exit(queue_main())
