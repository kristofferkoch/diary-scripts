#!/usr/bin/env python3
"""
Semantic search over embedded mail.

    scripts/search_mail.py "examplefund utbetaling 2025"
    scripts/search_mail.py --k 20 --tier 1 "hans skolesvømming"
    scripts/search_mail.py --since 2025-01-01 "fakturaer fra strøm"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

import psycopg

PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
OLLAMA = os.environ.get("OLLAMA_URL", "http://gpu-host:11434").rstrip("/")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3:latest")


def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": MODEL, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["embedding"]


def vec_lit(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--tier", type=int, choices=[1, 2, 3])
    ap.add_argument("--since")
    ap.add_argument("--from", dest="from_addr")
    ap.add_argument("--not-from", dest="not_from", action="append", default=[],
                    help="Exclude messages whose From matches this substring (repeatable).")
    ap.add_argument("--minus", action="append", default=[],
                    help="Semantically subtract a phrase from the query (repeatable). "
                         "Example: --minus dogs.  Combine with --weight.")
    ap.add_argument("--weight", type=float, default=0.3,
                    help="How strongly to subtract --minus terms (default 0.3; 0.7+ breaks the query).")
    args = ap.parse_args(argv)

    q = embed(args.query)
    for m in args.minus:
        nv = embed(m)
        q = [a - args.weight * b for a, b in zip(q, nv)]
    qv = vec_lit(q)

    # Build WHERE clause from typed (LiteralString, value) pairs so the SQL
    # composition stays type-safe. No user input lands in the SQL fragments.
    from psycopg import sql as _sql
    clauses: list[_sql.SQL] = [_sql.SQL("TRUE")]
    params: list[object] = []
    if args.tier:
        clauses.append(_sql.SQL("m.tier = %s"))
        params.append(args.tier)
    if args.since:
        clauses.append(_sql.SQL("m.date >= %s"))
        params.append(args.since)
    if args.from_addr:
        clauses.append(_sql.SQL("m.from_addr ILIKE %s"))
        params.append(f"%{args.from_addr}%")
    for nf in args.not_from:
        clauses.append(_sql.SQL("m.from_addr NOT ILIKE %s"))
        params.append(f"%{nf}%")

    query = _sql.SQL("""
        SELECT m.date, m.from_addr, m.subject, m.message_id,
               c.embedding <=> %s::vector AS dist,
               left(c.text, 240) AS snippet
        FROM chunks c JOIN messages m ON m.id = c.message_id
        WHERE {where_clause}
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """).format(where_clause=_sql.SQL(" AND ").join(clauses))
    with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(query, [qv, *params, qv, args.k])
        for date, frm, subj, mid, dist, snip in cur:
            print(f"[{dist:.3f}] {date}  {frm}")
            print(f"        {subj}")
            print(f"        id:{mid}")
            print(f"        {snip.strip()[:220]!r}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
