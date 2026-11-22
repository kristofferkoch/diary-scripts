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
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from scripts import agent_merge_queue as queue
from scripts import agent_merge_review as review
from scripts import diary_audit

DecisionAction = Literal["merge", "deny"]
Repo = Literal["scripts", "diary"]
REPOS: tuple[str, ...] = ("scripts", "diary")
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024


class ConsoleError(RuntimeError):
    """A console request cannot be completed without weakening the boundary."""


class OwnerAuthorizationRequired(ConsoleError):
    """The independent owner UI did not supply a valid owner grant."""


class QueueChanged(ConsoleError):
    """The displayed queue OID no longer identifies the current queued state."""


class ApprovedBaseChanged(QueueChanged):
    """Approved master no longer matches the reviewed diff base."""


class PublicationNotStarted(ConsoleError):
    """The decision failed before any repository operation was attempted."""


class PublicationFailed(ConsoleError):
    """Publication was attempted; the state is derived from the repositories."""

    STATES = ("published", "not_published", "unknown")

    def __init__(self, state: str) -> None:
        self.state = state if state in self.STATES else "unknown"
        super().__init__(f"publication outcome: {self.state}")


@dataclass(frozen=True)
class OwnerGrant:
    """A trusted CTL-07 authorization result, never a browser-supplied claim."""

    subject: str
    authorization_id: str


class DecisionAuditor(Protocol):
    def record(self, record: DecisionRecord) -> None:
        """Durably record one decision before reporting success."""


@dataclass(frozen=True)
class DecisionRecord:
    action: DecisionAction
    queue_oid: str
    base_oid: str
    outcome: str
    lock_state: str
    repository: str = "scripts"

    def as_audit_event(self) -> dict[str, str]:
        decisions = {
            "owner_authorized": "allow",
            "merge_completed": "allow",
            "merge_failed": "error",
            "merge_failed_published": "error",
            "merge_failed_not_published": "error",
            "merge_failed_unknown": "error",
            "owner_denied": "deny",
            "queue_changed": "deny",
            "base_changed": "deny",
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
            "base_oid": self.base_oid,
            "repository": self.repository,
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
class ChangedFile:
    path: str
    status: str
    diff: str


@dataclass(frozen=True)
class FileView:
    path: str
    content: str


@dataclass(frozen=True)
class QueueSnapshot:
    queue_oid: str
    base_oid: str
    commits: tuple[QueueCommit, ...]
    diff: str
    advisory: AdvisoryView
    files: tuple[ChangedFile, ...] = ()
    pending: bool = True
    repo: str = "scripts"


@dataclass(frozen=True)
class DecisionResult:
    action: DecisionAction
    queue_oid: str
    base_oid: str
    outcome: str


@dataclass(frozen=True)
class _CurrentQueue:
    oid: str
    base: str
    pending: bool


OwnerAuthorizer = Callable[[DecisionAction, str, str], OwnerGrant]


class Publisher(Protocol):
    def __call__(
        self,
        reviewed: str,
        paths: queue.QueuePaths,
        /,
        *,
        expected_base: str | None = None,
    ) -> bool:
        """Publish only the reviewed queue/base pair."""


def _diff_sections(text: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _changed_files(paths: queue.QueuePaths, base: str, oid: str, diff: str) -> tuple[ChangedFile, ...]:
    raw = queue.git(
        paths.queue, "diff", "--name-status", "-z", "--no-renames", f"{base}...{oid}",
    ).stdout
    fields = raw.split("\0")
    if fields[-1] or (len(fields) - 1) % 2:
        raise ConsoleError("Git returned an unterminated changed-file list")
    entries = [(fields[index], fields[index + 1]) for index in range(0, len(fields) - 1, 2)]
    sections = _diff_sections(diff)
    if len(entries) != len(sections):
        raise ConsoleError("Git diff sections do not match the changed-file list")
    return tuple(
        ChangedFile(path, status, section)
        for (status, path), section in zip(entries, sections, strict=True)
    )


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
        repo: str = "scripts",
    ) -> None:
        if lock_state not in {"locked", "attended"}:
            raise ValueError("console audit lock state must be locked or attended")
        if repo not in REPOS:
            raise ValueError("console repository must be scripts or diary")
        self.repo = repo
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
        if self.repo != "scripts":
            return AdvisoryView(
                "unavailable",
                "The advisory model review covers the diary-scripts queue only. "
                "Review the real diff; owner approval remains available.",
            )
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
                files=_changed_files(self.paths, base, current.oid, diff), repo=self.repo,
            )

    def read_file(self, reviewed_oid: str, reviewed_base: str, path: str) -> FileView:
        if (
            not queue.OID_RE.fullmatch(reviewed_oid)
            or not queue.OID_RE.fullmatch(reviewed_base)
            or not isinstance(path, str)
            or not path
            or len(path.encode("utf-8")) > 4096
        ):
            raise ConsoleError("file review requires full reviewed OIDs and a changed path")
        current = self._current()
        if current.oid != reviewed_oid or current.base != reviewed_base or not current.pending:
            raise QueueChanged("queue or approved master changed before the file was read")
        with queue.queue_lock(self.paths):
            latest = queue.git(self.paths.queue, "rev-parse", "HEAD").stdout.strip()
            base = queue.git(self.paths.approved, "rev-parse", "master").stdout.strip()
            if latest != reviewed_oid or base != reviewed_base:
                raise QueueChanged("queue or approved master changed before the file was read")
            raw_paths = queue.git(
                self.paths.queue, "diff", "--name-only", "-z", "--no-renames",
                f"{reviewed_base}...{reviewed_oid}",
            ).stdout
            changed = {item for item in raw_paths.split("\0") if item}
            if path not in changed:
                raise ConsoleError("only a file changed by the reviewed commit can be opened")
            object_name = f"{reviewed_oid}:{path}"
            if queue.git(self.paths.queue, "cat-file", "-t", object_name).stdout.strip() != "blob":
                raise ConsoleError("the reviewed path is not a regular Git blob")
            try:
                size = int(queue.git(self.paths.queue, "cat-file", "-s", object_name).stdout.strip())
            except ValueError as exc:
                raise ConsoleError("the reviewed file size is invalid") from exc
            if size > MAX_FILE_BYTES:
                raise ConsoleError("the reviewed file is too large to display in the browser")
            try:
                content = queue.git(self.paths.queue, "cat-file", "blob", object_name).stdout
            except UnicodeError as exc:
                raise ConsoleError("binary files cannot be displayed as text") from exc
            if "\x00" in content:
                raise ConsoleError("binary files cannot be displayed as text")
            return FileView(path, content)

    def _record(self, action: DecisionAction, oid: str, base: str, outcome: str) -> None:
        self.auditor.record(
            DecisionRecord(action, oid, base, outcome, self.lock_state, self.repo)
        )

    def decide(self, action: DecisionAction, reviewed_oid: str, reviewed_base: str) -> DecisionResult:
        if (
            action not in {"merge", "deny"}
            or not queue.OID_RE.fullmatch(reviewed_oid)
            or not queue.OID_RE.fullmatch(reviewed_base)
        ):
            raise ConsoleError("decision requires merge or deny and full reviewed queue and base OIDs")
        grant = self.authorizer(action, reviewed_oid, reviewed_base)
        if not isinstance(grant, OwnerGrant):
            raise OwnerAuthorizationRequired("an independent owner grant is required")
        current = self._current()
        if current.oid != reviewed_oid:
            self._record(action, reviewed_oid, reviewed_base, "queue_changed")
            raise QueueChanged("queue changed after review; no repository operation was performed")
        if current.base != reviewed_base:
            self._record(action, reviewed_oid, reviewed_base, "base_changed")
            raise ApprovedBaseChanged("approved master changed after review; no repository operation was performed")
        if not current.pending:
            self._record(action, reviewed_oid, reviewed_base, "queue_changed")
            raise QueueChanged("reviewed queue is already approved; no repository operation was performed")
        if action == "deny":
            self._record(action, reviewed_oid, reviewed_base, "owner_denied")
            return DecisionResult(action, reviewed_oid, reviewed_base, "denied")

        try:
            self._record(action, reviewed_oid, reviewed_base, "owner_authorized")
        except Exception as exc:
            # Nothing has touched a repository yet, and the pre-merge audit is
            # mandatory, so this is a clean refusal rather than an unknown state.
            raise PublicationNotStarted(
                "the decision could not be audited; nothing was published"
            ) from exc
        try:
            self.publisher(reviewed_oid, self.paths, expected_base=reviewed_base)
        except Exception as exc:
            state = self._publication_state(reviewed_oid)
            self._record_best_effort(
                action, reviewed_oid, reviewed_base, f"merge_failed_{state}",
            )
            raise PublicationFailed(state) from exc
        try:
            self._record(action, reviewed_oid, reviewed_base, "merge_completed")
        except Exception as exc:
            # The merge happened; only the completion record is missing.
            raise PublicationFailed("published") from exc
        return DecisionResult(action, reviewed_oid, reviewed_base, "merged")

    def _publication_state(self, reviewed_oid: str) -> str:
        """Derive what actually happened, rather than guessing from the error."""
        try:
            in_approved = queue.git(
                self.paths.approved, "merge-base", "--is-ancestor", reviewed_oid, "master",
                check=False,
            ).returncode == 0
            if in_approved:
                return "published"
            head = queue.git(self.paths.queue, "rev-parse", "HEAD").stdout.strip()
            if head == reviewed_oid:
                return "not_published"
            return "unknown"
        except (OSError, ValueError, RuntimeError):
            return "unknown"

    def _record_best_effort(
        self, action: DecisionAction, oid: str, base: str, outcome: str,
    ) -> None:
        try:
            self._record(action, oid, base, outcome)
        except (OSError, ValueError, RuntimeError):
            return


DIFF_LINE_CLASSES = {
    "diff --git ": "meta", "index ": "meta", "new file": "meta",
    "deleted file": "meta", "old mode": "meta", "new mode": "meta",
    "Binary files": "meta", "+++ ": "meta", "--- ": "meta", "@@": "hunk",
    "+": "add", "-": "del",
}
LANGUAGES = {
    ".bash": "bash", ".c": "c", ".cc": "cpp", ".conf": "ini", ".cpp": "cpp",
    ".css": "css", ".go": "go", ".h": "c", ".html": "xml", ".ini": "ini",
    ".java": "java", ".js": "javascript", ".json": "json", ".jsx": "javascript",
    ".md": "markdown", ".markdown": "markdown", ".nix": "nix", ".py": "python", ".rs": "rust",
    ".sh": "bash", ".sql": "sql", ".toml": "ini", ".ts": "typescript",
    ".tsx": "typescript", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
}


def language_for_path(path: str) -> str:
    if Path(path).name == "flake.lock":
        return "json"
    return LANGUAGES.get(Path(path).suffix.lower(), "plaintext")


def render_diff(text: str, path: str = "") -> str:
    """Escape diff lines and annotate source lines for frontend highlighting."""
    lines = []
    language = language_for_path(path)
    for line in text.splitlines():
        kind = "ctx"
        for prefix, candidate in DIFF_LINE_CLASSES.items():
            if line.startswith(prefix):
                kind = candidate
                break
        syntax = "" if kind in {"meta", "hunk"} else f' data-syntax="{language}"'
        lines.append(f'<span class="dline {kind}"{syntax}>{html.escape(line)}</span>')
    return "".join(lines)


def render_console(
    snapshot: QueueSnapshot | None,
    action_path: str = "/lock/merge/decide",
    csrf_token: str | None = None,
    notice: str | None = None,
    file_path: str = "/lock/merge/file",
) -> str:
    """Render a fixed, escaped HTML fragment for the owner console host.

    The CSRF token is placed only in the two decision forms; it is a
    double-submit value backed by the host's session-bound token store, never
    authority by itself.
    """
    token = "" if csrf_token is None else (
        f'<input type="hidden" name="csrf" value="{html.escape(csrf_token, quote=True)}">'
    )
    if snapshot is None:
        content = "<p>No queued changes require review in this queue.</p>"
    else:
        commits = "".join(
            f"<li><code>{html.escape(item.oid)}</code> {html.escape(item.subject)}</li>"
            for item in snapshot.commits
        )
        disabled = " disabled" if not snapshot.commits and not snapshot.diff else ""
        note = "" if not notice else (
            f'<p class="notice" role="status">{html.escape(notice)}</p>'
        )
        file_sections = []
        for index, item in enumerate(snapshot.files):
            view_link = ""
            if item.status != "D":
                query = urllib.parse.urlencode({
                    "repo": snapshot.repo, "queue_oid": snapshot.queue_oid,
                    "base_oid": snapshot.base_oid, "path": item.path, "anchor": f"file-{index}",
                })
                view_link = f' <a class="view-file" href="{file_path}?{html.escape(query, quote=True)}">View file</a>'
            file_sections.append(f"""
<section class="diff-file" id="file-{index}">
  <h3><code>{html.escape(item.path)}</code> <span class="file-status">{html.escape(item.status)}</span>{view_link}</h3>
  <pre class="diff">{render_diff(item.diff, item.path)}</pre>
</section>""")
        diff_content = "".join(file_sections) or f'<pre class="diff">{render_diff(snapshot.diff)}</pre>'
        content = f"""
<section id="merge-review">
  {note}
  <p class="repo">Queue: <code id="repo">{html.escape(snapshot.repo)}</code>
    ({html.escape("diary repository" if snapshot.repo == "diary" else "diary-scripts tools repository")})</p>
  <h2>Queued commits</h2>
  <p>Reviewed queue OID: <code id="queue-oid">{html.escape(snapshot.queue_oid)}</code></p>
  <p>Approved master OID: <code id="base-oid">{html.escape(snapshot.base_oid)}</code></p>
  <ul>{commits}</ul>
  <h2>Real diff against approved master</h2>
  {diff_content}
  <h2>Advisory local-model report ({html.escape(snapshot.advisory.status)})</h2>
  <pre class="advisory">{html.escape(snapshot.advisory.text)}</pre>
  <p>The report is advisory only. Missing or stale reports do not block owner review or approval.</p>
  <form method="post" action="{html.escape(action_path, quote=True)}">
    {token}
    <input type="hidden" name="repo" value="{html.escape(snapshot.repo, quote=True)}">
    <input type="hidden" name="action" value="merge">
    <input type="hidden" name="queue_oid" value="{html.escape(snapshot.queue_oid, quote=True)}">
    <input type="hidden" name="base_oid" value="{html.escape(snapshot.base_oid, quote=True)}">
    <button type="submit" name="confirm" value="merge"{disabled}>Merge exact reviewed queue/base OIDs</button>
  </form>
  <form method="post" action="{html.escape(action_path, quote=True)}">
    {token}
    <input type="hidden" name="repo" value="{html.escape(snapshot.repo, quote=True)}">
    <input type="hidden" name="action" value="deny">
    <input type="hidden" name="queue_oid" value="{html.escape(snapshot.queue_oid, quote=True)}">
    <input type="hidden" name="base_oid" value="{html.escape(snapshot.base_oid, quote=True)}">
    <button type="submit" name="confirm" value="deny"{disabled}>Deny without merge or push</button>
  </form>
</section>"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'">
<title>Owner merge review</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#1b1b1b}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:0;background:#f6f8fa;border:1px solid #d8dee4;border-radius:6px;overflow:hidden}}pre.advisory{{padding:1rem}}code{{overflow-wrap:anywhere;font-size:0.95em}}form{{display:inline-block;margin-right:1rem}}button{{padding:0.5rem 0.9rem;border-radius:4px;border:1px solid #8b949e;background:#f6f8fa;cursor:pointer}}button[value=merge]{{border-color:#1a7f37;background:#e6ffed}}button[value=deny]{{border-color:#cf222e;background:#ffeef0}}button:disabled{{opacity:0.5;cursor:not-allowed}}.notice{{padding:0.6rem 0.8rem;border-left:4px solid #bf8700;background:#fff8c5;border-radius:4px}}.diff-file{{margin:1rem 0 1.5rem}}.diff-file h3{{display:flex;align-items:center;gap:.6rem;margin-bottom:.4rem}}.file-status{{font-size:.75rem;color:#57606a}}.view-file{{font-size:.85rem;margin-left:auto}}.diff{{padding:0.5rem 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.875rem;line-height:1.45}}.dline{{display:block;padding:0 0.8rem;white-space:pre-wrap}}.dline.add{{background:#e6ffed}}.dline.del{{background:#ffeef0}}.dline.hunk{{background:#eef4ff;color:#0a3069}}.dline.meta{{color:#57606a}}.hljs-keyword,.hljs-selector-tag,.hljs-literal{{color:#cf222e}}.hljs-string,.hljs-attr,.hljs-template-tag,.hljs-template-variable{{color:#0a3069}}.hljs-comment,.hljs-quote{{color:#6e7781;font-style:italic}}.hljs-number,.hljs-symbol,.hljs-bullet{{color:#953800}}.hljs-title,.hljs-title.function_,.hljs-name{{color:#8250df}}.hljs-type,.hljs-built_in,.hljs-variable,.hljs-property{{color:#0550ae}}</style>
</head>
<body><h1>Owner merge review</h1>{content}
<script src="/lock/merge/assets/highlight.min.js" defer></script>
<script src="/lock/merge/assets/nix.min.js" defer></script>
<script src="/lock/merge/assets/review.js" defer></script></body>
</html>
"""
