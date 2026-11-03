"""Layer 1 of the pre-production harness: the real protocol stack, unprivileged.

Everything the harness writes lives under one scratch root. It refuses to start
when a path would escape that root or would touch production state, so a test can
never mutate the owner's diary, the installed units, the audit journal, or the
live queues.

What runs for real here:

* ``AuditDaemon`` with its socket handler, dedup store, and native-journal
  writer, against a Unix-datagram sink that records each field dictionary;
* ``diary_merge_console_service.handle_request`` — the same decide path the
  socket-activated executor serves, including the single-use grant check and
  ``approve_queue`` publication into scratch Git repositories;
* scratch ``queue/automatic``, approved, and integration checkouts with a real
  bare origin, so a merge fast-forwards, synchronises, and pushes for real.

The audit daemon's ``daemon_main`` refuses to run without root, so the harness
constructs the same ``AuditDaemon`` class in-process; the root-only wrapper is
the only thing skipped, not the handler, the validation, or the journal write.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts import agent_merge_console as console
from scripts import agent_merge_queue as queue
from scripts import diary_audit as audit

#: Production state the harness must never touch, even by accident.
FORBIDDEN_PREFIXES = (
    "/home/kek/diary",
    "/home/kek/diary-scripts-approved",
    "/etc/diary",
    "/etc/authelia",
    "/run/diary-",
    "/run/systemd/journal",
    "/var/lib/diary",
    "/usr/local/lib/diary",
    "/usr/local/sbin/diary",
    "/usr/local/bin/diary",
)


class HarnessError(RuntimeError):
    """The scratch contract was violated."""


@dataclass
class Scratch:
    """A single test's scratch root; every written path must stay inside it."""

    root: Path

    @classmethod
    def create(cls, base: Path) -> "Scratch":
        root = (base / "e2e").resolve()
        root.mkdir(parents=True, exist_ok=False)
        # The audit daemon refuses a state or socket directory that is group or
        # world writable; keep the scratch root private like /run/diary-audit.
        os.chmod(root, 0o700)
        return cls(root)

    def path(self, *parts: str) -> Path:
        candidate = (self.root.joinpath(*parts)).resolve()
        if root_escapes(candidate, self.root):
            raise HarnessError(f"path escapes the scratch root: {candidate}")
        return candidate

    def directory(self, *parts: str) -> Path:
        target = self.path(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def root_escapes(candidate: Path, root: Path) -> bool:
    """True when a resolved path is outside the scratch root or forbidden."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    text = str(candidate.resolve())
    return any(text == prefix or text.startswith(prefix + "/") for prefix in FORBIDDEN_PREFIXES)


def run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return run("git", *args, cwd=repo)


def commit_file(repo: Path, name: str, text: str, message: str | None = None) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", message or f"add {name}")
    return git(repo, "rev-parse", "HEAD")


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "E2E Harness")
    git(repo, "config", "user.email", "e2e@example.invalid")
    commit_file(repo, "README.md", "scratch\n", "initial")


def scratch_paths(scratch: Scratch, name: str) -> queue.QueuePaths:
    """Build a scratch integration/approved/queue triple with a bare origin."""
    base = scratch.root / name
    integration = base / "integration"
    approved = base / "approved"
    queue_repo = base / "automatic"
    origin = base / "origin.git"
    worktrees = base / "worktrees"

    init_repo(integration)
    run("git", "init", "--bare", str(origin))
    run("git", "clone", "--no-hardlinks", str(integration), str(approved))
    git(approved, "config", "user.name", "E2E Harness")
    git(approved, "config", "user.email", "e2e@example.invalid")
    git(approved, "remote", "set-url", "origin", str(origin))
    git(approved, "push", "-u", "origin", "master")
    run("git", "clone", "--no-hardlinks", str(approved), str(queue_repo))
    git(queue_repo, "config", "user.name", "E2E Harness")
    git(queue_repo, "config", "user.email", "e2e@example.invalid")
    git(queue_repo, "checkout", "-b", "queue/automatic", "master")
    worktrees.mkdir(parents=True, exist_ok=True)

    review_config = scratch.path(f"{name}-review.toml")
    review_config.write_text('[review]\nmodel = "local-test"\n')
    return queue.QueuePaths(
        integration=integration,
        approved=approved,
        queue=queue_repo,
        worktrees=worktrees,
        lock=scratch.path(f"{name}-queue.lock"),
        review_config=review_config,
    )


def enqueue_change(paths: queue.QueuePaths, name: str = "queued.txt", text: str = "queued\n") -> str:
    """Create the queued commit a decision will review, and return its OID."""
    if git(paths.queue, "symbolic-ref", "HEAD") != "refs/heads/queue/automatic":
        raise HarnessError("scratch queue is not on queue/automatic")
    return commit_file(paths.queue, name, text)


class JournalSink:
    """Unix-datagram sink standing in for the native journal namespace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.bind(str(path))
        self._socket.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._socket.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            fields: dict[str, str] = {}
            for line in payload.decode("ascii", "replace").splitlines():
                key, _, value = line.partition("=")
                if key:
                    fields[key] = value
            if fields:
                with self._lock:
                    self.events.append(fields)

    def snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=2)


@dataclass
class AuditHandle:
    """The daemon's real service stack, driven without its socket.

    ``AuditDaemon.authorized`` refuses any peer whose UID resolves to ``agent``
    (that is the production boundary, not an accident), and an unprivileged test
    cannot present another UID. So the harness drives the same
    ``AuditService.write`` the socket handler calls — validation, dedup store,
    journal field mapping, and the native-journal datagram — and leaves the
    socket peer-authorization layer to the root-only L4 rehearsal.
    """

    scratch: Scratch
    service: audit.AuditService
    state_path: Path
    sink: JournalSink
    peer: dict[str, int]

    def write(self, event: dict[str, Any]) -> dict[str, Any]:
        return self.service.write(event, self.peer)

    def events(self) -> list[dict[str, str]]:
        return self.sink.snapshot()

    def close(self) -> None:
        self.sink.close()


def start_audit(
    scratch: Scratch,
    *,
    state: Path | None = None,
) -> AuditHandle:
    """Start the audit service stack against a scratch sink and dedup state."""
    journal_path = scratch.path("journal.sock")
    sink = JournalSink(journal_path)
    state_path = state or scratch.path("dedup.json")
    service = audit.AuditService(
        audit.DedupStore(str(state_path)),
        audit.JournalWriter(str(journal_path)),
    )
    peer = {"pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid()}
    return AuditHandle(scratch, service, state_path, sink, peer)


class StubVerifier:
    """Grant verifier double that mirrors the real per-repository binding."""

    def __init__(self, repo: str = "scripts", transaction: str | None = None) -> None:
        self.repo = repo
        self.transaction = transaction
        self.calls: list[tuple[str, str, str, str, str]] = []

    def verify(
        self, repo: str, action: str, queue_oid: str, base_oid: str, transaction: str,
    ) -> bool:
        self.calls.append((repo, action, queue_oid, base_oid, transaction))
        if repo != self.repo:
            return False
        return self.transaction is None or self.transaction == transaction


def decision_audit(handle: AuditHandle) -> console.DecisionAuditor:
    return console.DiaryDecisionAudit(str(handle.socket_path))


def publish_without_venv(
    reviewed: str, paths: queue.QueuePaths, /, *, expected_base: str | None = None,
) -> bool:
    """Publication exactly as the console performs it, minus the venv sync."""
    return queue.approve_queue(
        reviewed, paths, expected_base=expected_base, sync_environment=False,
    )


@dataclass
class Stack:
    """The real decide path wired to scratch state."""

    scratch: Scratch
    scripts: queue.QueuePaths
    diary: queue.QueuePaths
    audit: AuditHandle
    verifier: StubVerifier = field(default_factory=StubVerifier)

    @property
    def paths_by_repo(self) -> dict[str, queue.QueuePaths]:
        return {"scripts": self.scripts, "diary": self.diary}

    def decide(
        self, repo: str, action: str, queue_oid: str, base_oid: str,
        transaction: str = "e2e-transaction",
    ) -> dict[str, Any]:
        from scripts import diary_merge_console_service as executor

        request = {
            "cmd": "decide",
            "repo": repo,
            "action": action,
            "queue_oid": queue_oid,
            "base_oid": base_oid,
            "transaction": transaction,
        }
        try:
            return executor.handle_request(
                request,
                self.paths_by_repo,
                self.verifier,
                auditor=decision_audit(self.audit),
                publisher=publish_without_venv,
                lock_state="locked",
            )
        except Exception as exc:  # mirrored to the socket reply the console sees
            return executor.error_reply(exc)

    def close(self) -> None:
        self.audit.close()


def build_stack(base: Path) -> Stack:
    scratch = Scratch.create(base)
    try:
        scripts = scratch_paths(scratch, "scripts")
        diary = scratch_paths(scratch, "diary")
        handle = start_audit(scratch)
    except Exception:
        scratch.cleanup()
        raise
    return Stack(scratch, scripts, diary, handle)
