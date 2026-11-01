#!/usr/bin/env python3
"""
Embed non-mail messages — Signal, Spond, Vigilo JSONL records — into the
Postgres + pgvector `mailvec` store alongside mail, reusing embed_mail's
schema (migration 016), chunking, batching and insertion helpers.

Sources:
    memory/signal/YYYY-MM-DD.jsonl       signal-capture records (immutable)
    /var/lib/diary/legacy-state/spond/YYYY-MM-DD.jsonl
    /var/lib/diary/legacy-state/vigilo/YYYY-MM-DD.jsonl
    /var/lib/diary/legacy-state/skolemelding/YYYY-MM-DD.jsonl

Each record becomes one `messages` row (tier 0, source tag, synthesized
message_id: `signal:{ts}:{hash12}`, `spond:{kind}:{id}`,
`vigilo:{kind}:{id}`, `skolemelding:message:{id}`) plus body chunks, exactly
like embed_mail — so search-mail finds everything with one query.

Idempotency / upsert, keyed on message_id + content_hash (sha1 of the
assembled text + extracted attachment text):
    no row          → insert
    same hash       → skip (the common case on the 15-min full rescan)
    hash differs    → DELETE (cascades chunks/attachments) + re-insert —
                      covers Spond RSVP/reschedule and Vigilo/Skolemelding
                      message_update

Signal attachment blobs live under ~/.local/share/signal-cli/attachments/<id>;
Vigilo and Skolemelding files live under their source directories in
/var/lib/diary/legacy-state/ (paths recorded by the syncs).
Extractable types (pdf/docx/odt/text) get their text chunked; images and
missing blobs stay metadata-only rows (text_chars=0), same as mail.

Env:
    PG_DSN                  postgres://user@host/mailvec   (default: dbname=mailvec)
    EMBED_MODEL / EMBED_DIMS / EMBED_BATCH / EMBED_BATCH_CHARS
                            shared with embed_mail (see its docstring)
    SIGNAL_CLI_ATTACHMENTS  signal-cli blob dir (default:
                            ~/.local/share/signal-cli/attachments)

Examples:
    uv run embed-messages                          # all sources, full history
    uv run embed-messages --source signal --limit 50
    uv run embed-messages --quiet                  # timer mode: silent on
                                                   # success, `!!` + exit 1 on
                                                   # failure (embed-mail contract)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import psycopg

from mail_reader.config import legacy_state_root, workspace_root

from scripts.embed_mail import (
    BATCH_CHUNKS,
    AttachmentPayload,
    MessagePayload,
    _embed_pending,
    _insert_payload_rows,
    chunk_text,
    extract_attachment_text,
    is_extractable_mime,
)

PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
SIGNAL_ATTACH_DIR = Path(os.environ.get(
    "SIGNAL_CLI_ATTACHMENTS",
    Path.home() / ".local" / "share" / "signal-cli" / "attachments"))

SOURCES = ("signal", "spond", "vigilo", "skolemelding")


class SourcePayload(MessagePayload):
    """embed_mail's payload plus the source tag and change-detection hash."""
    source: str
    content_hash: str


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _content_hash(text: str, attachments: list[AttachmentPayload]) -> str:
    """Stable hash over everything that ends up embedded for this record.

    A changed body OR a newly extracted attachment text flips the hash and
    triggers the delete + re-insert upsert path. Parts are length-prefixed
    into one digest, so the concatenation stays unambiguous even if a body
    or filename contains the separator byte.
    """
    parts = [text]
    for a in attachments:
        parts.append(a["filename"])
        parts.extend(a["chunks"])
    h = hashlib.sha1()
    for part in parts:
        b = part.encode("utf-8")
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return h.hexdigest()


def _payload(source: str, mid: str, *, date: str | None, from_addr: str,
             to_addrs: str, subject: str, tid: str | None, text: str,
             attachments: list[AttachmentPayload]) -> SourcePayload:
    """Assemble the payload + hash shared by all four source builders."""
    return {
        "mid": mid,
        "hdr": {"date": date, "from": from_addr,
                "to": to_addrs, "subject": subject},
        "tid": tid,
        "body_chars": len(text),
        "body_chunks": chunk_text(text),
        "attachments": attachments,
        "source": source,
        "content_hash": _content_hash(text, attachments),
    }


def _attachment(filename: str, mime: str, size: int,
                blob: Path | None, *, store: Path | None = None) -> AttachmentPayload:
    """One attachment row; extracted text chunks when the blob is on disk
    and the MIME type is extractable, metadata-only otherwise."""
    text = ""
    if blob is not None and is_extractable_mime(mime):
        try:
            if store is None:
                data = blob.read_bytes()
            else:
                from scripts.attachment_store import read_attachment_path
                data = read_attachment_path(store, blob)
            text = extract_attachment_text(mime, data)
        except (OSError, ValueError):
            text = ""
    return {
        "filename": filename or "(unnamed)",
        "mime": mime or "application/octet-stream",
        "size": size,
        "text_chars": len(text),
        "chunks": chunk_text(text) if text else [],
    }


# ---------- signal ----------

def _signal_attachment(att: dict[str, Any]) -> AttachmentPayload:
    from scripts.attachment_store import validate_attachment_id
    try:
        attachment_id = validate_attachment_id(att.get("id"))
    except ValueError:
        blob = None
    else:
        blob = Path(attachment_id)
    return _attachment(str(att.get("filename") or ""),
                       str(att.get("contentType") or ""),
                       int(att.get("size") or 0), blob, store=SIGNAL_ATTACH_DIR)


def signal_payload(rec: dict[str, Any]) -> SourcePayload | None:
    """One signal-capture record → payload, or None when nothing recordable.

    >>> rec = {"kind": "message", "direction": "in", "ts": 1780835700497,
    ...        "iso": "2026-06-07T12:35:00Z",
    ...        "from": {"name": "Alice", "number": "+4700000000", "uuid": "u1"},
    ...        "to": None, "group": None, "text": "hi", "quote": None,
    ...        "attachments": []}
    >>> p = signal_payload(rec)
    >>> p["mid"].startswith("signal:1780835700497:")
    True
    >>> p["hdr"]["from"], p["hdr"]["subject"]
    ('Alice', 'Alice')
    >>> p["tid"]
    'signal:peer:+4700000000'

    A reply quote folds in as a `> author: text` line:

    >>> rec["quote"] = {"author": "+4700000000", "text": "earlier"}
    >>> signal_payload(rec)["body_chunks"][0]
    'hi\\n> +4700000000: earlier'

    Empty records (no text, no attachments) are skipped:

    >>> signal_payload({"ts": 1, "text": "", "attachments": []}) is None
    True
    """
    text = (rec.get("text") or "").strip()
    quote = rec.get("quote") or {}
    if quote.get("text"):
        text = (f"{text}\n> {quote.get('author') or '?'}: "
                f"{quote['text']}").strip()
    sender = rec.get("from") or {}
    dest = rec.get("to") or {}
    group = rec.get("group") or {}
    attachments = [
        _signal_attachment(a) for a in (rec.get("attachments") or [])
    ]
    if not text and not attachments:
        return None
    peer = (dest.get("name") or dest.get("number")
            or sender.get("name") or sender.get("number") or sender.get("uuid"))
    label = str(group.get("name") or peer or "(unknown)")
    if group.get("id"):
        tid = f"signal:group:{group['id']}"
    else:
        other = dest if rec.get("direction") == "out" else sender
        key = other.get("number") or other.get("uuid") or other.get("name") or "unknown"
        tid = f"signal:peer:{key}"
    sender_key = sender.get("uuid") or sender.get("number") or sender.get("name") or ""
    mid = f"signal:{rec.get('ts') or 0}:{_sha1(f'{sender_key}|{text}')[:12]}"
    return _payload(
        "signal", mid,
        date=rec.get("iso") or rec.get("fetched_at"),
        from_addr=str(sender.get("name") or sender.get("number") or ""),
        to_addrs=str(group.get("name") or dest.get("name")
                     or dest.get("number") or ""),
        subject=label, tid=tid, text=text, attachments=attachments)


# ---------- spond ----------

# Same buckets spondshow.rsvp_status reads; ordered most→least common.
_RSVP_KEYS = ("acceptedIds", "declinedIds", "unansweredIds",
              "waitinglistIds", "unconfirmedIds")


def _spond_event_text(data: dict[str, Any]) -> str:
    """Heading + description + location + start + response counts.

    >>> _spond_event_text({"heading": "Training", "startTimestamp": "2026-05-30T08:30:00Z",
    ...                    "location": {"feature": "Clubhouse"},
    ...                    "description": "Bring boots",
    ...                    "responses": {"acceptedIds": ["A", "B"], "declinedIds": ["C"]}})
    'Training\\nBring boots\\nClubhouse\\n2026-05-30T08:30:00Z\\nResponses: accepted 2, declined 1'
    """
    parts = [str(data.get("heading") or "(no name)")]
    if data.get("cancelled"):
        parts.append("Cancelled")
    if data.get("description"):
        parts.append(str(data["description"]))
    loc = (data.get("location") or {}).get("feature")
    if loc:
        parts.append(str(loc))
    if data.get("startTimestamp"):
        parts.append(str(data["startTimestamp"]))
    responses = data.get("responses") or {}
    counts = [f"{k.removesuffix('Ids')} {len(responses[k])}"
              for k in _RSVP_KEYS if responses.get(k)]
    if counts:
        parts.append("Responses: " + ", ".join(counts))
    return "\n".join(parts)


def spond_payload(rec: dict[str, Any]) -> SourcePayload | None:
    """One spond-sync record (chat | event | post) → payload.

    >>> rec = {"kind": "chat", "fetched_at": "2026-05-26T11:30:00Z",
    ...        "data": {"id": "c1", "name": "Team chat",
    ...                 "newestTimestamp": "2026-05-26T11:00:00Z",
    ...                 "message": {"type": "TEXT", "text": "see you there"}}}
    >>> p = spond_payload(rec)
    >>> p["mid"], p["hdr"]["subject"], p["body_chunks"][0]
    ('spond:chat:c1', 'Team chat', 'Team chat\\nsee you there')

    Records without a stable id, and chats with no message text, are skipped:

    >>> spond_payload({"kind": "chat", "data": {"name": "x"}}) is None
    True
    >>> spond_payload({"kind": "chat", "data": {"id": "c2", "name": "x",
    ...                                         "message": {}}}) is None
    True
    """
    kind = rec.get("kind")
    data = rec.get("data") or {}
    rid = data.get("id") or data.get("uid") or data.get("chatId")
    if rid is None:
        return None
    fetched = rec.get("fetched_at") or ""
    if kind == "chat":
        name = str(data.get("name") or "(no name)")
        msg = data.get("message") or {}
        body = str(msg.get("text") or msg.get("newName") or "")
        if not body:
            return None
        text = f"{name}\n{body}"
        date = str(data.get("newestTimestamp") or fetched)
        from_addr = str(msg.get("sender") or msg.get("senderId") or "")
        subject = name
    elif kind == "event":
        text = _spond_event_text(data)
        date = str(data.get("startTimestamp") or fetched)
        from_addr = ""
        subject = str(data.get("heading") or "(no name)")
    elif kind == "post":
        title = str(data.get("title") or "")
        body = str(data.get("body") or data.get("text") or "")
        text = f"{title}\n{body}".strip()
        if not text:
            return None
        date = str(data.get("timestamp") or fetched)
        from_addr = ""
        subject = title
    else:
        return None
    return _payload("spond", f"spond:{kind}:{rid}",
                    date=date or None, from_addr=from_addr, to_addrs="",
                    subject=subject, tid=None, text=text, attachments=[])


# ---------- vigilo ----------

def _vigilo_attachment(att: dict[str, Any]) -> AttachmentPayload:
    """Message-kind attachment: the sync recorded the local file it landed
    in, so re-derive nothing — resolve like vigiloshown does."""
    from scripts import vigiloshown as vw
    return _attachment(str(att.get("name") or ""), str(att.get("mimeType") or ""),
                       0, vw.local_attachment_path(att), store=vw.ATTACH_DIR)


def vigilo_payload(rec: dict[str, Any]) -> SourcePayload | None:
    """One vigilo-sync record (message | message_update | news | consent)
    → payload.

    message and message_update collapse to one row (`vigilo:message:<uid>`):
    an update re-emits the whole thread, so it upserts over the original
    instead of duplicating it.

    ``check_out_note`` is deliberately **not** embedded: a standing hentemelding
    is not a message with a sender, it is a fact about a date, and the sjekk
    reads it from ``vigiloshown`` — so it falls through to ``None`` below.

    >>> rec = {"kind": "message", "fetched_at": "2026-10-01T08:00:00Z",
    ...        "data": {"threadUid": "t1", "unit": "Example AKS",
    ...                 "title": "Pickup times",
    ...                 "messages": [{"sender": "Alice", "timeCreated": "2026-10-01T07:00:00Z",
    ...                               "body": "New hours from Monday", "attachments": []}]}}
    >>> p = vigilo_payload(rec)
    >>> p["mid"], p["hdr"]["from"], p["hdr"]["to"]
    ('vigilo:message:t1', 'Alice', 'Example AKS')
    >>> p["body_chunks"][0]
    'Pickup times\\n--- Alice (2026-10-01T07:00:00Z) ---\\nNew hours from Monday'

    >>> vigilo_payload({"kind": "message_update", "fetched_at": rec["fetched_at"],
    ...                 "data": rec["data"]})["mid"] == p["mid"]
    True
    """
    kind = rec.get("kind")
    data = rec.get("data") or {}
    fetched = rec.get("fetched_at") or ""
    attachments: list[AttachmentPayload] = []
    if kind in ("message", "message_update"):
        uid = data.get("threadUid")
        if not uid:
            return None
        mid = f"vigilo:message:{uid}"
        parts = [str(data.get("title") or "")]
        messages = [m for m in (data.get("messages") or []) if isinstance(m, dict)]
        for m in messages:
            parts.append(f"--- {m.get('sender') or '?'} "
                         f"({m.get('timeCreated') or ''}) ---")
            parts.append(str(m.get("body") or ""))
            attachments.extend(
                _vigilo_attachment(a) for a in (m.get("attachments") or [])
                if isinstance(a, dict))
        text = "\n".join(p for p in parts if p)
        if not text and not attachments:
            return None
        times = [str(m.get("timeCreated") or "") for m in messages]
        date = max(times) if any(times) else fetched
        from_addr = str(messages[-1].get("sender") or "") if messages else ""
        subject = str(data.get("title") or "")
    elif kind == "news":
        pid = data.get("postId")
        if not pid:
            return None
        mid = f"vigilo:news:{pid}"
        title = str(data.get("title") or "")
        text = f"{title}\n{data.get('body') or ''}".strip()
        # News attachments download over the Bearer endpoint at show time —
        # no local file here, so metadata-only rows.
        attachments = [
            _attachment(str(a.get("name") or ""), str(a.get("mimeType") or ""), 0, None)
            for a in (data.get("attachments") or []) if isinstance(a, dict)
        ]
        if not text and not attachments:
            return None
        date = str(data.get("created") or fetched)
        from_addr = ""
        subject = title
    elif kind == "consent":
        cid = data.get("consentId")
        if not cid:
            return None
        mid = f"vigilo:consent:{cid}"
        title = str(data.get("title") or "")
        parts = [title, str(data.get("body") or "")]
        status = data.get("status")
        deadline = data.get("deadline")
        if status or deadline:
            parts.append(f"Status: {status or '?'}, deadline: {deadline or '?'}")
        text = "\n".join(p for p in parts if p)
        if not text:
            return None
        date = fetched
        from_addr = ""
        subject = title
    else:
        return None
    return _payload("vigilo", mid, date=date or None, from_addr=from_addr,
                    to_addrs=str(data.get("unit") or ""), subject=subject,
                    tid=None, text=text, attachments=attachments)


# ---------- skolemelding ----------

def _skolemelding_attachment(att: dict[str, Any]) -> AttachmentPayload:
    """The sync recorded the local file it landed in, so re-derive nothing —
    resolve like skolemeldingshow does."""
    from scripts import skolemeldingshow as sw
    return _attachment(str(att.get("name") or ""), str(att.get("mimeType") or ""),
                       0, sw.local_attachment_path(att), store=sw.ATTACH_DIR)


def skolemelding_payload(rec: dict[str, Any]) -> SourcePayload | None:
    """One skolemelding-sync record (message | message_update) → payload.

    message and message_update collapse to one row
    (`skolemelding:message:<Id>`): an update re-emits the whole message, so
    it upserts over the original instead of duplicating it.

    >>> rec = {"kind": "message", "fetched_at": "2026-10-13T08:00:00Z",
    ...        "data": {"id": 123, "header": "Ukeplan uke 42",
    ...                 "sender": "Eksempel skole",
    ...                 "timestamp": "2026-10-12T10:00:00+02:00",
    ...                 "body": "Se vedlegg", "threads": [
    ...                     {"sender": "Forelder", "timestamp": "2026-10-13T07:00:00+02:00",
    ...                      "body": "Takk!", "attachments": []}],
    ...                 "attachments": []}}
    >>> p = skolemelding_payload(rec)
    >>> p["mid"], p["hdr"]["from"], p["hdr"]["subject"]
    ('skolemelding:message:123', 'Eksempel skole', 'Ukeplan uke 42')
    >>> p["body_chunks"][0]
    'Ukeplan uke 42\\nSe vedlegg\\n--- Forelder (2026-10-13T07:00:00+02:00) ---\\nTakk!'

    >>> skolemelding_payload({"kind": "message_update", "fetched_at": rec["fetched_at"],
    ...                       "data": rec["data"]})["mid"] == p["mid"]
    True
    >>> skolemelding_payload({"kind": "message", "data": {}}) is None
    True
    """
    if rec.get("kind") not in ("message", "message_update"):
        return None
    data = rec.get("data") or {}
    mid_raw = data.get("id")
    if mid_raw is None:
        return None
    fetched = rec.get("fetched_at") or ""
    parts = [str(data.get("header") or "")]
    if data.get("body"):
        parts.append(str(data["body"]))
    for thread in data.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        parts.append(f"--- {thread.get('sender') or '?'} "
                     f"({thread.get('timestamp') or ''}) ---")
        if thread.get("body"):
            parts.append(str(thread["body"]))
    attachments = [
        _skolemelding_attachment(a) for a in (data.get("attachments") or [])
        if isinstance(a, dict)
    ]
    text = "\n".join(p for p in parts if p)
    if not text and not attachments:
        return None
    return _payload("skolemelding", f"skolemelding:message:{mid_raw}",
                    date=str(data.get("timestamp") or fetched) or None,
                    from_addr=str(data.get("sender") or ""), to_addrs="",
                    subject=str(data.get("header") or ""), tid=None,
                    text=text, attachments=attachments)


BUILDERS = {"signal": signal_payload, "spond": spond_payload,
            "vigilo": vigilo_payload, "skolemelding": skolemelding_payload}


# ---------- upsert + batch flush ----------

def decide(existing_hash: str | None, new_hash: str) -> Literal["insert", "skip", "replace"]:
    """Upsert decision for one record.

    >>> decide(None, "abc")
    'insert'
    >>> decide("abc", "abc")
    'skip'
    >>> decide("old", "abc")
    'replace'
    """
    if existing_hash is None:
        return "insert"
    return "skip" if existing_hash == new_hash else "replace"


def _flush_batch(conn: psycopg.Connection, batch: list[SourcePayload],
                 verbose: bool) -> tuple[int, int]:
    """embed_mail's flow: one batched embed call for the whole batch, then
    one transaction per message."""
    per_msg = _embed_pending(batch)
    done = failed = 0
    for mi, payload in enumerate(batch):
        slots = per_msg.get(mi, {})
        try:
            with conn.cursor() as cur, conn.transaction():
                cur.execute("""
                    INSERT INTO messages
                      (message_id, date, from_addr, to_addrs, subject,
                       thread_id, tier, body_chars, source, content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s)
                    RETURNING id
                """, (payload["mid"], payload["hdr"]["date"],
                      payload["hdr"]["from"], payload["hdr"]["to"],
                      payload["hdr"]["subject"], payload["tid"],
                      payload["body_chars"], payload["source"],
                      payload["content_hash"]))
                row = cur.fetchone()
                assert row is not None, "RETURNING id must yield a row"
                _insert_payload_rows(cur, row[0], payload, slots)
            done += 1
        except Exception as e:
            failed += 1
            print(f"  !! {payload['mid']} (write): {e}", file=sys.stderr)
    return done, failed


def iter_records(source: str, verbose: bool) -> Iterator[dict[str, Any]]:
    """Stream one source's JSONL history, oldest file first. A missing
    directory is a note, not an error (e.g. vigilo before the first sync)."""
    jsonl_dir = (workspace_root() / "memory" / source if source == "signal"
                 else legacy_state_root() / source)
    if not jsonl_dir.exists():
        if verbose:
            print(f"[{source}] {jsonl_dir} does not exist — skipping",
                  file=sys.stderr)
        return
    for fp in sorted(jsonl_dir.glob("*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def process(conn: psycopg.Connection, source: str, limit: int | None,
            verbose: bool) -> tuple[int, int, int]:
    """Scan one source's full JSONL history; upsert what changed.
    Returns (done, skipped, failed)."""
    builder = BUILDERS[source]
    done = skipped = failed = replaced = 0
    t0 = time.time()

    def chunk_count(p: SourcePayload) -> int:
        return (len(p["body_chunks"])
                + sum(len(a["chunks"]) for a in p["attachments"]))

    def delete_row(mid: str) -> None:
        # DELETE cascades chunks/attachments; the re-insert rebuilds them.
        # If the run dies in between, the next one sees no row and inserts
        # fresh — resumable either way.
        with conn.cursor() as cur, conn.transaction():
            cur.execute("DELETE FROM messages WHERE message_id = %s", (mid,))

    # Pass 1: build payloads, grouped by mid. One message_id can appear
    # several times in the JSONL history (spond re-emits on RSVP change,
    # vigilo message_update re-emits the whole thread), and files iterate
    # oldest-first — so the last sighting is the newest version, and the
    # only one worth deciding on. Comparing a stale sighting's hash against
    # the stored row would force a delete + re-insert on EVERY run.
    latest: dict[str, SourcePayload] = {}
    for n, rec in enumerate(iter_records(source, verbose), 1):
        if limit is not None and n > limit:
            break
        try:
            payload = builder(rec)
        except Exception as e:
            failed += 1
            print(f"  !! {source} record (prepare): {e}", file=sys.stderr)
            continue
        if payload is None:
            skipped += 1
            continue
        latest[payload["mid"]] = payload   # newest sighting wins

    # Pass 2: one decide per mid, against the newest version's hash.
    pending: list[SourcePayload] = []
    pending_chunks = 0

    def flush() -> None:
        nonlocal done, failed, pending, pending_chunks
        if not pending:
            return
        try:
            d, f = _flush_batch(conn, pending, verbose)
            done += d
            failed += f
        except Exception as e:
            failed += len(pending)
            print(f"  !! batch embed failed ({len(pending)} records): {e}",
                  file=sys.stderr)
        pending = []
        pending_chunks = 0

    for payload in latest.values():
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM messages WHERE message_id = %s",
                        (payload["mid"],))
            row = cur.fetchone()
        action = decide(row[0] if row else None, payload["content_hash"])
        if action == "skip":
            skipped += 1
            continue
        if action == "replace":
            delete_row(payload["mid"])
            replaced += 1
        pending.append(payload)
        pending_chunks += chunk_count(payload)
        if pending_chunks >= BATCH_CHUNKS:
            flush()

    flush()
    if verbose or failed:
        print(f"[{source}] done={done} (replaced={replaced}) "
              f"skipped={skipped} failed={failed} "
              f"elapsed={(time.time() - t0):.0f}s", file=sys.stderr)
    return done, skipped, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", choices=list(SOURCES),
                    default=None,
                    help="Restrict to a source (repeatable; default: all).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N records per source (trial runs).")
    ap.add_argument("--quiet", action="store_true",
                    help="Silent on success; `!!` stderr lines + nonzero exit "
                         "on failure (same contract as embed-mail).")
    args = ap.parse_args(argv)

    failed_total = 0
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for source in (args.source or list(SOURCES)):
            _, _, f = process(conn, source, args.limit,
                              verbose=not args.quiet)
            failed_total += f
    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
