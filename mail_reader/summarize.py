"""Get-or-create LLM summaries cached in the `summaries` table.

Hits Ollama on gpu-host (`OLLAMA_URL`, default `http://gpu-host:11434`)
with `qwen3.6:35b-a3b` by default. One sentence, mail's primary language.

The body text is rebuilt from the `chunks` table — we already have it
chunked and de-quoted in there, so we don't re-fetch and re-parse from
notmuch. If the message isn't in `chunks` (e.g. an inbox mail that
hasn't been embedded yet), returns None.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import psycopg

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://gpu-host:11434").rstrip("/")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen3.6:35b-a3b")

_SYSTEM = (
    "Du oppsummerer e-poster på én setning, maks 140 tegn. "
    "Skriv på samme språk som e-posten (norsk eller engelsk). "
    "Beskriv hva e-posten HANDLER OM og hva (om noe) den BER mottakeren om. "
    "Ikke gjenta avsender eller emnefelt. "
    "Ingen markdown, ingen anførselstegn — bare den ene setningen."
)


def _ollama_chat(subject: str, from_addr: str, body: str) -> str:
    prompt = f"Avsender: {from_addr}\nEmne: {subject}\n\n{body[:6000]}"
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": SUMMARY_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            # qwen3.6:35b-a3b cold-loads in ~3 min on gpu-host; once
            # resident, inference is sub-second. Hold the model in VRAM
            # between requests so the second card+ in a tankekart are fast.
            "keep_alive": "30m",
            "options": {"temperature": 0.2},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return (data.get("message", {}).get("content") or "").strip()


def get_or_create_summary(conn: psycopg.Connection,
                          notmuch_msg_id: str) -> str | None:
    """Return cached summary for the configured model, generating it if
    missing. Returns None if the message isn't in our DB yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.short
            FROM summaries s
            JOIN messages m ON m.id = s.message_id
            WHERE m.message_id = %s AND s.model = %s
            LIMIT 1
            """,
            (notmuch_msg_id, SUMMARY_MODEL),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]

        cur.execute(
            "SELECT id, subject, from_addr FROM messages WHERE message_id = %s",
            (notmuch_msg_id,),
        )
        meta = cur.fetchone()
        if meta is None:
            return None
        msg_row_id, subject, from_addr = meta

        cur.execute(
            "SELECT text FROM chunks "
            "WHERE message_id = %s AND attachment_id IS NULL "
            "ORDER BY chunk_idx ASC",
            (msg_row_id,),
        )
        body = "\n\n".join(r[0] for r in cur.fetchall())
        if not body:
            return None

        try:
            summary = _ollama_chat(subject or "", from_addr or "", body)
        except (urllib.error.URLError, TimeoutError) as e:
            return f"(summarisering feilet: {e})"
        if not summary:
            return None

        cur.execute(
            """
            INSERT INTO summaries (message_id, model, short)
            VALUES (%s, %s, %s)
            ON CONFLICT (message_id, model) DO UPDATE
              SET short = EXCLUDED.short,
                  generated_at = now()
            """,
            (msg_row_id, SUMMARY_MODEL, summary),
        )
        conn.commit()
    return summary
