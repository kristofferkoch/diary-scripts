#!/usr/bin/env python3
"""skolemelding-sync — pull Skolemelding (Skoleplattform Oslo) messages into JSONL.

Sink of the Skolemelding pipeline; the reader is ``skolemeldingshow``.
Skeleton follows ``vigilo_sync``: ``--once``, one stderr line, state bumped
only on a clean run. Simpler than Vigilo: a single account inbox, one message
kind, and the search rows already carry the full record (no detail fetch).

The search (``POST /api/messagesearchmobile/``) pages the inbox newest-first
and **returns some messages twice** — everything is deduped by ``Id``. "New"
and "changed" are our own judgements, made from a content signature over the
school-controlled fields only (:func:`message_signature`): an ``Id`` we have
not seen is a ``message``, one whose signature moved is a ``message_update``.
Read-receipts (``IsMessageSeen``/``IsFavourite``) and the feedback counters
(``TotalYes``/``TotalNo``/``TotalNotAnswered`` — they move when *other*
parents answer) are deliberately excluded, or every poll would re-emit
messages nobody at the school touched.

Output: ``memory/skolemelding/YYYY-MM-DD.jsonl``, one record per line::

    {"kind": "message"|"message_update", "fetched_at": "…Z", "data": {…}}

``data`` is distilled (id, header, sender, timestamp, plain-text body,
threads, attachments as name + local path + portal source path) — HTML bodies
go through ``embed_mail.html_to_text`` so the reader and the embedder see
text, not markup. Attachments are downloaded immediately to
``memory/skolemelding/attachments/<Id>/<name>`` (git-ignored in the diary
repo); their portal paths are stable and Bearer-gated, so recording them in
the JSONL leaks nothing usable.

State: ``memory/skolemelding-state.json``::

    {
      "messages": { "<Id>": {"sig": "…", "timestamp": "…"} },
      "attachment_retries": { "<Id>": 1 },
      "cursor": "…",                  # triage-owned — only skolemeldingshow --bump
      "auth_required": false,
      "last_successful_run": "…Z"
    }

A message whose attachment download failed is **not** recorded as fully
seen — it is retried for a few cycles and only then given up on with a note
(same lesson as Vigilo: marking seen anyway loses the file forever).

Auth loss is a normal condition, not a crash: ``auth_required`` is set, the
cursor is left alone, and we **exit 0** so a sjekk never breaks.

Examples::

    uv run skolemelding-sync --once
    uv run skolemelding-sync --once --dry-run
    uv run skolemelding-sync --once --force        # ignore the 5-minute poll floor
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from mail_reader.config import cfg, workspace_root

from scripts.attachment_store import find_attachment, write_attachment
from scripts.embed_mail import html_to_text
from scripts.safe_io import append_text, ensure_dir
from scripts.skolemelding_api import (
    ApiError,
    AuthExpiredError,
    SkolemeldingApi,
    SkolemeldingError,
)

PROJECT_ROOT = workspace_root()
STATE_PATH = PROJECT_ROOT / "memory" / "skolemelding-state.json"
OUT_DIR = PROJECT_ROOT / "memory" / "skolemelding"
ATTACH_DIR = OUT_DIR / "attachments"

PAGE_SIZE = 25
# Hard ceiling on inbox pages per run — a runaway loop against a buggy
# pager is worse than a missing backfill.
MAX_PAGES = 100
ATTACH_RETRY_BUDGET = 3
POLL_FLOOR_SECONDS = 300

_UNSAFE = re.compile(r"[^\w.()\[\]-]+")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename(name: str, index: int) -> str:
    """A filesystem-safe attachment name — server-supplied, so assume nothing.

    >>> safe_filename("Uke 42.pdf", 0)
    'Uke_42.pdf'
    >>> safe_filename("../../etc/passwd", 1)
    '1_passwd'
    >>> safe_filename("", 2)
    '2_attachment'
    """
    cleaned = _UNSAFE.sub("_", Path(name or "").name).strip("._") or "attachment"
    return cleaned if index == 0 else f"{index}_{cleaned}"


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], *, bump: bool = True) -> None:
    if bump:
        state["last_successful_run"] = utcnow_iso()
        state["auth_required"] = False
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


# --- Distillation -------------------------------------------------------------


def _body_text(html: Any) -> str:
    """The portal's ``Body`` is HTML; the sink stores plain text."""
    return html_to_text(str(html or "")).strip()


def attachment_name(portal_path: str) -> str:
    """Display name of an attachment: the basename of its portal path.

    >>> attachment_name("/globalassets/attachments/abc/Uke 42.pdf")
    'Uke 42.pdf'
    >>> attachment_name("")
    'attachment'
    """
    return Path(urlsplit(portal_path).path).name or "attachment"


def _thread_brief(thread: dict[str, Any]) -> dict[str, Any]:
    """Distilled reply — the thread item shape is only partially mapped, so
    pick from the candidate keys rather than trusting one."""
    return {
        "sender": str(thread.get("From") or thread.get("Sender")
                      or thread.get("Author") or ""),
        "timestamp": str(thread.get("Timestamp") or thread.get("Created") or ""),
        "body": _body_text(thread.get("Body") or thread.get("Text")),
        "attachments": [
            attachment_name(str(a)) for a in (thread.get("Attachments") or []) if a
        ],
    }


def message_signature(msg: dict[str, Any]) -> str:
    """sha256 over a stable JSON of the school-controlled content fields.

    Includes ``Header``, ``Body`` (as text), ``From``, ``Timestamp``,
    ``Attachments`` (portal paths) and ``Threads`` (distilled). Excludes
    everything that moves without the school acting: ``IsMessageSeen``,
    ``IsFavourite``, the feedback counters (``TotalYes``/``TotalNo``/
    ``TotalNotAnswered`` change as other parents answer), and the upstream
    typo field ``AuthorUserCategroy`` is not relied on anywhere.

    >>> a = {"Id": 1, "Header": "H", "Body": "<p>hei</p>", "From": "S",
    ...      "Timestamp": "2026-10-12T08:00:00+02:00", "Attachments": [],
    ...      "Threads": [], "IsMessageSeen": False, "TotalYes": 0}
    >>> b = dict(a, IsMessageSeen=True, IsFavourite=True, TotalYes=7,
    ...          TotalNo=2, TotalNotAnswered=1)
    >>> message_signature(a) == message_signature(b)
    True
    >>> message_signature(dict(a, Body="<p>hei!</p>")) == message_signature(a)
    False
    """
    payload = {
        "header": str(msg.get("Header") or ""),
        "sender": str(msg.get("From") or ""),
        "timestamp": str(msg.get("Timestamp") or ""),
        "body": _body_text(msg.get("Body")),
        "attachments": [str(a) for a in (msg.get("Attachments") or []) if a],
        "threads": [
            _thread_brief(t) for t in (msg.get("Threads") or [])
            if isinstance(t, dict)
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _message_record(
    msg: dict[str, Any], paths: dict[int, tuple[Path, str]], note: str | None = None
) -> dict[str, Any]:
    """One JSONL record's ``data`` — distilled, never the raw search row.

    ``paths`` is keyed by attachment index and holds (local file, mime) for
    the files that actually landed on disk; the portal path is always
    recorded under ``source`` so a missing file can be re-fetched later.
    """
    atts: list[dict[str, Any]] = []
    for j, raw in enumerate(msg.get("Attachments") or []):
        if not raw:
            continue
        entry: dict[str, Any] = {
            "name": attachment_name(str(raw)),
            "source": str(raw),
        }
        if j in paths:
            entry["path"] = str(paths[j][0])
            entry["mimeType"] = paths[j][1]
        atts.append(entry)
    data: dict[str, Any] = {
        "id": msg.get("Id"),
        "header": str(msg.get("Header") or ""),
        "sender": str(msg.get("From") or ""),
        "timestamp": str(msg.get("Timestamp") or ""),
        "body": _body_text(msg.get("Body")),
        "threads": [
            _thread_brief(t) for t in (msg.get("Threads") or [])
            if isinstance(t, dict)
        ],
        "attachments": atts,
    }
    # Feedback *type* flags are message properties (set once by the school),
    # unlike the answer counters — kept so triage can spot a question that
    # wants an answer. They stay out of the signature regardless.
    if msg.get("IsFeedBackQuestion"):
        data["feedback_question"] = True
    if msg.get("IsFeedBackReadConfirmation"):
        data["read_confirmation"] = True
    if note:
        data["note"] = note
    return data


# --- Fetch ------------------------------------------------------------------


def make_api() -> SkolemeldingApi:
    """Factory hook — tests swap in a stubbed transport here, not on the class.

    (Patching ``SkolemeldingApi`` wholesale would also swap out the attachment
    downloader, which is the other thing tests need to fake.)
    """
    return SkolemeldingApi()


def iter_inbox(api: SkolemeldingApi, known_ids: set[str]) -> Iterator[dict[str, Any]]:
    """Page the inbox newest-first, deduped by ``Id`` (the search answers
    some messages twice).

    Stops once every message already in state has been re-seen, or when
    ``TotalMatching`` is covered — always the case on the first run, which
    is what makes it a full backfill. Re-seeing known messages means an
    edit deep in the history still gets caught, unlike a "first unchanged
    page" stop. An absent or unparseable ``TotalMatching`` is *unknown*,
    not zero — paging then ends on an empty page / the known-ids condition
    instead of truncating after page 1.
    """
    seen_ids: set[str] = set()
    remaining = set(known_ids)
    check_remaining = bool(remaining)
    for page in range(1, MAX_PAGES + 1):
        data = api.search_messages(page=page, page_size=PAGE_SIZE)
        rows = [m for m in (data.get("Result") or []) if isinstance(m, dict)]
        try:
            total = int(data.get("TotalMatching") or 0) or None
        except (TypeError, ValueError):
            total = None
        for msg in rows:
            mid = msg.get("Id")
            if mid is None:
                continue
            key = str(mid)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            remaining.discard(key)
            yield msg
        if not rows or (total is not None and len(seen_ids) >= total):
            return
        if check_remaining and not remaining:
            return
    else:
        print(
            f"!! skolemelding: hit the {MAX_PAGES}-page safety cap — "
            "older messages were not scanned this run",
            file=sys.stderr,
        )


def _download_attachments(
    api: SkolemeldingApi, mid: str, portal_paths: list[Any]
) -> tuple[list[str], dict[int, tuple[Path, str]]]:
    """Download every attachment into ``ATTACH_DIR/<mid>/``.

    Returns ``(failed_names, {index: (local_path, mime)})`` — an empty
    failure list is what lets the message be recorded as fully seen. Files
    already on disk are kept as-is, so a retry after a partial failure does
    not re-download what already arrived.
    """
    failed: list[str] = []
    paths: dict[int, tuple[Path, str]] = {}
    used: set[str] = set()
    for j, raw in enumerate(portal_paths):
        if not raw:
            continue
        source = str(raw)
        name = attachment_name(source)
        target = ATTACH_DIR / mid / safe_filename(name, 0 if not used else j + 1)
        while target.name in used:  # two attachments, one name
            target = target.with_name(f"_{target.name}")
        used.add(target.name)
        try:
            existing = find_attachment(ATTACH_DIR, mid, target.name)
            if existing is not None:
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                paths[j] = (existing, mime)
                continue
            data, mime = api.attachment(source)
            target = write_attachment(ATTACH_DIR, mid, target.name, data)
        except AuthExpiredError:
            raise  # a dead login is main()'s job, not an attachment failure
        except Exception as e:  # noqa: BLE001 — one bad file must not kill the run
            print(f"!! skolemelding: attachment {name!r}: {e}", file=sys.stderr)
            failed.append(name)
            continue
        paths[j] = (target, mime)
    return failed, paths


def fetch_messages(
    api: SkolemeldingApi, state: dict[str, Any], out: list[dict[str, Any]]
) -> dict[str, int]:
    """New messages + messages whose signature moved, attachments pulled."""
    seen: dict[str, Any] = state.setdefault("messages", {})
    retries: dict[str, int] = state.setdefault("attachment_retries", {})
    fetched_at = utcnow_iso()
    counts = {"message": 0, "message_update": 0}

    for msg in iter_inbox(api, set(seen)):
        mid = msg.get("Id")
        if mid is None:
            continue
        key = str(mid)
        sig = message_signature(msg)
        known = seen.get(key)
        if known is not None and known.get("sig") == sig:
            continue
        kind = "message" if known is None else "message_update"

        failed, paths = _download_attachments(api, key, msg.get("Attachments") or [])
        if failed:
            attempts = int(retries.get(key, 0)) + 1
            retries[key] = attempts
            if attempts <= ATTACH_RETRY_BUDGET:
                # Signature deliberately left unrecorded: next cycle
                # re-emits and retries the file instead of losing it.
                print(
                    f"!! skolemelding: {len(failed)} attachment(s) failed for "
                    f"{key} (attempt {attempts}), retrying next run",
                    file=sys.stderr,
                )
                out.append({
                    "kind": kind,
                    "fetched_at": fetched_at,
                    "data": _message_record(
                        msg, paths,
                        note=f"attachment download failed "
                             f"(attempt {attempts}/{ATTACH_RETRY_BUDGET}): "
                             f"{', '.join(failed)}"),
                })
                counts[kind] += 1
                continue
            print(
                f"!! skolemelding: giving up on {len(failed)} attachment(s) for "
                f"{key} after {ATTACH_RETRY_BUDGET} attempts",
                file=sys.stderr,
            )
        retries.pop(key, None)
        seen[key] = {"sig": sig, "timestamp": msg.get("Timestamp")}
        out.append({
            "kind": kind,
            "fetched_at": fetched_at,
            "data": _message_record(msg, paths),
        })
        counts[kind] += 1
    return counts


# --- Run --------------------------------------------------------------------


def run_once(state: dict[str, Any]) -> tuple[int, list[dict[str, Any]], dict[str, int]]:
    api = make_api()
    records: list[dict[str, Any]] = []
    try:
        counts = fetch_messages(api, state, records)
    finally:
        api.close()
    return sum(counts.values()), records, counts


def today_jsonl() -> Path:
    ensure_dir(OUT_DIR)
    return OUT_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
    append_text(path, text)


def poll_floor_seconds() -> int:
    try:
        return max(0, int(cfg("skolemelding.poll_floor_seconds", POLL_FLOOR_SECONDS)))
    except (TypeError, ValueError):
        return POLL_FLOOR_SECONDS


def recently_ran(state: dict[str, Any]) -> bool:
    """True when a successful run happened inside the poll floor (be gentle)."""
    floor = poll_floor_seconds()
    last = state.get("last_successful_run")
    if not floor or not last:
        return False
    try:
        raw = str(last).replace("Z", "+00:00")
        then = datetime.fromisoformat(raw)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - then < timedelta(seconds=floor)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--once", action="store_true",
                    help="One-shot fetch (only mode; polling is systemd's job).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print records to stdout; write no JSONL, touch no state.")
    ap.add_argument("--force", action="store_true",
                    help=f"Ignore the {POLL_FLOOR_SECONDS}s poll floor between runs.")
    ap.add_argument("--event-spool", type=Path,
                    help="Also emit fresh in-memory records to this trusted event spool.")
    args = ap.parse_args(argv)

    if not args.once:
        ap.error("--once is required (no daemon mode)")

    state = load_state()
    if not args.force and not args.dry_run and recently_ran(state):
        print(f"skolemelding: ran less than {poll_floor_seconds()}s ago — nothing to do")
        return 0

    try:
        total, records, counts = run_once(state)
    except AuthExpiredError as e:
        # Normal end-of-life for a refresh token. Loud, but a success as far
        # as the sjekk is concerned. State is reloaded from disk first:
        # fetch_messages mutates the in-memory copy mid-run, and persisting
        # those seen-marks would skip records the aborted run never emitted.
        state = load_state()
        state["auth_required"] = True
        save_state(state, bump=False)
        print("!! skolemelding auth expired — run skolemelding-login", file=sys.stderr)
        print(f"   ({e})", file=sys.stderr)
        return 0
    except (ApiError, SkolemeldingError) as e:
        print(f"!! skolemelding sync failed: {e}", file=sys.stderr)
        return 1

    print(f"new records: total={total}, by_kind={counts}", file=sys.stderr)
    if args.dry_run:
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.event_spool:
        from scripts.event_export import emit_records
        emit_records(args.event_spool, "skolemelding", records)
    if total:
        append_jsonl(today_jsonl(), records)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
