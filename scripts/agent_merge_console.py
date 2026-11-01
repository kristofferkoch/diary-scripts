#!/usr/bin/python3
"""Owner-authorized merge/deny console extension for the scripts queue.

CTL-07 supplies the HTTP, same-origin, and fresh-owner authorization adapters.
This module renders trusted Git data, requires an owner grant for every decision,
and emits trusted audit metadata. Only Merge calls the existing exact-OID
publication path; Deny records its decision without changing any repository.
"""
from __future__ import annotations

import html
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from scripts import agent_merge_queue as queue
from scripts import agent_merge_review as review
from scripts import diary_audit

DecisionAction = Literal["merge", "deny"]
MAX_REPORT_BYTES = 2 * 1024 * 1024


class ConsoleError(RuntimeError):
    """A console request cannot be completed without weakening the boundary."""


class OwnerAuthorizationRequired(ConsoleError):
    """The independent owner UI did not supply a valid owner grant."""


class QueueChanged(ConsoleError):
    """The displayed queue OID no longer identifies the current queued state."""


@dataclass(frozen=True)
class OwnerGrant:
    """A trusted CTL-07 authorization result, never a browser-supplied claim."""

    subject: str
    authorization_id: str


class DecisionAuditor(Protocol):
    def record(self, record: "DecisionRecord") -> None:
        """Durably record one decision before reporting success."""


@dataclass(frozen=True)
class DecisionRecord:
    action: DecisionAction
    queue_oid: str
    base_oid: str
    outcome: str
    lock_state: str

    def as_audit_event(self) -> dict[str, str]:
        decisions = {
            "owner_authorized": "allow",
            "merge_completed": "allow",
            "merge_failed": "error",
            "owner_denied": "deny",
            "queue_changed": "deny",
        }
        if self.outcome not in decisions:
            raise ConsoleError("unknown console audit outcome")
        return {
            "event": "merge_console.decision",
            "component": "diary-lock-ui",
            "operation": self.action,
            "decision": decisions[self.outcome],
            "lock_state": self.lock_state,
            "reason": self.outcome,
            "event_id": self.queue_oid,
        }


class DiaryDecisionAudit:
    """Fail-closed adapter for the trusted metadata-only audit daemon."""

    def __init__(self, socket_path: str = diary_audit.DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    def record(self, record: DecisionRecord) -> None:
        diary_audit.request(self.socket_path, {"cmd": "write", "event": record.as_audit_event()})


@dataclass(frozen=True)
class QueueCommit:
    oid: str
    subject: str


@dataclass(frozen=True)
class AdvisoryView:
    status: str
    text: str


@dataclass(frozen=True)
class QueueSnapshot:
    queue_oid: str
    base_oid: str
    commits: tuple[QueueCommit, ...]
    diff: str
    advisory: AdvisoryView
    pending: bool = True


@dataclass(frozen=True)
class DecisionResult:
    action: DecisionAction
    queue_oid: str
    outcome: str


@dataclass(frozen=True)
class _CurrentQueue:
    oid: str
    base: str
    pending: bool


OwnerAuthorizer = Callable[[DecisionAction, str], OwnerGrant]
Publisher = Callable[[str, queue.QueuePaths], bool]


class MergeConsole:
    """A UI-neutral LATER-01 boundary with no agent-session authority."""

    def __init__(
        self,
        paths: queue.QueuePaths,
        authorizer: OwnerAuthorizer,
        auditor: DecisionAuditor,
        *,
        report_dir: Path = review.DEFAULT_REPORT_DIR,
        publisher: Publisher = queue.approve_queue,
        lock_state: str = "locked",
    ) -> None:
        if lock_state not in {"locked", "attended"}:
            raise ValueError("console audit lock state must be locked or attended")
        self.paths = paths
        self.authorizer = authorizer
        self.auditor = auditor
        self.report_dir = report_dir
        self.publisher = publisher
        self.lock_state = lock_state

    def _current(self) -> _CurrentQueue:
        with queue.queue_lock(self.paths):
            queue.ensure_queue(self.paths)
            oid = queue.git(self.paths.queue, "rev-parse", "HEAD").stdout.strip()
            base = queue.git(self.paths.approved, "rev-parse", "master").stdout.strip()
            if not queue.OID_RE.fullmatch(oid) or not queue.OID_RE.fullmatch(base):
                raise ConsoleError("queue and approved master must resolve to full commit OIDs")
            queue.git(self.paths.queue, "cat-file", "-e", f"{base}^{{commit}}")
            already_approved = queue.git(
                self.paths.queue, "merge-base", "--is-ancestor", oid, base, check=False,
            ).returncode == 0
            return _CurrentQueue(oid, base, not already_approved)

    def _advisory(self, oid: str, base: str) -> AdvisoryView:
        path = self.report_dir / f"{oid}.json"
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
                raise review.ReviewError("advisory report is not a bounded regular file")
            report = review.load_report(path)
            if report.get("queue_oid") != oid or report.get("base_oid") != base or report.get("stale") is not False:
                return AdvisoryView("stale", "The advisory report is stale. Review the real diff; owner approval remains available.")
            if report.get("repository") != "diary-scripts" or report.get("approval") != "owner-required":
                raise review.ReviewError("advisory report has an invalid approval or repository binding")
            return AdvisoryView("current", review.render_report(report))
        except FileNotFoundError:
            return AdvisoryView("missing", "No advisory report is available. Review the real diff; owner approval remains available.")
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, review.ReviewError) as exc:
            return AdvisoryView("unavailable", f"Advisory report unavailable ({type(exc).__name__}). Review the real diff; owner approval remains available.")

    def snapshot(self) -> QueueSnapshot | None:
        current = self._current()
        if not current.pending:
            return None
        with queue.queue_lock(self.paths):
            queue.ensure_queue(self.paths)
            latest = queue.git(self.paths.queue, "rev-parse", "HEAD").stdout.strip()
            base = queue.git(self.paths.approved, "rev-parse", "master").stdout.strip()
            if latest != current.oid or base != current.base:
                raise QueueChanged("queue or approved master changed while preparing the review")
            raw_commits = queue.git(
                self.paths.queue, "log", "--format=%H%x00%s", f"{base}..{current.oid}",
            ).stdout
            commits = tuple(
                QueueCommit(oid, subject)
                for oid, subject in (line.split("\0", 1) for line in raw_commits.splitlines())
            )
            diff = queue.git(
                self.paths.queue,
                "diff", "--no-ext-diff", "--no-textconv", "--no-renames", f"{base}...{current.oid}",
            ).stdout
            return QueueSnapshot(
                current.oid, base, commits, diff, self._advisory(current.oid, base),
            )

    def _record(self, action: DecisionAction, oid: str, base: str, outcome: str) -> None:
        self.auditor.record(DecisionRecord(action, oid, base, outcome, self.lock_state))

    def decide(self, action: DecisionAction, reviewed_oid: str) -> DecisionResult:
        if action not in {"merge", "deny"} or not queue.OID_RE.fullmatch(reviewed_oid):
            raise ConsoleError("decision requires merge or deny and a full reviewed queue OID")
        grant = self.authorizer(action, reviewed_oid)
        if not isinstance(grant, OwnerGrant):
            raise OwnerAuthorizationRequired("an independent owner grant is required")
        current = self._current()
        if current.oid != reviewed_oid or not current.pending:
            self._record(action, reviewed_oid, current.base, "queue_changed")
            raise QueueChanged("queue changed after review; no repository operation was performed")
        if action == "deny":
            self._record(action, reviewed_oid, current.base, "owner_denied")
            return DecisionResult(action, reviewed_oid, "denied")

        self._record(action, reviewed_oid, current.base, "owner_authorized")
        try:
            self.publisher(reviewed_oid, self.paths)
        except Exception:
            self._record(action, reviewed_oid, current.base, "merge_failed")
            raise
        self._record(action, reviewed_oid, current.base, "merge_completed")
        return DecisionResult(action, reviewed_oid, "merged")


def render_console(snapshot: QueueSnapshot | None, action_path: str = "/lock/merge/decide") -> str:
    """Render a fixed, escaped HTML fragment for CTL-07's independent owner UI."""
    if snapshot is None:
        content = "<p>No queued diary-scripts changes require review.</p>"
    else:
        commits = "".join(
            f"<li><code>{html.escape(item.oid)}</code> {html.escape(item.subject)}</li>"
            for item in snapshot.commits
        )
        disabled = " disabled" if not snapshot.commits and not snapshot.diff else ""
        content = f"""
<section id="merge-review">
  <h2>Queued commits</h2>
  <p>Reviewed queue OID: <code id="queue-oid">{html.escape(snapshot.queue_oid)}</code></p>
  <p>Approved master OID: <code id="base-oid">{html.escape(snapshot.base_oid)}</code></p>
  <ul>{commits}</ul>
  <h2>Real diff against approved master</h2>
  <pre class="diff">{html.escape(snapshot.diff)}</pre>
  <h2>Advisory local-model report ({html.escape(snapshot.advisory.status)})</h2>
  <pre class="advisory">{html.escape(snapshot.advisory.text)}</pre>
  <p>The report is advisory only. Missing or stale reports do not block owner review or approval.</p>
  <form method="post" action="{html.escape(action_path, quote=True)}">
    <input type="hidden" name="action" value="merge">
    <input type="hidden" name="queue_oid" value="{html.escape(snapshot.queue_oid, quote=True)}">
    <button type="submit" name="confirm" value="merge"{disabled}>Merge exact reviewed OID</button>
  </form>
  <form method="post" action="{html.escape(action_path, quote=True)}">
    <input type="hidden" name="action" value="deny">
    <input type="hidden" name="queue_oid" value="{html.escape(snapshot.queue_oid, quote=True)}">
    <button type="submit" name="confirm" value="deny"{disabled}>Deny without merge or push</button>
  </form>
</section>"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'">
<title>Owner merge review</title>
<style>body{{font-family:sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:1rem;background:#f4f4f4}}code{{overflow-wrap:anywhere}}form{{display:inline-block;margin-right:1rem}}</style>
</head>
<body><h1>diary-scripts owner merge review</h1>{content}</body>
</html>
"""
