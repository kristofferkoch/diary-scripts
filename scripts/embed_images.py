#!/usr/bin/env python3
"""
Describe image attachments (mail + Signal + Vigilo) with a vision-language
model and embed the descriptions into the Postgres + pgvector `mailvec`
store — so image content (receipts by their printed text, photos by what's
in them) becomes searchable via `search-mail`, through the exact same
retrieval path as extracted attachment text.

Pending = an `attachments` row with an image MIME type, `text_chars = 0`,
`vlm_model IS NULL`, `vlm_attempts < 3` (schema: `017_vlm.sql`), newest
message first. The pass is idempotent and resumable by construction: a
described row gets `vlm_model = <model id>` and leaves the pending set.

Per image: locate the blob (mail via notmuch, Signal via the signal-cli
blob dir, Vigilo via the path vigilo-sync recorded), downscale with Pillow
(long side ≤ 1568 px, GIF → first frame, JPEG q85, base64 data URL),
describe with the OpenAI-compatible chat endpoint (Norwegian "describe +
transcribe verbatim" prompt, thinking left on), then store the description
prefixed `Vedlegg <filename> (bilde): ` as ordinary chunks with
`attachment_id` set — batched through embed_mail's embed_batch caps, one
transaction per attachment.

Images whose longest side is under 128 px are icons / tracking pixels:
marked `vlm_model = 'skipped:tiny'` without spending an attempt. Vigilo
news attachments never have a local blob (they download over the API at
show time) and are marked terminal `'skipped:no-blob'` right away. Any
other failure (blob gone, VLM error, write error) increments
`vlm_attempts`; at 3 the row is marked `vlm_model = 'error'` and leaves
the hot loop.

Env:
    PG_DSN              postgres://user@host/mailvec      (default: dbname=mailvec)
    VLM_URL             http://gpu-host:8080              (default; config hosts.vlm)
    VLM_MODEL           qwen3.8-flash-next                (default; config vlm.model)
    EMBED_URL / EMBED_MODEL / EMBED_DIMS / EMBED_BATCH / EMBED_BATCH_CHARS
                        shared with embed_mail (see its docstring)

Examples:
    uv run embed-images --limit 5              # trial: 5 newest pending images
    uv run embed-images --source signal        # one source only
    uv run embed-images --quiet --limit 12     # timer mode: silent on success,
                                               # `!!` + exit 1 on failure
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

import psycopg
from PIL import Image

from mail_reader.config import cfg, vlm_url

from scripts.embed_mail import (
    BATCH_CHUNKS,
    chunk_text,
    embed_batch,
    iter_attachments,
    nm_raw,
    vec_literal,
)
from scripts.embed_messages import BUILDERS, SIGNAL_ATTACH_DIR, iter_records

PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
VLM = vlm_url()  # $VLM_URL → config hosts.vlm (see config.vlm_url)
VLM_MODEL = os.environ.get("VLM_MODEL") or cfg("vlm.model", "qwen3.8-flash-next")

SOURCES = ("mail", "signal", "vigilo")
MAX_ATTEMPTS = 3
# Size floor on the longest side: below this an image is an icon, logo or
# tracking pixel — not worth a VLM call (marked 'skipped:tiny').
MIN_PIXELS = 128
# Downscale cap on the long side before sending (qwen-vl's sweet spot).
MAX_PIXELS = 1568

# MIME types worth sending to the VLM. Matches the `attachments.mime_type`
# values produced by embed_mail / embed_messages for these formats; keep in
# sync with is_image_mime.
IMAGE_MIMES = ("image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp")


def is_image_mime(mime: str) -> bool:
    """MIME types the VLM pass handles (Pillow can open them, the model can
    see them). TIFF/BMP/SVG stay out — barely seen in the store, and SVG
    isn't raster at all.

    >>> is_image_mime("image/jpeg")
    True
    >>> is_image_mime("IMAGE/PNG")
    True
    >>> is_image_mime("image/tiff")
    False
    >>> is_image_mime("application/pdf")
    False
    >>> is_image_mime("")
    False
    """
    return (mime or "").lower() in IMAGE_MIMES


class PendingRow(TypedDict):
    att_id: int            # attachments.id
    message_row_id: int    # attachments.message_id → messages.id
    filename: str
    mime: str
    size: int
    vlm_attempts: int
    mid: str               # messages.message_id (notmuch id / synthesized)
    source: str            # messages.source: mail | signal | vigilo
    from_addr: str
    subject: str
    date: str | None


class Prepared(TypedDict):
    """One described attachment queued for the batched embed + store."""
    row: PendingRow
    description: str       # raw VLM output (text_chars basis)
    chunks: list[str]      # prefixed + chunked, ready to embed


# ---------- pending selection ----------

def select_pending(conn: psycopg.Connection, sources: tuple[str, ...],
                   limit: int) -> list[PendingRow]:
    """Newest-first pending image attachments, joined with their message for
    the prompt context line (from / subject / date)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.message_id, a.filename, a.mime_type, a.size_bytes,
                   a.vlm_attempts,
                   m.message_id, m.source, m.from_addr, m.subject, m.date
            FROM attachments a
            JOIN messages m ON m.id = a.message_id
            WHERE a.mime_type = ANY(%s)
              AND a.text_chars = 0
              AND a.vlm_model IS NULL
              AND a.vlm_attempts < %s
              AND m.source = ANY(%s)
            ORDER BY m.date DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (list(IMAGE_MIMES), MAX_ATTEMPTS, list(sources), limit),
        )
        rows = cur.fetchall()
    return [
        {
            "att_id": int(r[0]),
            "message_row_id": int(r[1]),
            "filename": str(r[2] or "(unnamed)"),
            "mime": str(r[3]),
            "size": int(r[4] or 0),
            "vlm_attempts": int(r[5] or 0),
            "mid": str(r[6]),
            "source": str(r[7]),
            "from_addr": str(r[8] or ""),
            "subject": str(r[9] or ""),
            "date": str(r[10]) if r[10] is not None else None,
        }
        for r in rows
    ]


# ---------- blob resolution ----------

def _occurrence_index(conn: psycopg.Connection, row: PendingRow) -> int:
    """Which same-key duplicate this row is within its message.

    embed_mail inserts attachments in `iter_attachments` walk order, so the
    n-th DB row with a given (filename, mime, size) key — ordered by id —
    is the n-th matching MIME part in the raw message."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM attachments
            WHERE message_id = %s
              AND filename IS NOT DISTINCT FROM %s
              AND mime_type = %s
              AND size_bytes IS NOT DISTINCT FROM %s
              AND id < %s
            """,
            (row["message_row_id"], row["filename"], row["mime"],
             row["size"], row["att_id"]),
        )
        n = cur.fetchone()
    return int(n[0]) if n else 0


def mail_blob(raw: bytes, filename: str, mime: str, size: int,
              occurrence: int = 0) -> bytes | None:
    """The occurrence-th MIME part in `raw` matching (filename, mime, size),
    or None when the message no longer carries it."""
    n = 0
    for fn, part_mime, data, _text in iter_attachments(raw):
        if fn == filename and part_mime == mime and len(data) == size:
            if n == occurrence:
                return data
            n += 1
    return None


def _record_index(source: str) -> dict[str, dict[str, Any]]:
    """mid → newest JSONL record for a non-mail source, recomputing mids
    with embed_messages' builder (records iterate oldest-first, so the last
    sighting wins — same rule as embed-messages' own upsert). Built once
    per run, not per row."""
    index: dict[str, dict[str, Any]] = {}
    for rec in iter_records(source, verbose=False):
        try:
            payload = BUILDERS[source](rec)
        except Exception:
            continue
        if payload is not None:
            index[payload["mid"]] = rec
    return index


def signal_blob(rec: dict[str, Any], filename: str, size: int) -> bytes | None:
    """The signal-cli blob for a record's attachment matching
    (filename, size), or None when it isn't safely readable."""
    from scripts.attachment_store import read_attachment

    for a in rec.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("filename") or "(unnamed)") != filename:
            continue
        if int(a.get("size") or 0) != size:
            continue
        try:
            return read_attachment(SIGNAL_ATTACH_DIR, a.get("id"))
        except (OSError, ValueError):
            return None
    return None


def vigilo_blob(rec: dict[str, Any], filename: str, mime: str) -> bytes | None:
    """The local file vigilo-sync recorded for a matching attachment
    (message attachments live under `data.messages[*].attachments`, news
    under `data.attachments` — the latter have no local copy and resolve
    to None)."""
    from scripts import vigiloshown as vw
    from scripts.attachment_store import read_attachment_path

    data = rec.get("data") or {}
    atts: list[Any] = []
    for m in data.get("messages") or []:
        if isinstance(m, dict):
            atts.extend(a for a in (m.get("attachments") or [])
                        if isinstance(a, dict))
    atts.extend(a for a in (data.get("attachments") or [])
                if isinstance(a, dict))
    for a in atts:
        if str(a.get("name") or "(unnamed)") != filename:
            continue
        if str(a.get("mimeType") or "application/octet-stream") != mime:
            continue
        raw = a.get("path")
        if not raw:
            return None
        try:
            return read_attachment_path(vw.ATTACH_DIR, str(raw))
        except (OSError, ValueError):
            return None
    return None


class NoLocalBlob(Exception):
    """The blob can never be on disk for this row — the caller marks it
    terminal ('skipped:no-blob') instead of burning attempts to 'error'."""


def resolve_blob(conn: psycopg.Connection, row: PendingRow,
                 indexes: dict[str, dict[str, dict[str, Any]]]) -> bytes | None:
    """Raw bytes of the attachment, from wherever its source keeps blobs.
    None when it can't be found right now (counts as a failed attempt
    upstream); NoLocalBlob when it never will be."""
    if row["source"] == "mail":
        raw = nm_raw(row["mid"])
        return mail_blob(raw, row["filename"], row["mime"], row["size"],
                         _occurrence_index(conn, row))
    rec = indexes.get(row["source"], {}).get(row["mid"])
    if rec is None:
        return None
    if row["source"] == "signal":
        return signal_blob(rec, row["filename"], row["size"])
    # Vigilo news attachments are metadata-only by design — embed_messages
    # stores no blob and they download over the API at show time, so a
    # local copy will never appear.
    if rec.get("kind") == "news":
        raise NoLocalBlob(
            f"vigilo news attachment {row['filename']!r} has no local copy")
    return vigilo_blob(rec, row["filename"], row["mime"])


# ---------- image preprocessing ----------

def scale_dims(w: int, h: int, cap: int = MAX_PIXELS) -> tuple[int, int]:
    """Fit (w, h) inside `cap` on the long side, preserving aspect.

    >>> scale_dims(3136, 1568)
    (1568, 784)
    >>> scale_dims(800, 600)      # already inside — untouched
    (800, 600)
    >>> scale_dims(100, 4000)
    (39, 1568)
    """
    if max(w, h) <= cap:
        return (w, h)
    scale = cap / max(w, h)
    return (max(1, round(w * scale)), max(1, round(h * scale)))


def prepare_image(data: bytes) -> str | None:
    """→ a base64 JPEG data URL ready for the VLM, or None when the image is
    below the size floor (icons / tracking pixels — caller marks
    'skipped:tiny'). GIFs contribute their first frame; alpha/palette modes
    flatten to RGB."""
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if max(w, h) < MIN_PIXELS:
            return None
        if max(w, h) > MAX_PIXELS:
            im = im.resize(scale_dims(w, h), Image.Resampling.LANCZOS)
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ---------- VLM call ----------

PROMPT_TEMPLATE = (
    "Beskriv dette bildet på norsk, kort og presist (1–3 setninger) — hva "
    "viser bildet? Transkriber deretter all lesbar tekst i bildet ordrett, "
    "linje for linje. Hvis bildet ikke inneholder tekst, skriver du bare "
    "beskrivelsen.\n"
    "Kontekst: bildet er vedlegget «{filename}» i en melding fra "
    "{from_addr}, emne «{subject}», dato {date}."
)


def build_prompt(filename: str, from_addr: str, subject: str,
                 date: str | None) -> str:
    """The describe + transcribe prompt with one message-context line."""
    return PROMPT_TEMPLATE.format(
        filename=filename, from_addr=from_addr or "?",
        subject=subject or "(uten emne)", date=date or "ukjent")


def _post_chat(payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{VLM}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        out: dict[str, Any] = json.loads(r.read())
        return out


def _response_content(response: dict[str, Any]) -> str:
    """The answer text. `reasoning_content` (thinking) is deliberately
    ignored — the description lives in `content`."""
    return str(response["choices"][0]["message"]["content"]).strip()


def describe_image(data_url: str, prompt: str) -> str:
    """One chat-completions round trip. Thinking is left at the server
    default (on): it noticeably improves verbatim transcription of small
    text (receipt amounts), so no enable_thinking flag is sent."""
    payload = {
        "model": VLM_MODEL,
        # Generous cap: reasoning tokens count toward it, and a long thinking
        # chain can otherwise consume the whole budget and return empty
        # content (seen live at 1024, 2026-10-11).
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }
    text = _response_content(_post_chat(payload))
    if not text:
        raise RuntimeError("vlm: empty description")
    return text


# ---------- failure accounting ----------

def mark_error(attempts: int) -> tuple[int, str | None]:
    """Next (vlm_attempts, vlm_model) after a failed attempt.

    >>> mark_error(0)
    (1, None)
    >>> mark_error(1)
    (2, None)
    >>> mark_error(2)          # third strike leaves the pending set
    (3, 'error')
    """
    attempts += 1
    return attempts, "error" if attempts >= MAX_ATTEMPTS else None


def _mark_terminal(conn: psycopg.Connection, att_id: int, mark: str) -> None:
    """Stamp a terminal vlm_model ('skipped:tiny' / 'skipped:no-blob').

    The `AND vlm_model IS NULL` guard keeps a racing failure path (manual
    run vs timer) from re-pending a row the winner already described —
    an unguarded stamp would null the model, re-select the row, and the
    duplicate chunk INSERT would then burn 3 strikes to a terminal 'error'
    on a successfully described attachment."""
    with conn.cursor() as cur, conn.transaction():
        cur.execute(
            "UPDATE attachments SET vlm_model = %s WHERE id = %s "
            "AND vlm_model IS NULL",
            (mark, att_id))


def _mark_failure(conn: psycopg.Connection, row: PendingRow) -> None:
    attempts, model = mark_error(row["vlm_attempts"])
    with conn.cursor() as cur, conn.transaction():
        # Same race guard as _mark_terminal: never touch a row that already
        # carries a stamp (success or terminal-skip).
        cur.execute(
            "UPDATE attachments SET vlm_attempts = %s, vlm_model = %s "
            "WHERE id = %s AND vlm_model IS NULL",
            (attempts, model, row["att_id"]))
    row["vlm_attempts"] = attempts


# ---------- batched embed + store ----------

def _store(conn: psycopg.Connection, batch: list[Prepared],
           vectors: list[list[float]], verbose: bool) -> tuple[int, int]:
    """Write each prepared attachment in its own transaction: chunk rows
    with attachment_id set, then the attachment row itself (text_chars +
    vlm_model stamp). Mirrors embed_mail's _flush_batch shape."""
    done = failed = 0
    vi = 0
    for prep in batch:
        row = prep["row"]
        n_chunks = len(prep["chunks"])
        vecs = vectors[vi:vi + n_chunks]
        vi += n_chunks
        try:
            with conn.cursor() as cur, conn.transaction():
                for ci, c in enumerate(prep["chunks"]):
                    cur.execute(
                        "INSERT INTO chunks "
                        "(message_id, attachment_id, chunk_idx, text, embedding) "
                        "VALUES (%s, %s, %s, %s, %s::vector)",
                        (row["message_row_id"], row["att_id"], ci, c,
                         vec_literal(vecs[ci])))
                cur.execute(
                    "UPDATE attachments SET text_chars = %s, vlm_model = %s "
                    "WHERE id = %s",
                    (len(prep["description"]), VLM_MODEL, row["att_id"]))
            done += 1
        except Exception as e:
            failed += 1
            print(f"  !! {row['mid']} {row['filename']} (write): {e}",
                  file=sys.stderr)
            try:
                _mark_failure(conn, row)
            except Exception:
                pass
    return done, failed


def flush(conn: psycopg.Connection, batch: list[Prepared],
          verbose: bool) -> tuple[int, int]:
    """One batched embed call for every queued description chunk (embed_mail's
    BATCH caps apply inside embed_batch), then one tx per attachment."""
    flat = [c for prep in batch for c in prep["chunks"]]
    vectors = embed_batch(flat)
    return _store(conn, batch, vectors, verbose)


# ---------- main loop ----------

def process(conn: psycopg.Connection, sources: tuple[str, ...], limit: int,
            verbose: bool) -> tuple[int, int, int]:
    """Walk pending image rows once. Returns (done, skipped, failed)."""
    rows = select_pending(conn, sources, limit)
    if verbose:
        print(f"[embed-images] {len(rows)} pending image attachments "
              f"(sources={','.join(sources)}, model={VLM_MODEL})",
              file=sys.stderr)

    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    pending: list[Prepared] = []
    pending_chunks = 0
    done = skipped = failed = 0
    t0 = time.time()

    def do_flush() -> None:
        nonlocal done, failed, pending, pending_chunks
        if not pending:
            return
        try:
            d, f = flush(conn, pending, verbose)
            done += d
            failed += f
        except Exception as e:
            # Embed endpoint down etc: count attempts so a persistent
            # outage doesn't re-describe the same images every run.
            failed += len(pending)
            print(f"  !! batch embed failed ({len(pending)} attachments): {e}",
                  file=sys.stderr)
            for prep in pending:
                try:
                    _mark_failure(conn, prep["row"])
                except Exception:
                    pass
        pending = []
        pending_chunks = 0

    for n, row in enumerate(rows, 1):
        try:
            src = row["source"]
            if src in ("signal", "vigilo") and src not in indexes:
                indexes[src] = _record_index(src)
            blob = resolve_blob(conn, row, indexes)
            if blob is None:
                raise FileNotFoundError(f"no blob for {row['filename']!r}")
            data_url = prepare_image(blob)
            if data_url is None:
                _mark_terminal(conn, row["att_id"], "skipped:tiny")
                skipped += 1
                if verbose:
                    print(f"  {n}/{len(rows)} {src} {row['filename']} "
                          f"— skipped:tiny", file=sys.stderr)
                continue
            prompt = build_prompt(row["filename"], row["from_addr"],
                                  row["subject"], row["date"])
            description = describe_image(data_url, prompt)
        except NoLocalBlob as e:
            skipped += 1
            _mark_terminal(conn, row["att_id"], "skipped:no-blob")
            if verbose:
                print(f"  {n}/{len(rows)} {row['source']} {row['filename']} "
                      f"— skipped:no-blob ({e})", file=sys.stderr)
            continue
        except Exception as e:
            failed += 1
            print(f"  !! {row['mid']} {row['filename']}: {e}", file=sys.stderr)
            try:
                _mark_failure(conn, row)
            except Exception:
                pass
            continue
        text = f"Vedlegg {row['filename']} (bilde): {description}"
        pending.append({"row": row, "description": description,
                        "chunks": chunk_text(text)})
        pending_chunks += len(pending[-1]["chunks"])
        if verbose:
            print(f"  {n}/{len(rows)} {src} {row['filename']} "
                  f"({len(description)} chars)", file=sys.stderr)
        if pending_chunks >= BATCH_CHUNKS:
            do_flush()

    do_flush()
    if verbose or failed:
        print(f"[embed-images] done={done} skipped={skipped} failed={failed} "
              f"elapsed={(time.time() - t0):.0f}s", file=sys.stderr)
    return done, skipped, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", choices=list(SOURCES),
                    default=None,
                    help="Restrict to a source (repeatable; default: all).")
    ap.add_argument("--limit", type=int, default=25,
                    help="Max pending attachments per run (default: 25).")
    ap.add_argument("--quiet", action="store_true",
                    help="Silent on success; `!!` stderr lines + nonzero exit "
                         "on failure (same contract as embed-mail).")
    args = ap.parse_args(argv)

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        _, _, failed = process(conn, tuple(args.source or SOURCES),
                               args.limit, verbose=not args.quiet)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
