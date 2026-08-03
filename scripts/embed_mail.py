#!/usr/bin/env python3
"""
Embed mail from notmuch into Postgres + pgvector via an OpenAI-compatible
embedding server (llama.cpp `llama-server`, Qwen3-Embedding).

Tiers (set with --tier or run them all sequentially):
    1  date:1y..                                          (~6.3k msgs)
    2  thread:"{from:<me>}"                               (~0.7k msgs)
    3  from:<addr1> or from:<addr2> or ...                (~9k msgs)
       (addresses harvested from your past recipients)
    4  tag:digest::keep                                   (~48k msgs)
    5  attachment                                         (~7k msgs, mostly
       overlapping tiers 1-3; idempotency dedups automatically)
    6  from:*@<NO institution>                            (~1k msgs)
       (skatteetaten, oslo.kommune, altinn, digipost, nav, posten, vy, ...)

Per message: notmuch raw → email.parse → main text body (plain/html)
+ extract text from PDF/DOCX/ODT/ICS/text attachments
→ chunk (≈512 tok, ≈64 overlap, ≈2048 chars) → accumulate across messages
→ batched POST /v1/embeddings (≥32 chunks per call) → write rows in one tx per msg.

Image / unsupported attachments still get an `attachments` row with text_chars=0,
so a later VLM pass (`scripts/embed_images.py`, todo) can find them.

Re-runs are idempotent: messages with an existing message_id are skipped.
`--reembed` instead walks `messages` rows that have no chunks/attachments
yet (e.g. after `TRUNCATE chunks, attachments` for a model switch) and
rebuilds only those tables — resumable, just re-run if interrupted.

Env:
    PG_DSN              postgres://user@host/mailvec      (default: dbname=mailvec)
    EMBED_URL           http://gpu-host:8081              (default; config hosts.embed)
    EMBED_MODEL         qwen3-embedding                   (default; server alias)
    EMBED_DIMS          1024                              (stored dim; wider native
                        vectors are truncated MRL-style + L2-renormalized)
    EMBED_BATCH         32                                (default; max chunks/api call)
    EMBED_BATCH_CHARS   6000                              (default; max total chars/api
                        call — oversized calls corrupt llama-server, 2026-08-03)
    EMBED_QUERY_INSTRUCTION                               (query-side Instruct prefix;
                        default retrieval instruction, required by qwen3)
    ME_ADDRS            comma-separated; default: user@example.com

Examples:
    scripts/embed_mail.py --tier 1
    scripts/embed_mail.py --tier 2
    scripts/embed_mail.py --tier 3 --limit 100         # dry-ish trial
    scripts/embed_mail.py --all
    scripts/embed_mail.py --reembed --limit 1000       # rebuild trial
"""
from __future__ import annotations

import argparse
import email
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.policy import default as email_default
from email.utils import parsedate_to_datetime
from typing import TypedDict

import psycopg

from mail_reader.config import embed_url


# `from` is a Python keyword, so use TypedDict's functional syntax to keep the
# dict key matching the email header name exactly.
Headers = TypedDict("Headers", {
    "date":    "str | None",
    "from":    str,
    "to":      str,
    "subject": str,
})


class AttachmentPayload(TypedDict):
    filename: str
    mime: str
    size: int
    text_chars: int
    chunks: list[str]


class MessagePayload(TypedDict):
    mid: str
    hdr: Headers
    tid: str | None
    body_chars: int
    body_chunks: list[str]
    attachments: list[AttachmentPayload]

try:
    from mailparser_reply import EmailReplyParser
    # 'da' catches Norwegian "skrev" / Danish; 'sv' covers Swedish.
    _REPLY_PARSER = EmailReplyParser(languages=["en", "da", "sv"])
    HAVE_REPLY_PARSER = True
except Exception:
    HAVE_REPLY_PARSER = False

ME = [a.strip() for a in os.environ.get(
    "ME_ADDRS", "user@example.com").split(",") if a.strip()]
PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
EMBED = embed_url()  # $EMBED_URL → config hosts.embed (see config.embed_url)
MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding")
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "1024"))
BATCH_CHUNKS = int(os.environ.get("EMBED_BATCH", "32"))
# Cap total chars per /v1/embeddings call: oversized batched requests
# (32 × ~2000-char chunks ≈ 16k tokens) corrupt llama-server's embedding
# state — subsequent long-text calls return null vectors / hang (2026-08-03
# incident, reproduced on two builds). Keep each call small; long chunks
# dominate wall-time anyway, so throughput is barely affected.
BATCH_CHARS = int(os.environ.get("EMBED_BATCH_CHARS", "6000"))
# Qwen3-Embedding query-side instruction (retrieval): queries embedded
# without it retrieve junk micro-chunks over relevant content (2026-08-03).
QUERY_INSTRUCTION = os.environ.get(
    "EMBED_QUERY_INSTRUCTION",
    "Instruct: Given a search query, retrieve relevant email passages that "
    "answer the query\nQuery: ")

# ---------- notmuch helpers ----------

def nm_search_ids(query: str, limit: int | None = None) -> list[str]:
    cmd = ["notmuch", "search", "--output=messages"]
    if limit:
        cmd.append(f"--limit={limit}")
    cmd.append(query)
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [l[3:].strip() for l in out.splitlines() if l.startswith("id:")]

def nm_raw(mid: str) -> bytes:
    return subprocess.run(
        ["notmuch", "show", "--format=raw", f"id:{mid}"],
        check=True, capture_output=True).stdout

def nm_thread_id(mid: str) -> str | None:
    out = subprocess.run(
        ["notmuch", "search", "--output=threads", f"id:{mid}"],
        check=True, capture_output=True, text=True).stdout.strip()
    return out.split()[0] if out else None

# ---------- body extraction ----------

_HTML_STYLE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&[a-zA-Z#0-9]+;")
_WS_RUN = re.compile(r"[ \t]+")
_BLANKLINES = re.compile(r"\n\s*\n+")

def html_to_text(html: str) -> str:
    """
    >>> html_to_text("<p>hello <b>world</b></p>").strip()
    'hello world'
    >>> html_to_text("<style>x{}</style><p>hi</p>").strip()
    'hi'
    >>> html_to_text("a &amp; b &lt;c&gt; &nbsp; d").strip()
    'a & b <c> d'
    >>> html_to_text("<script>alert(1)</script>safe").strip()
    'safe'
    """
    t = _HTML_STYLE.sub(" ", html)
    t = _HTML_TAG.sub(" ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    t = _HTML_ENTITY.sub("", t)
    t = _WS_RUN.sub(" ", t)
    return _BLANKLINES.sub("\n\n", t)

# Heuristic quote stripping fallback (talon does this better).
_QUOTE_LINE = re.compile(r"^>.*$", re.MULTILINE)
_ON_WROTE = re.compile(
    r"\n[-\s]*\n?On .{1,120}? wrote:\s*\n.*\Z", re.DOTALL | re.IGNORECASE)
_FROM_HEADER = re.compile(
    r"\n[-\s]*\n?(From|Fra|Von|De): .{1,200}?\n.*\Z", re.DOTALL | re.IGNORECASE)
_SIG_DASH = re.compile(r"\n-- ?\n.*\Z", re.DOTALL)

def strip_quotes_sig(text: str) -> str:
    if HAVE_REPLY_PARSER:
        try:
            t = _REPLY_PARSER.read(text).latest_reply or text
        except Exception:
            t = text
    else:
        t = text
    # mail-parser-reply strips quoted history but not "-- " sig blocks; the
    # regex fallback strips both. Apply the sig stripper to both paths so
    # behavior is consistent.
    t = _ON_WROTE.sub("", t)
    t = _FROM_HEADER.sub("", t)
    t = _QUOTE_LINE.sub("", t)
    t = _SIG_DASH.sub("", t)
    return t.strip()

def pick_date(msg) -> str | None:
    # Some (mostly spammy) messages carry multiple Date: headers, the first of
    # which is garbage like year 2270. Pick the first one that parses to a
    # plausible instant (1990..now+1d) instead of trusting msg.get("Date").
    lo = datetime(1990, 1, 1, tzinfo=timezone.utc)
    hi = datetime.now(timezone.utc) + timedelta(days=1)
    for raw in msg.get_all("Date", []):
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if lo <= dt <= hi:
            return dt.isoformat()
    return None

def parse_message(raw: bytes) -> tuple[Headers, str]:
    msg = email.message_from_bytes(raw, policy=email_default)
    headers: Headers = {
        "date":    pick_date(msg),
        "from":    msg.get("From", ""),
        "to":      msg.get("To", ""),
        "subject": msg.get("Subject", ""),
    }
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return headers, ""
    content = body.get_content()
    if body.get_content_type() == "text/html":
        content = html_to_text(content)
    return headers, strip_quotes_sig(content)

# ---------- attachment extraction ----------

# MIME types we extract text from. Everything else gets a metadata-only row
# (text_chars=0) so a future VLM pass can find image/unsupported attachments.
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_ODT_MIME = "application/vnd.oasis.opendocument.text"
_PLAIN_TEXT_MIMES = {
    "text/plain", "text/csv", "text/markdown", "text/calendar",
    "application/ics", "application/json",
}

def _strip_xml(text: str) -> str:
    """
    >>> _strip_xml('<w:p><w:r><w:t>Hello</w:t></w:r></w:p>').strip()
    'Hello'
    >>> _strip_xml('a &amp; b &lt;c&gt;').strip()
    'a & b <c>'
    """
    t = re.sub(r"<[^>]+>", " ", text)
    t = (t.replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
    t = _HTML_ENTITY.sub("", t)
    t = _WS_RUN.sub(" ", t)
    return _BLANKLINES.sub("\n\n", t).strip()

def extract_pdf(data: bytes) -> str:
    """Run `pdftotext - -` on the bytes. Returns '' on any failure."""
    try:
        r = subprocess.run(
            ["pdftotext", "-q", "-layout", "-", "-"],
            input=data, capture_output=True, timeout=60)
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", errors="replace").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""

def _extract_from_zip(data: bytes, inner: str) -> str:
    """Open `data` as a zip, read `inner`, strip XML. '' on failure."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open(inner) as f:
                return _strip_xml(f.read().decode("utf-8", errors="replace"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return ""

def extract_docx(data: bytes) -> str:
    return _extract_from_zip(data, "word/document.xml")

def extract_odt(data: bytes) -> str:
    return _extract_from_zip(data, "content.xml")

def extract_attachment_text(mime: str, data: bytes) -> str:
    """
    Dispatch on MIME. Returns '' for unsupported types (images, video, blobs).
    >>> extract_attachment_text("text/plain", b"hello world").strip()
    'hello world'
    >>> extract_attachment_text("image/png", b"\\x89PNG...")
    ''
    """
    mime = (mime or "").lower()
    if mime == "application/pdf":
        return extract_pdf(data)
    if mime == _DOCX_MIME:
        return extract_docx(data)
    if mime == _ODT_MIME:
        return extract_odt(data)
    if mime == "text/html":
        return html_to_text(data.decode("utf-8", errors="replace")).strip()
    if mime in _PLAIN_TEXT_MIMES:
        return data.decode("utf-8", errors="replace").strip()
    return ""

def iter_attachments(raw: bytes) -> Iterator[tuple[str, str, bytes, str]]:
    """Yield (filename, mime_type, bytes, extracted_text) per attachment part.

    A part is treated as an attachment if it has a `filename` parameter OR an
    explicit `Content-Disposition: attachment`. This skips body alternatives
    that parse_message already returned.
    """
    msg = email.message_from_bytes(raw, policy=email_default)
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        disp = (part.get_content_disposition() or "").lower()
        if not fn and disp != "attachment":
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            continue
        # decode=True returns bytes for non-multipart leaf parts; skip otherwise.
        if not isinstance(data, bytes):
            continue
        mime = part.get_content_type()
        text = extract_attachment_text(mime, data)
        yield (fn or "(unnamed)", mime, data, text)

# ---------- chunking ----------

# Rough: 1 token ~ 4 chars for English/Norwegian mixed. 512 tok ~ 2048 chars.
CHUNK_CHARS = 2048
OVERLAP = 256

def chunk_text(text: str) -> list[str]:
    """
    >>> chunk_text("")
    []
    >>> chunk_text("   ")
    []
    >>> chunk_text("short message")
    ['short message']
    >>> chunks = chunk_text("x" * 5000)
    >>> len(chunks) >= 2 and all(len(c) <= CHUNK_CHARS for c in chunks)
    True
    >>> chunk_text("hello\\x00world")    # NULs stripped (TEXT cols reject them)
    ['helloworld']
    >>> chunk_text("padding ͏\\xa0͏\\xa0͏\\xa0 here")  # invisible joiners stripped
    ['padding \\xa0\\xa0\\xa0 here']
    """
    # PostgreSQL TEXT cannot store NUL bytes; strip them everywhere before
    # they reach the writer. Real mail with NULs has been seen from ancient
    # senders (ifi.uio.no, blackberry.rim.net) — usually a stray byte in PDF
    # or DOCX extracted text.
    text = text.replace("\x00", "")
    # Invisible joiner/format chars (newsletter anti-truncation padding,
    # e.g. LinkedIn's CGJ+NBSP runs) make qwen3-embedding produce NaN
    # vectors that llama-server serializes as null (2026-08-03 incident).
    text = re.sub("[͏​‌‍⁠﻿]", "", text).strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks = []
    i = 0
    while i < len(text):
        end = min(i + CHUNK_CHARS, len(text))
        # snap to nearest blankline going back, if feasible
        if end < len(text):
            snap = text.rfind("\n\n", i + CHUNK_CHARS - 512, end)
            if snap > i + 512:
                end = snap
        chunks.append(text[i:end].strip())
        if end == len(text):
            break
        i = max(end - OVERLAP, i + 1)
    return [c for c in chunks if c]

# ---------- embedding (OpenAI-compatible /v1/embeddings) ----------

def _fit_dims(v: list[float]) -> list[float]:
    """Force a server vector to the stored dim.

    Qwen3-Embedding is Matryoshka-trained and the server ignores the OpenAI
    `dimensions` parameter, so a natively wider vector is truncated to
    EMBED_DIMS and L2-renormalized client-side (sanctioned MRL usage).
    A *shorter* vector means the wrong model is being served — fail the
    batch loudly rather than poison the store with a foreign vector space
    (the 2026-06-02 lesson)."""
    if len(v) < EMBED_DIMS:
        raise RuntimeError(
            f"embed: vector dim {len(v)} < EMBED_DIMS {EMBED_DIMS} — "
            f"wrong model served?")
    if len(v) > EMBED_DIMS:
        v = v[:EMBED_DIMS]
        norm = math.sqrt(sum(x * x for x in v))
        if norm > 0:
            v = [x / norm for x in v]
    return v

def _post_embeddings(texts: list[str]) -> list[list[float]]:
    """One /v1/embeddings round-trip; raw server vectors, unvalidated."""
    req = urllib.request.Request(
        f"{EMBED}/v1/embeddings",
        data=json.dumps({"model": MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    data = out.get("data")
    if data is None or len(data) != len(texts):
        raise RuntimeError(
            f"embed: expected {len(texts)} vectors, got {len(data) if data else 0}")
    return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]

def _has_nulls(v: list[float] | None) -> bool:
    return not isinstance(v, list) or not v or any(x is None for x in v)

def embed_batch(texts: list[str]) -> list[list[float]]:
    """POST a list of strings, get a list of EMBED_DIMS embedding vectors back.

    Texts are split into sub-batches capped by count (BATCH_CHUNKS) AND total
    chars (BATCH_CHARS) — oversized batched requests corrupt llama-server's
    embedding state (2026-08-03 incident; see BATCH_CHARS).

    llama-server can also return null embeddings transiently: retry the
    sub-batch with backoff, then retry the poisoned texts individually to
    isolate genuinely bad inputs; only raise if a text stays null."""
    if not texts:
        return []
    out: list[list[float]] = []
    group: list[str] = []
    group_chars = 0
    for t in texts:
        if group and (len(group) >= BATCH_CHUNKS
                      or group_chars + len(t) > BATCH_CHARS):
            out += _embed_group(group)
            group, group_chars = [], 0
        group.append(t)
        group_chars += len(t)
    if group:
        out += _embed_group(group)
    return out

def _embed_group(texts: list[str]) -> list[list[float]]:
    """Embed one size-capped group of texts, with null-retry handling."""
    vecs = None
    for attempt in range(3):
        try:
            vecs = _post_embeddings(texts)
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if not any(_has_nulls(v) for v in vecs):
            break
        time.sleep(2 * (attempt + 1))
    if vecs is None:  # unreachable (3rd transport failure re-raises)
        raise RuntimeError("embed: no response after retries")
    fixed: list[list[float]] = []
    for i, v in enumerate(vecs):
        if _has_nulls(v):
            for attempt in range(3):
                v = _post_embeddings([texts[i]])[0]
                if not _has_nulls(v):
                    break
                time.sleep(2 * (attempt + 1))
            if _has_nulls(v):
                raise RuntimeError(
                    f"embed: server persistently returns null vector for "
                    f"text {i}/{len(texts)} (len {len(texts[i])} chars)")
        fixed.append(v)
    return [_fit_dims(v) for v in fixed]

def embed_query(text: str) -> list[float]:
    """Query-side embedding with Qwen3-Embedding's instruction prefix.

    Without it the model ranks junk micro-chunks (control-char PDF
    extraction artifacts) above genuinely relevant content; with it the
    ranking inverts correctly (2026-08-03 verification). Documents are
    indexed plain — the prefix belongs to queries only, per the model's
    training. Chunk-vs-chunk similarity (tankekart) also goes plain."""
    return embed_batch([QUERY_INSTRUCTION + text])[0]

def vec_literal(v: list[float]) -> str:
    """
    >>> vec_literal([0.1, -0.25, 1.0])
    '[0.100000,-0.250000,1.000000]'
    >>> vec_literal([])
    '[]'
    """
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

# ---------- tier queries ----------

# Tier 6: Norwegian governmental / institutional senders. These are addresses
# whose mail is rarely replied to (so tier 3 misses them) but is high-value
# for retrieval — tax letters, school portal notifications, postal pickups,
# health system mail, etc. Extend as needed.
TIER6_DOMAINS: tuple[str, ...] = (
    "skatteetaten.no", "altinn.no", "digipost.no", "oslo.kommune.no",
    "nav.no", "helsenorge.no", "helsedirektoratet.no", "fhi.no",
    "pasientreiser.no", "politiet.no", "statensvegvesen.no", "digdir.no",
    "posten.no", "bring.no", "vy.no", "ruter.no",
)


def tier_query(tier: int) -> str:
    me_or = " or ".join(f"from:{a}" for a in ME)
    if tier == 1:
        return "date:1y.."
    if tier == 2:
        return f'thread:"{{{me_or}}}"'
    if tier == 3:
        recips = subprocess.run(
            ["notmuch", "address", "--output=recipients", me_or],
            check=True, capture_output=True, text=True).stdout
        addrs = sorted(set(re.findall(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", recips)))
        # drop our own
        addrs = [a for a in addrs if a.lower() not in {m.lower() for m in ME}]
        return " or ".join(f"from:{a}" for a in addrs)
    if tier == 4:
        return "tag:digest::keep"
    if tier == 5:
        return "attachment"
    if tier == 6:
        return " or ".join(f"from:*@{d}" for d in TIER6_DOMAINS)
    raise ValueError(tier)

# ---------- main loop ----------

def _prepare_message(mid: str) -> MessagePayload | None:
    """Pull raw, parse body + attachments. Returns a payload dict, or None
    if nothing about this message is worth recording (no body chunks and no
    attachments at all). Raises on notmuch / parse errors."""
    raw = nm_raw(mid)
    hdr, body = parse_message(raw)
    body_chunks = chunk_text(body) if len(body) >= 40 else []
    attachments = []
    for fn, mime, data, text in iter_attachments(raw):
        a_chunks = chunk_text(text) if text else []
        attachments.append({
            "filename": fn,
            "mime": mime,
            "size": len(data),
            "text_chars": len(text),
            "chunks": a_chunks,
        })
    if not body_chunks and not attachments:
        return None
    return {
        "mid": mid,
        "hdr": hdr,
        "tid": nm_thread_id(mid),
        "body_chars": len(body),
        "body_chunks": body_chunks,
        "attachments": attachments,
    }

Slot = tuple[str, int] | tuple[str, int, int]   # ("body", ci) or ("att", ai, ci)


def _embed_pending(batch: list[MessagePayload]) -> dict[int, dict[Slot, list[float]]]:
    """Flatten every chunk in the batch, embed in one HTTP call, and map the
    vectors back to their (batch index, slot)."""
    flat: list[str] = []
    locator: list[tuple[int, Slot]] = []
    for mi, payload in enumerate(batch):
        for ci, c in enumerate(payload["body_chunks"]):
            flat.append(c)
            locator.append((mi, ("body", ci)))
        for ai, att in enumerate(payload["attachments"]):
            for ci, c in enumerate(att["chunks"]):
                flat.append(c)
                locator.append((mi, ("att", ai, ci)))

    embeds = embed_batch(flat) if flat else []
    per_msg: dict[int, dict[Slot, list[float]]] = defaultdict(dict)
    for (mi, slot), v in zip(locator, embeds):
        per_msg[mi][slot] = v
    return per_msg


def _insert_payload_rows(cur: psycopg.Cursor, row_id: int,
                         payload: MessagePayload,
                         slots: dict[Slot, list[float]]) -> None:
    """Insert attachment + chunk rows for one prepared message against an
    existing `messages` row id. The caller owns the transaction."""
    for ci, c in enumerate(payload["body_chunks"]):
        cur.execute(
            "INSERT INTO chunks "
            "(message_id, attachment_id, chunk_idx, text, embedding) "
            "VALUES (%s, NULL, %s, %s, %s::vector)",
            (row_id, ci, c, vec_literal(slots[("body", ci)])))
    for ai, att in enumerate(payload["attachments"]):
        cur.execute("""
            INSERT INTO attachments
              (message_id, filename, mime_type, size_bytes, text_chars)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (row_id, att["filename"], att["mime"],
              att["size"], att["text_chars"]))
        att_row = cur.fetchone()
        assert att_row is not None, "RETURNING id must yield a row"
        att_id = att_row[0]
        for ci, c in enumerate(att["chunks"]):
            cur.execute(
                "INSERT INTO chunks "
                "(message_id, attachment_id, chunk_idx, text, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                (row_id, att_id, ci, c,
                 vec_literal(slots[("att", ai, ci)])))


def _flush_batch(conn: psycopg.Connection, tier: int, batch: list[MessagePayload],
                 verbose: bool) -> tuple[int, int]:
    """Embed all chunks in one HTTP call, then write each message in its own tx."""
    per_msg = _embed_pending(batch)

    done = failed = 0
    for mi, payload in enumerate(batch):
        slots = per_msg.get(mi, {})
        try:
            with conn.cursor() as cur, conn.transaction():
                cur.execute("""
                    INSERT INTO messages
                      (message_id, date, from_addr, to_addrs, subject,
                       thread_id, tier, body_chars)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (payload["mid"], payload["hdr"]["date"],
                      payload["hdr"]["from"], payload["hdr"]["to"],
                      payload["hdr"]["subject"], payload["tid"], tier,
                      payload["body_chars"]))
                row = cur.fetchone()
                assert row is not None, "RETURNING id must yield a row"
                row_id = row[0]
                _insert_payload_rows(cur, row_id, payload, slots)
            done += 1
        except Exception as e:
            failed += 1
            if verbose:
                print(f"  !! {payload['mid']} (write): {e}", file=sys.stderr)
    return done, failed

def process(conn: psycopg.Connection, tier: int, limit: int | None, verbose: bool) -> None:
    q = tier_query(tier)
    if verbose:
        print(f"[tier {tier}] query: {q[:200]}{'…' if len(q)>200 else ''}",
              file=sys.stderr)
    ids = nm_search_ids(q, limit)
    if verbose:
        print(f"[tier {tier}] {len(ids)} candidate messages "
              f"(batch={BATCH_CHUNKS})", file=sys.stderr)

    done = skipped = failed = 0
    pending: list[MessagePayload] = []
    pending_chunks = 0
    t0 = time.time()

    for n, mid in enumerate(ids, 1):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM messages WHERE message_id = %s", (mid,))
            if cur.fetchone():
                skipped += 1
                continue
        try:
            payload = _prepare_message(mid)
        except Exception as e:
            failed += 1
            print(f"  !! {mid} (prepare): {e}", file=sys.stderr)
            continue
        if payload is None:
            skipped += 1
            continue
        pending.append(payload)
        pending_chunks += (len(payload["body_chunks"])
                           + sum(len(a["chunks"]) for a in payload["attachments"]))
        if pending_chunks >= BATCH_CHUNKS:
            try:
                d, f = _flush_batch(conn, tier, pending, verbose)
                done += d; failed += f
            except Exception as e:
                failed += len(pending)
                print(f"  !! batch embed failed ({len(pending)} msgs): {e}",
                      file=sys.stderr)
            pending = []
            pending_chunks = 0

        if verbose and n % 100 == 0:
            rate = n / (time.time() - t0)
            eta = (len(ids) - n) / rate if rate else 0
            print(f"  {n}/{len(ids)}  {rate:.1f} msg/s  eta {eta/60:.1f} min  "
                  f"done={done} skipped={skipped} failed={failed}",
                  file=sys.stderr)

    if pending:
        try:
            d, f = _flush_batch(conn, tier, pending, verbose)
            done += d; failed += f
        except Exception as e:
            failed += len(pending)
            print(f"  !! final batch embed failed ({len(pending)} msgs): {e}",
                  file=sys.stderr)

    if verbose or failed:
        print(f"[tier {tier}] done={done} skipped={skipped} failed={failed} "
              f"elapsed={(time.time()-t0)/60:.1f} min", file=sys.stderr)

# ---------- reembed (chunks/attachments rebuild for a model switch) ----------

def _flush_reembed(conn: psycopg.Connection,
                   batch: list[tuple[int, MessagePayload]],
                   verbose: bool) -> tuple[int, int]:
    """Variant of `_flush_batch` for `--reembed`: (messages.id, payload) pairs.
    Inserts only `attachments` + `chunks` rows against the existing messages
    row — the messages metadata stays untouched."""
    per_msg = _embed_pending([p for _, p in batch])

    done = failed = 0
    for mi, (row_id, payload) in enumerate(batch):
        slots = per_msg.get(mi, {})
        try:
            with conn.cursor() as cur, conn.transaction():
                _insert_payload_rows(cur, row_id, payload, slots)
            done += 1
        except Exception as e:
            failed += 1
            if verbose:
                print(f"  !! {payload['mid']} (write): {e}", file=sys.stderr)
    return done, failed

def process_reembed(conn: psycopg.Connection, limit: int | None,
                    verbose: bool) -> None:
    """Re-embed every `messages` row that has no chunks and no attachments
    (the state after `TRUNCATE chunks, attachments`). Resumable: already
    rebuilt rows no longer match the query, so just re-run after a crash."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.message_id
            FROM messages m
            WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_id = m.id)
              AND NOT EXISTS (SELECT 1 FROM attachments a
                              WHERE a.message_id = m.id)
            ORDER BY m.id
            """ + (" LIMIT %s" if limit else ""),
            (limit,) if limit else (),
        )
        rows = cur.fetchall()
    if verbose:
        print(f"[reembed] {len(rows)} messages missing chunks "
              f"(batch={BATCH_CHUNKS})", file=sys.stderr)

    done = skipped = failed = 0
    pending: list[tuple[int, MessagePayload]] = []
    pending_chunks = 0
    t0 = time.time()

    def flush() -> None:
        nonlocal done, failed, pending, pending_chunks
        if not pending:
            return
        try:
            d, f = _flush_reembed(conn, pending, verbose)
            done += d; failed += f
        except Exception as e:
            failed += len(pending)
            print(f"  !! batch embed failed ({len(pending)} msgs): {e}",
                  file=sys.stderr)
        pending = []
        pending_chunks = 0

    for n, (row_id, mid) in enumerate(rows, 1):
        try:
            payload = _prepare_message(mid)
        except Exception as e:
            failed += 1
            print(f"  !! {mid} (prepare): {e}", file=sys.stderr)
            continue
        if payload is None:
            # Nothing recordable (no body, no attachments) — will re-match
            # the query on the next run; cheap to re-check, harmless.
            skipped += 1
            continue
        pending.append((row_id, payload))
        pending_chunks += (len(payload["body_chunks"])
                           + sum(len(a["chunks"]) for a in payload["attachments"]))
        if pending_chunks >= BATCH_CHUNKS:
            flush()

        if verbose and n % 100 == 0:
            rate = n / (time.time() - t0)
            eta = (len(rows) - n) / rate if rate else 0
            print(f"  {n}/{len(rows)}  {rate:.1f} msg/s  eta {eta/60:.1f} min  "
                  f"done={done} skipped={skipped} failed={failed}",
                  file=sys.stderr)

    flush()

    if verbose or failed:
        print(f"[reembed] done={done} skipped={skipped} failed={failed} "
              f"elapsed={(time.time()-t0)/60:.1f} min", file=sys.stderr)

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", type=int, choices=[1, 2, 3, 4, 5, 6])
    ap.add_argument("--all", action="store_true",
                    help="run tiers in order 2,1,3,6,5,4 (smallest first)")
    ap.add_argument("--reembed", action="store_true",
                    help="rebuild chunks/attachments for `messages` rows that "
                         "have none (post-TRUNCATE model switch); resumable")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not (args.tier or args.all or args.reembed):
        ap.error("pass --tier N, --all or --reembed")

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        if args.reembed:
            process_reembed(conn, args.limit, verbose=not args.quiet)
            return 0
        # Order matters only for runtime feedback (smaller tiers finish first
        # so failures surface fast). Idempotency on message_id means overlap
        # between tiers gets skipped on the second visit, so the ORDER also
        # determines which tier label each message ends up tagged with: a
        # message in both tier 1 and tier 4 will be embedded under tier 1
        # (whichever runs first) and skipped by tier 4.
        tiers = [2, 1, 3, 6, 5, 4] if args.all else [args.tier]
        for t in tiers:
            process(conn, t, args.limit, verbose=not args.quiet)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
