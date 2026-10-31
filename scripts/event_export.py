"""Export freshly collected mail and in-memory collector records to the event spool."""
from __future__ import annotations

import argparse
import email
import subprocess
from email.policy import default as email_default
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.event_spool import EventSpool, EventValidationError, build_event
from scripts.mailshow import html_to_text, notmuch_search_ids

DEFAULT_SPOOL = Path("/var/lib/diary/events")
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


def fetch_raw(message_id: str) -> bytes:
    result = subprocess.run(
        ["notmuch", "show", "--format=raw", f"id:{message_id}"],
        check=True, capture_output=True, timeout=60,
    )
    if len(result.stdout) > MAX_MAIL_BYTES:
        raise EventValidationError("raw mail exceeds the collector limit")
    return result.stdout


def mail_payload(message_id: str, raw: bytes) -> dict[str, Any]:
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
        "message_id": message_id,
        "date": str(message.get("Date", "")),
        "from": str(message.get("From", "")),
        "to": str(message.get("To", "")),
        "subject": str(message.get("Subject", "")),
        "body": text,
        "attachments": attachments,
    }


def export_mail(
    spool_path: Path | str = DEFAULT_SPOOL,
    *,
    search_ids: Callable[[str, int | None], list[str]] = notmuch_search_ids,
    raw_fetcher: Callable[[str], bytes] = fetch_raw,
) -> dict[str, int]:
    spool = EventSpool(spool_path)
    counts = {"written": 0, "duplicate": 0}
    for message_id in search_ids("tag:inbox", None):
        event_id = build_event(
            "mail", "message", message_id, {}, revision="notmuch-v1",
        )["event_id"]
        if spool.contains(event_id):
            counts["duplicate"] += 1
            continue
        event = build_event(
            "mail", "message", message_id,
            mail_payload(message_id, raw_fetcher(message_id)), revision="notmuch-v1",
        )
        counts[spool.write(event)] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["mail"])
    parser.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    args = parser.parse_args(argv)
    counts = export_mail(args.spool)
    print(f"mail events: written={counts['written']} duplicate={counts['duplicate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
