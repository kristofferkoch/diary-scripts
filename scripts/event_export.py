"""Export freshly collected mail and in-memory collector records to the event spool."""
from __future__ import annotations

import argparse
import email
import json
import os
import stat
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


class MailContentError(EventValidationError):
    """Deterministically malformed or oversized mail content."""


def search_mail_messages(query: str = "tag:inbox") -> list[str]:
    result = subprocess.run(
        ["notmuch", "search", "--output=messages", "--", query],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return [line.removeprefix("id:") for line in result.stdout.splitlines() if line]


def _message_file_nodes(node: Any) -> Iterable[list[str]]:
    if isinstance(node, dict):
        if node.get("match") is True and isinstance(node.get("filename"), list):
            yield [str(path) for path in node["filename"]]
        for value in node.values():
            yield from _message_file_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _message_file_nodes(value)


def mail_message_files(message_id: str) -> list[str]:
    result = subprocess.run(
        ["notmuch", "show", "--format=json", "--body=false", "--entire-thread=false",
         "--", f"id:{message_id}"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    document = json.loads(result.stdout)
    return sorted({path for paths in _message_file_nodes(document) for path in paths})


def canonical_inbox_files(paths: Iterable[str]) -> list[str]:
    """Select physical INBOX copies, excluding Proton's All Mail duplicates."""
    return sorted(
        path for path in paths
        if "INBOX" in Path(path).parts and "All Mail" not in Path(path).parts
    )


def fetch_raw(mail_path: str) -> bytes:
    """Read one regular mail file directly, without links or unbounded buffering."""
    fd = os.open(mail_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise MailContentError("mail source is not a regular file")
        if before.st_size > MAX_MAIL_BYTES:
            raise MailContentError("raw mail exceeds the collector limit")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_MAIL_BYTES:
            chunk = os.read(fd, min(128 * 1024, MAX_MAIL_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        current = os.stat(mail_path, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise OSError("mail file changed while being read")
        if size > MAX_MAIL_BYTES:
            raise MailContentError("raw mail exceeds the collector limit")
        return b"".join(chunks)
    finally:
        os.close(fd)


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


def quarantine_key(message_id: str, mail_path: str) -> str:
    return message_id + "\n" + mail_path


class MailJournal:
    """Durable logical-message discovery, completion, and quarantine state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending: dict[str, str] = {}
        self.completed: dict[str, list[str]] = {}
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
                or type(data["version"]) is not int or data["version"] != 2:
            raise EventValidationError("invalid mail event journal")
        for key in ("pending", "failed"):
            values = data[key]
            if not isinstance(values, dict) or not all(
                isinstance(item, str) and item and isinstance(value, str)
                for item, value in values.items()
            ):
                raise EventValidationError(f"invalid mail journal {key}")
        if not isinstance(data["completed"], dict):
            raise EventValidationError("invalid mail journal completed")
        for message_id, event_ids in data["completed"].items():
            if not isinstance(message_id, str) or not message_id \
                    or not isinstance(event_ids, list) \
                    or not all(isinstance(value, str) and EVENT_ID_RE.fullmatch(value)
                               for value in event_ids):
                raise EventValidationError("invalid completed mail event IDs")
        self.pending = dict(data["pending"])
        self.completed = {key: list(value) for key, value in data["completed"].items()}
        self.failed = dict(data["failed"])

    def save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps({
                "version": 2,
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
    search_messages: Callable[[str], list[str]] = search_mail_messages,
    message_files: Callable[[str], list[str]] = mail_message_files,
    raw_fetcher: Callable[[str], bytes] = fetch_raw,
) -> dict[str, int]:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    journal = MailJournal(state_dir / "mail-event-journal.json")
    spool = EventSpool(spool_path)
    counts = {"written": 0, "duplicate": 0, "failed": 0, "quarantined": 0, "retry": 0}
    discovered = set(search_messages("tag:inbox"))

    for message_id in sorted(discovered | set(journal.pending)):
        try:
            current_files = message_files(message_id)
        except (OSError, subprocess.SubprocessError, ValueError):
            journal.pending.setdefault(message_id, "")
            journal.save()
            counts["retry"] += 1
            continue
        paths = canonical_inbox_files(current_files) if message_id in discovered else current_files
        if not paths:
            if message_id in journal.pending:
                counts["retry"] += 1
            continue
        journal.pending.setdefault(message_id, "")
        journal.save()
        transient = False
        for mail_path in paths:
            failed_key = quarantine_key(message_id, mail_path)
            if failed_key in journal.failed:
                counts["quarantined"] += 1
                continue
            try:
                raw = raw_fetcher(mail_path)
                event = build_event("mail", "message", message_id, mail_payload(raw))
            except (ValueError, TypeError) as exc:
                journal.failed[failed_key] = str(exc)[:500]
                journal.save()
                counts["failed"] += 1
                continue
            except (OSError, subprocess.SubprocessError, TimeoutError):
                transient = True
                counts["retry"] += 1
                continue
            status = spool.write(event)
            completed = journal.completed.setdefault(message_id, [])
            if event["event_id"] not in completed:
                completed.append(event["event_id"])
                completed.sort()
            journal.save()
            counts[status] += 1
        if not transient:
            journal.pending.pop(message_id, None)
            journal.save()
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
        f"failed={counts['failed']} quarantined={counts['quarantined']} retry={counts['retry']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
