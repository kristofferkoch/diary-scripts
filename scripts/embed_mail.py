#!/usr/bin/env python3
"""
Embed mail from notmuch into Postgres + pgvector using a local Ollama model.

Tiers (set with --tier or run all three sequentially):
    1  date:1y..                                          (~6.3k msgs)
    2  thread:"{from:<me>}"                               (~733 msgs)
    3  from:<addr1> or from:<addr2> or ...                (~9k msgs)
       (addresses harvested from your past recipients)

Pipeline per message:
    notmuch raw → email.parse → prefer text/plain → html_to_text fallback
    → quote/sig strip (talon if installed, else heuristic)
    → chunk (≈512 tokens, ≈64 overlap)
    → POST /api/embeddings to Ollama
    → INSERT into messages + chunks

Re-runs are idempotent: messages with an existing message_id are skipped.

Env:
    PG_DSN              postgres://user@host/mailvec   (default: dbname=mailvec)
    OLLAMA_URL          http://localhost:11434          (default)
    EMBED_MODEL         bge-m3                          (default; 1024d)
    ME_ADDRS            comma-separated; default: user@example.com

Examples:
    scripts/embed_mail.py --tier 1
    scripts/embed_mail.py --tier 2
    scripts/embed_mail.py --tier 3 --limit 100         # dry-ish trial
    scripts/embed_mail.py --all
"""
from __future__ import annotations

import argparse
import email
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from email.policy import default as email_default

import psycopg
from psycopg.types.json import Json

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
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")

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
            return _REPLY_PARSER.read(text).latest_reply.strip()
        except Exception:
            pass
    t = _ON_WROTE.sub("", text)
    t = _FROM_HEADER.sub("", t)
    t = _QUOTE_LINE.sub("", t)
    t = _SIG_DASH.sub("", t)
    return t.strip()

def parse_message(raw: bytes) -> tuple[dict, str]:
    msg = email.message_from_bytes(raw, policy=email_default)
    headers = {
        "date":    msg.get("Date", ""),
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

# ---------- chunking ----------

# Rough: 1 token ~ 4 chars for English/Norwegian mixed. 512 tok ~ 2048 chars.
CHUNK_CHARS = 2048
OVERLAP = 256

def chunk_text(text: str) -> list[str]:
    text = text.strip()
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

# ---------- embedding (Ollama HTTP) ----------

def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": MODEL, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["embedding"]

def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

# ---------- tier queries ----------

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
    raise ValueError(tier)

# ---------- main loop ----------

def parse_date(s: str) -> str | None:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt.isoformat() if dt else None
    except Exception:
        return None

def process(conn, tier: int, limit: int | None, verbose: bool) -> None:
    q = tier_query(tier)
    if verbose:
        print(f"[tier {tier}] query: {q[:200]}{'…' if len(q)>200 else ''}",
              file=sys.stderr)
    ids = nm_search_ids(q, limit)
    print(f"[tier {tier}] {len(ids)} candidate messages", file=sys.stderr)

    done = skipped = failed = 0
    t0 = time.time()
    for n, mid in enumerate(ids, 1):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM messages WHERE message_id = %s", (mid,))
            if cur.fetchone():
                skipped += 1
                continue
        try:
            raw = nm_raw(mid)
            hdr, body = parse_message(raw)
            if len(body) < 40:        # too short to be worth embedding
                skipped += 1
                continue
            chunks = chunk_text(body)
            if not chunks:
                skipped += 1
                continue
            vecs = [embed(c) for c in chunks]
            tid = nm_thread_id(mid)
            with conn.cursor() as cur, conn.transaction():
                cur.execute("""
                    INSERT INTO messages
                      (message_id, date, from_addr, to_addrs, subject,
                       thread_id, tier, body_chars)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (mid, parse_date(hdr["date"]), hdr["from"], hdr["to"],
                      hdr["subject"], tid, tier, len(body)))
                row_id = cur.fetchone()[0]
                for i, (c, v) in enumerate(zip(chunks, vecs)):
                    cur.execute(
                        "INSERT INTO chunks (message_id, chunk_idx, text, embedding) "
                        "VALUES (%s,%s,%s,%s::vector)",
                        (row_id, i, c, vec_literal(v)))
            done += 1
        except Exception as e:
            failed += 1
            print(f"  !! {mid}: {e}", file=sys.stderr)
        if verbose and n % 50 == 0:
            rate = n / (time.time() - t0)
            eta = (len(ids) - n) / rate if rate else 0
            print(f"  {n}/{len(ids)}  {rate:.1f} msg/s  eta {eta/60:.1f} min  "
                  f"done={done} skipped={skipped} failed={failed}",
                  file=sys.stderr)
    print(f"[tier {tier}] done={done} skipped={skipped} failed={failed} "
          f"elapsed={(time.time()-t0)/60:.1f} min", file=sys.stderr)

def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", type=int, choices=[1, 2, 3])
    ap.add_argument("--all", action="store_true", help="run tiers 2,1,3 in order")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not (args.tier or args.all):
        ap.error("pass --tier N or --all")

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        tiers = [2, 1, 3] if args.all else [args.tier]
        for t in tiers:
            process(conn, t, args.limit, verbose=not args.quiet)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
