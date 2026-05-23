"""Tankekart: branches of related mail, selectable across three modes.

* `chunks`  (default) — each body chunk of the open mail is its own query
  vector; top-N nearest neighbours per chunk become the branch's leaves.
  Labels are the first ~80 chars of the source chunk.
* `themes`  — branches are the open mail's tier-2 themes. Leaves are
  other mails linked to the same theme via `summary_themes`; if a theme
  undersupplies, top up via vector nearest-neighbours over `themes.embedding`.
* `emergent` — pull a wider candidate set via the chunks query, then
  cluster the candidates' tier-2 themes. Top 3-5 theme-clusters become
  branches; useful for "what are these mails *about*" when the open
  mail's own themes don't capture the neighbourhood.

If the open message isn't in `mailvec` yet (`embed_mail.py` runs every
15 min — newest mail is often not indexed), the chunks mode falls back
to live-embedding via bge-m3 on the fly. The themes and emergent modes
require a done tier-2 summary and return [] otherwise.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Literal, TypedDict

import psycopg

from scripts.embed_mail import chunk_text, embed_batch, nm_raw, parse_message

from .thread_id import ThreadId


Mode = Literal["chunks", "themes", "emergent"]


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
    summary_action_required: bool


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
        "summary_action_required": False,
    }


# ----------------- indexed path (open mail is in mailvec) -----------------

def _branches_indexed(conn: psycopg.Connection, msg_row_id: int,
                      thread_id: ThreadId | None,
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
            (msg_row_id, MAX_BRANCHES, msg_row_id,
             thread_id.db_form if thread_id is not None else None,
             thread_id.db_form if thread_id is not None else None,
             n_per_branch),
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


# ----------------- themes mode -----------------

def _branches_themes(conn: psycopg.Connection, msg_row_id: int,
                     thread_id: ThreadId | None,
                     n_per_branch: int) -> list[Branch]:
    """Branches are the open mail's own themes (from its best done
    summary at the current prompt version). Each branch's leaves come
    from `summary_themes` joined on the same theme_id; if a branch
    undersupplies, top up with mails linked to nearest-neighbour theme
    ids (8-NN over `themes.embedding`)."""
    from . import summarize  # local to avoid an import cycle at module load
    pv = summarize.PROMPT_VERSION
    tid_param = thread_id.db_form if thread_id is not None else None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.text
            FROM summaries s
            JOIN summary_themes st ON st.summary_id = s.id
            JOIN themes t ON t.id = st.theme_id
            WHERE s.message_id = %s
              AND s.prompt_version = %s
              AND s.status = 'done'
            ORDER BY s.quality_tier DESC, s.generated_at DESC, t.id
            """,
            (msg_row_id, pv),
        )
        rows = cur.fetchall()

    seen_themes: set[int] = set()
    open_themes: list[tuple[int, str]] = []
    for tid, text in rows:
        if tid in seen_themes:
            continue
        seen_themes.add(tid)
        open_themes.append((tid, text))
        if len(open_themes) >= MAX_BRANCHES:
            break
    if not open_themes:
        return []

    branches: list[Branch] = []
    with conn.cursor() as cur:
        for branch_idx, (tid, text) in enumerate(open_themes):
            cur.execute(
                """
                SELECT DISTINCT ON (m.message_id)
                       m.message_id, m.date, m.from_addr, m.subject
                FROM summary_themes st
                JOIN summaries s ON s.id = st.summary_id AND s.status = 'done'
                JOIN messages m ON m.id = s.message_id
                WHERE st.theme_id = %s
                  AND m.id <> %s
                  AND (m.thread_id IS NULL OR %s::text IS NULL
                       OR m.thread_id <> %s::text)
                ORDER BY m.message_id, m.date DESC NULLS LAST
                LIMIT %s
                """,
                (tid, msg_row_id, tid_param, tid_param, n_per_branch),
            )
            join_rows = cur.fetchall()
            seen_mids = {r[0] for r in join_rows}

            fallback_rows: list = []
            if len(join_rows) < n_per_branch:
                need = n_per_branch - len(join_rows)
                cur.execute(
                    """
                    WITH near_themes AS (
                        SELECT id
                        FROM themes
                        WHERE id <> %s
                        ORDER BY embedding <=> (
                            SELECT embedding FROM themes WHERE id = %s
                        )
                        LIMIT 8
                    )
                    SELECT DISTINCT ON (m.message_id)
                           m.message_id, m.date, m.from_addr, m.subject
                    FROM near_themes nt
                    JOIN summary_themes st ON st.theme_id = nt.id
                    JOIN summaries s ON s.id = st.summary_id AND s.status = 'done'
                    JOIN messages m ON m.id = s.message_id
                    WHERE m.id <> %s
                      AND NOT (m.message_id = ANY(%s::text[]))
                      AND (m.thread_id IS NULL OR %s::text IS NULL
                           OR m.thread_id <> %s::text)
                    ORDER BY m.message_id, m.date DESC NULLS LAST
                    LIMIT %s
                    """,
                    (tid, tid, msg_row_id, list(seen_mids),
                     tid_param, tid_param, need),
                )
                fallback_rows = cur.fetchall()

            leaves: list[Related] = [
                _leaf_row(mid, date, from_addr, subject, 0.0)
                for mid, date, from_addr, subject in (
                    list(join_rows) + list(fallback_rows)
                )
            ]
            if leaves:
                branches.append({
                    "chunk_idx": branch_idx,
                    "label": text,
                    "leaves": leaves,
                })
    return branches


# ----------------- emergent mode -----------------

def _branches_emergent(conn: psycopg.Connection, msg_row_id: int,
                       thread_id: ThreadId | None,
                       n_per_branch: int) -> list[Branch]:
    """Cluster the chunks-mode candidate set by their tier-2 themes. Top
    3-5 theme-clusters by mail count become branches; leaves are the
    candidate mails belonging to that cluster, sorted by their
    semantic distance to the open mail (so the similarity bars stay
    meaningful)."""
    chunk_branches = _branches_indexed(conn, msg_row_id, thread_id,
                                       n_per_branch=10)
    candidates: dict[str, Related] = {}
    for b in chunk_branches:
        for leaf in b["leaves"]:
            mid = leaf["message_id"]
            existing = candidates.get(mid)
            if existing is None or leaf["distance"] < existing["distance"]:
                candidates[mid] = leaf
    if not candidates:
        return []

    from . import summarize
    pv = summarize.PROMPT_VERSION
    cand_mids = list(candidates.keys())
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.text, m.message_id
            FROM messages m
            JOIN summaries s ON s.message_id = m.id
                            AND s.prompt_version = %s
                            AND s.status = 'done'
            JOIN summary_themes st ON st.summary_id = s.id
            JOIN themes t ON t.id = st.theme_id
            WHERE m.message_id = ANY(%s::text[])
            """,
            (pv, cand_mids),
        )
        rows = cur.fetchall()

    theme_to_mids: dict[int, list[str]] = defaultdict(list)
    theme_label: dict[int, str] = {}
    for tid, text, mid in rows:
        theme_to_mids[tid].append(mid)
        theme_label[tid] = text
    if not theme_to_mids:
        return []

    top = sorted(theme_to_mids.items(),
                 key=lambda kv: (-len(kv[1]), kv[0]))[:5]

    branches: list[Branch] = []
    for idx, (tid, mids) in enumerate(top):
        leaves = sorted(
            (candidates[m] for m in mids if m in candidates),
            key=lambda l: l["distance"],
        )[:n_per_branch]
        if leaves:
            branches.append({
                "chunk_idx": idx,
                "label": theme_label[tid],
                "leaves": leaves,
            })
    return branches


# ----------------- public entry point -----------------

def tankekart(conn: psycopg.Connection, notmuch_msg_id: str,
              n_per_branch: int = DEFAULT_LEAVES_PER_BRANCH,
              mode: Mode = "chunks") -> list[Branch]:
    """Return branches for the open mail in the requested mode.

    `chunks` (default) reproduces the legacy behaviour and is the only
    mode that survives the open mail not being indexed (falls back to
    live-embedding). `themes` and `emergent` require a done tier-2
    summary on the open mail's row and return [] otherwise."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, thread_id FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        row = cur.fetchone()
    if row is None:
        if mode == "chunks":
            return _branches_live(conn, notmuch_msg_id, n_per_branch)
        return []
    msg_row_id, thread_id_raw = row
    thread_id = ThreadId(thread_id_raw) if thread_id_raw is not None else None
    if mode == "themes":
        return _branches_themes(conn, msg_row_id, thread_id, n_per_branch)
    if mode == "emergent":
        return _branches_emergent(conn, msg_row_id, thread_id, n_per_branch)
    return _branches_indexed(conn, msg_row_id, thread_id, n_per_branch)
