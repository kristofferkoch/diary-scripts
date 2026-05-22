"""Tankekart: per-chunk branches of semantically related mail.

Strategy: each body chunk of the open mail is its own query vector. For
each chunk we run a top-N nearest-neighbour search across `chunks` (body
chunks only, attachments excluded), grouped by message. The chunks
themselves become the branches — each branch's leaves are the mails
most related to *that part* of the open mail. Same-message and same-
thread results are excluded since the thread is already in arm's reach
from the detail page.

If the open message isn't in `mailvec` yet (`embed_mail.py` runs every
15 min — newest mail is often not indexed), fall back to live-embedding
via bge-m3 on the fly. Read-only; the next embed cycle picks it up.

Branch labels are the first ~80 chars of the source chunk in v1. The
LLM-named-branches variant lives in IDEAS.md (option 4) and depends on
the richer extraction work (themes column on `summaries`).
"""
from __future__ import annotations

import re
import sys as _sys
from datetime import datetime
from pathlib import Path as _Path
from typing import TypedDict

import psycopg

# scripts/ isn't a Python package; put it on sys.path so we can import
# the already-tested mail helpers instead of duplicating notmuch+MIME code.
_SCRIPTS_DIR = str(_Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
from embed_mail import (  # noqa: E402  (sys.path tweak above)
    chunk_text,
    embed_batch,
    nm_raw,
    parse_message,
)


class Related(TypedDict):
    message_id: str          # notmuch Message-ID (no <>)
    date: datetime | None
    from_addr: str
    subject: str
    distance: float
    similarity: float        # 1 - distance, clamped to [0, 1]
    summary: str | None      # final/partial text; set by server.py
    summary_status: str      # 'done' | 'pending' | 'streaming' | 'failed'
    summary_error: str | None


class Branch(TypedDict):
    chunk_idx: int           # source-chunk position, 0-indexed
    label: str               # short preview of the source chunk (v1)
    leaves: list[Related]


LABEL_CHARS = 80
DEFAULT_LEAVES_PER_BRANCH = 4
# Some mail (forwarded threads, mailing-list digests) chunks into dozens.
# Running one LATERAL nearest-neighbour subquery per chunk hits a wall
# fast (~15 s for 90 chunks). Cap at a handful — first N is fine in v1,
# representative-chunk selection (option 3 in IDEAS.md) is the upgrade.
MAX_BRANCHES = 6
_WS = re.compile(r"\s+")


def _label_for_chunk(text: str) -> str:
    """Compact a chunk into a single-line label. Collapse whitespace,
    cut at the first sensible boundary, ellipsis if truncated."""
    flat = _WS.sub(" ", text).strip()
    if len(flat) <= LABEL_CHARS:
        return flat
    # Try to cut at a word boundary near the limit.
    cut = flat.rfind(" ", LABEL_CHARS - 16, LABEL_CHARS)
    if cut < LABEL_CHARS // 2:
        cut = LABEL_CHARS
    return flat[:cut].rstrip() + "…"


def _leaf_row(mid: str, date, from_addr: str | None,
              subject: str | None, dist: float) -> Related:
    return {
        "message_id": mid,
        "date": date,
        "from_addr": from_addr or "",
        "subject": subject or "",
        "distance": dist,
        "similarity": max(0.0, min(1.0, 1.0 - dist)),
        "summary": None,
        "summary_status": "pending",
        "summary_error": None,
    }


# ----------------- indexed path (open mail is in mailvec) -----------------

def _branches_indexed(conn: psycopg.Connection, msg_row_id: int,
                      thread_id: str | None,
                      n_per_branch: int) -> list[Branch]:
    """One SQL query: for each chunk of the source message, take the
    top-N nearest neighbours (one row per neighbour message)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH src AS (
                SELECT chunk_idx, text, embedding
                FROM chunks
                WHERE message_id = %s AND attachment_id IS NULL
                ORDER BY chunk_idx
                LIMIT %s
            )
            SELECT s.chunk_idx, s.text,
                   r.message_id, r.date, r.from_addr, r.subject, r.dist
            FROM src s
            CROSS JOIN LATERAL (
                SELECT m.message_id, m.date, m.from_addr, m.subject,
                       MIN(c.embedding <=> s.embedding) AS dist
                FROM chunks c
                JOIN messages m ON m.id = c.message_id
                WHERE c.attachment_id IS NULL
                  AND m.id <> %s::bigint
                  AND (m.thread_id IS NULL
                       OR %s::text IS NULL
                       OR m.thread_id <> %s::text)
                GROUP BY m.id, m.message_id, m.date, m.from_addr, m.subject
                ORDER BY MIN(c.embedding <=> s.embedding) ASC
                LIMIT %s
            ) r
            ORDER BY s.chunk_idx ASC, r.dist ASC
            """,
            (msg_row_id, MAX_BRANCHES, msg_row_id, thread_id, thread_id, n_per_branch),
        )
        rows = cur.fetchall()
    return _group_branches(rows)


# ----------------- live-embed path (open mail not in mailvec) --------------

def _branches_live(conn: psycopg.Connection,
                   notmuch_msg_id: str,
                   n_per_branch: int) -> list[Branch]:
    """For mail not yet in `messages`: extract body, embed each chunk via
    bge-m3, run one nearest-neighbour query per chunk."""
    try:
        raw = nm_raw(notmuch_msg_id)
    except Exception:
        return []
    _, body = parse_message(raw)
    if len(body) < 40:
        return []
    chunk_texts = chunk_text(body)
    if not chunk_texts:
        return []
    chunk_texts = chunk_texts[:MAX_BRANCHES]
    vecs = embed_batch(chunk_texts)
    if not vecs or len(vecs) != len(chunk_texts):
        return []

    branches: list[Branch] = []
    with conn.cursor() as cur:
        for idx, (ctext, cvec) in enumerate(zip(chunk_texts, vecs)):
            cur.execute(
                """
                SELECT m.message_id, m.date, m.from_addr, m.subject,
                       MIN(c.embedding <=> %s::vector) AS dist
                FROM chunks c
                JOIN messages m ON m.id = c.message_id
                WHERE c.attachment_id IS NULL
                GROUP BY m.id, m.message_id, m.date, m.from_addr, m.subject
                ORDER BY dist ASC
                LIMIT %s
                """,
                (cvec, n_per_branch),
            )
            leaves = [
                _leaf_row(mid, date, from_addr, subject, float(dist))
                for mid, date, from_addr, subject, dist in cur.fetchall()
            ]
            if leaves:
                branches.append({
                    "chunk_idx": idx,
                    "label": _label_for_chunk(ctext),
                    "leaves": leaves,
                })
    return branches


# ----------------- shared row grouping -----------------

def _group_branches(rows) -> list[Branch]:
    """Fold a sequence of (chunk_idx, chunk_text, ...leaf cols) rows into
    branches. Rows are assumed sorted by chunk_idx, then by dist."""
    branches: list[Branch] = []
    current: Branch | None = None
    for chunk_idx, chunk_text_src, mid, date, from_addr, subject, dist in rows:
        if current is None or current["chunk_idx"] != chunk_idx:
            current = {
                "chunk_idx": chunk_idx,
                "label": _label_for_chunk(chunk_text_src),
                "leaves": [],
            }
            branches.append(current)
        current["leaves"].append(
            _leaf_row(mid, date, from_addr, subject, float(dist))
        )
    return branches


# ----------------- public entry point -----------------

def tankekart(conn: psycopg.Connection, notmuch_msg_id: str,
              n_per_branch: int = DEFAULT_LEAVES_PER_BRANCH) -> list[Branch]:
    """Return the open mail's branches, each branch a per-chunk top-N
    of semantic neighbours."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, thread_id FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        row = cur.fetchone()
    if row is None:
        return _branches_live(conn, notmuch_msg_id, n_per_branch)
    msg_row_id, thread_id = row
    return _branches_indexed(conn, msg_row_id, thread_id, n_per_branch)
