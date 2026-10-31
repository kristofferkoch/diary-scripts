"""Export freshly collected mail and in-memory collector records to the event spool."""
from __future__ import annotations

import argparse
import email
import json
import subprocess
from email.policy import default as email_default
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.event_spool import EVENT_ID_RE, EventSpool, EventValidationError, build_event
from scripts.mailshow import html_to_text
from scripts.safe_io import atomic_write_text

DEFAULT_SPOOL = Path("/var/lib/diary/events")
DEFAULT_STATE_DIR = Path("/var/lib/diary/collector-state")
MAX_MAIL_BYTES = 64 * 1024 * 1024
MAX_BODY_CHARS = 256 * 1024

NATIVE_ID_KEYS = {
    "spond": ("id", "uid", "chatId"),
    "vigilo": ("threadUid", "postId", "consentId", "noteId"),
    "skolemelding": ("id",),
}


def native_id(source: str, kind: str, data: dict[str, Any]) -> str:
    for key in NATIVE_ID_KEYS[source]:
        value = data.get(key)
        if value is not None and str(value):
            return str(value)
    raise EventValidationError(f"{source}/{kind} record has no native ID")


def emit_records(
    spool_path: Path | str,
    source: str,
    records: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Write fresh in-memory records, never records read back from legacy JSONL."""
    spool = EventSpool(spool_path)
    counts = {"written": 0, "duplicate": 0}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"kind", "fetched_at", "data"}:
            raise EventValidationError("collector record does not match the schema")
        kind = record["kind"]
        data = record["data"]
        if not isinstance(kind, str) or not isinstance(data, dict):
            raise EventValidationError("collector record has invalid kind/data")
        if not isinstance(record["fetched_at"], str):
            raise EventValidationError("collector record has invalid fetched_at")
        event = build_event(source, kind, native_id(source, kind, data), data)
        counts[spool.write(event)] += 1
    return counts


def search_mail_files(query: str = "tag:inbox") -> list[str]:
    result = subprocess.run(
        ["notmuch", "search", "--output=files", "--", query],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return [line for line in result.stdout.splitlines() if line]


def fetch_raw(mail_path: str) -> bytes:
    """Read one notmuch file without buffering more than MAX_MAIL_BYTES + 1."""
    proc = subprocess.Popen(
        ["notmuch", "show", "--format=raw", "--entire-thread=false", f"path:{mail_path}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    chunks: list[bytes] = []
    size = 0
    try:
        assert proc.stdout is not None
        while size <= MAX_MAIL_BYTES:
            chunk = proc.stdout.read(min(128 * 1024, MAX_MAIL_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_MAIL_BYTES:
            proc.kill()
            proc.wait(timeout=5)
            raise EventValidationError("raw mail exceeds the collector limit")
        return_code = proc.wait(timeout=60)
        if return_code:
            raise EventValidationError(f"notmuch raw read failed with exit {return_code}")
        return b"".join(chunks)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def mail_payload(raw: bytes) -> dict[str, Any]:
    message = email.message_from_bytes(raw, policy=email_default)
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        text = ""
    else:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = html_to_text(text)
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[body truncated by trusted collector]"
    attachments = []
    for part in message.walk():
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if "attachment" in disposition or filename:
            attachments.append({
                "filename": filename or "(unnamed)",
                "content_type": part.get_content_type(),
            })
    return {
        "message_id": str(message.get("Message-ID", "")),
        "date": str(message.get("Date", "")),
        "from": str(message.get("From", "")),
        "to": str(message.get("To", "")),
        "subject": str(message.get("Subject", "")),
        "body": text,
        "attachments": attachments,
    }


class MailJournal:
    """Durable discovery, completion, and quarantine state independent of inbox tags."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending: dict[str, str] = {}
        self.completed: dict[str, str] = {}
        self.failed: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise EventValidationError("unreadable mail event journal") from exc
        if not isinstance(data, dict) or set(data) != {"version", "pending", "completed", "failed"} \
                or data["version"] != 1:
            raise EventValidationError("invalid mail event journal")
        for key in ("pending", "completed", "failed"):
            values = data[key]
            if not isinstance(values, dict) or not all(
                isinstance(path, str) and path and isinstance(value, str)
                for path, value in values.items()
            ):
                raise EventValidationError(f"invalid mail journal {key}")
        if any(not EVENT_ID_RE.fullmatch(value) for value in data["completed"].values()):
            raise EventValidationError("invalid completed mail event ID")
        self.pending = dict(data["pending"])
        self.completed = dict(data["completed"])
        self.failed = dict(data["failed"])

    def save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps({
                "version": 1,
                "pending": self.pending,
                "completed": self.completed,
                "failed": self.failed,
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n",
            mode=0o600,
        )


def export_mail(
    spool_path: Path | str = DEFAULT_SPOOL,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    search_files: Callable[[str], list[str]] = search_mail_files,
    raw_fetcher: Callable[[str], bytes] = fetch_raw,
) -> dict[str, int]:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    journal = MailJournal(state_dir / "mail-event-journal.json")
    spool = EventSpool(spool_path)
    counts = {"written": 0, "duplicate": 0, "failed": 0, "quarantined": 0}
    discovered = set(search_files("tag:inbox"))
    paths = sorted(discovered | set(journal.pending))

    pending_changed = False
    for mail_path in discovered:
        if mail_path not in journal.completed and mail_path not in journal.failed \
                and mail_path not in journal.pending:
            journal.pending[mail_path] = ""
            pending_changed = True
    if pending_changed:
        journal.save()

    for mail_path in paths:
        if mail_path in journal.failed:
            counts["quarantined"] += 1
            continue
        if mail_path in journal.completed and mail_path not in journal.pending:
            event_id = journal.completed[mail_path]
            if spool.contains(event_id):
                counts["duplicate"] += 1
                continue
            del journal.completed[mail_path]
            journal.pending[mail_path] = ""
            journal.save()
        try:
            payload = mail_payload(raw_fetcher(mail_path))
            event = build_event("mail", "message", mail_path, payload)
        except Exception as exc:
            journal.pending.pop(mail_path, None)
            journal.failed[mail_path] = str(exc)[:500]
            journal.save()
            counts["failed"] += 1
            continue
        status = spool.write(event)
        journal.completed[mail_path] = event["event_id"]
        journal.pending.pop(mail_path, None)
        journal.failed.pop(mail_path, None)
        journal.save()
        counts[status] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["mail"])
    parser.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    counts = export_mail(args.spool, args.state_dir)
    print(
        f"mail events: written={counts['written']} duplicate={counts['duplicate']} "
        f"failed={counts['failed']} quarantined={counts['quarantined']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
