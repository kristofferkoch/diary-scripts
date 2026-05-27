#!/usr/bin/env python3
"""pr_compose.py — per-mail PR composer (classifier-only first cut).

For each candidate mail (notmuch query or --since-cursor), runs a cheap
triage filter and the local MLX classifier on gpu-host:8080. Decisions
go to stdout. No notmuch tag changes, no git, no PR creation in this cut
— the next step is to add the writer tier + git harness.

Usage:
    uv run scripts/pr_compose.py --since-cursor
    uv run scripts/pr_compose.py --limit 20 'tag:inbox and date:1w..'
    uv run scripts/pr_compose.py id:<message-id>
    uv run scripts/pr_compose.py --verbose --limit 10 'from:astrid'

Env overrides:
    MLX_BASE          (default http://gpu-host:8080)
    PR_COMPOSE_MODEL  (default mlx-community/Qwen3.6-35B-A3B-4bit)
"""

from __future__ import annotations

import argparse
import email
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from email.policy import default as email_default
from pathlib import Path

import httpx

# Reuse existing infrastructure from mailshow.py (cursor handling,
# notmuch search, raw fetch, HTML→text). Per feedback-use-mail-tools:
# don't roll your own MIME extraction — extend mailshow.py if anything
# is missing.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from scripts.mailshow import (  # noqa: E402
    cursor_query,
    fetch_raw,
    html_to_text,
    load_cursor,
    notmuch_search_ids,
)


# ---------- Config

MLX_BASE = os.environ.get("MLX_BASE", "http://gpu-host:8080")
MODEL = os.environ.get(
    "PR_COMPOSE_MODEL",
    "mlx-community/Qwen3.6-35B-A3B-4bit",
)
BODY_MAX_CHARS = 4000  # cap the body sent to the classifier; receipts/notices are short

# Triage filter: notmuch tags signalling "noise" — skip the classifier entirely.
SKIP_TAGS = frozenset({
    "digest::list",
    "digest::list-protected",
    "digest::newsletter",
    "digest::transactional",
})

# Triage filter: sender substrings that always skip. GitHub-side mail
# is noise as a category for memorialisation — account/security notices,
# repo invites, PAT-created alerts, PR/issue notifications. The bot-mail
# routing to Botmail catches PR notifications structurally; this catches
# the rest (PAT alerts, invites, security pings) without burning a
# classifier call.
SKIP_SENDER_SUBSTRINGS = (
    "notifications@github.com",  # PR / issue / discussion notifications
    "noreply@github.com",        # account / security / repo-invite notifications
)

CLASSIFIER_SYSTEM = """\
Du klassifiserer e-poster fra Examples inboks. Svar BARE med JSON på \
formen {"significant": bool, "reason": "kort streng på norsk (max 20 ord)"}.

"significant" = verdt å lagre i et personlig minne-arkiv. Eksempler:
- store kjøp / kvitteringer over ~5000 NOK
- skole- / barnehage-meldinger med frister eller hendelser
- familiebegivenheter (kalenderhendelser, RSVP-er, bursdager)
- kontrakter / fakturaer / regnskapsdokumenter
- viktig korrespondanse fra håndverkere / leverandører på pågående prosjekter

IKKE significant:
- nyhetsbrev, reklame, kampanjer
- rutine-bekreftelser (innloggingsvarsler, leveransebekreftelser uten innhold)
- automatiske ping fra tjenester (GitHub releases, Heroku-status)
- små rutinekjøp (apotek, Joker, dagligvarer)

Vær konservativ: i tvil, returner false. Det er bedre å gå glipp av en \
melding enn å støye inboksen.
"""


# ---------- Data

@dataclass
class MailRow:
    msg_id: str
    subject: str
    sender: str
    body: str
    tags: list[str]


# ---------- Mail helpers

def get_tags(msg_id: str) -> list[str]:
    """notmuch tags for a single message id (one per line on stdout)."""
    out = subprocess.run(
        ["notmuch", "search", "--output=tags", f"id:{msg_id}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [t.strip() for t in out.splitlines() if t.strip()]


def extract_body(msg: email.message.EmailMessage) -> str:
    """Best-effort plain-text body. Prefer text/plain; fall back to text/html → text.
    Skips attachments and multipart parents."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if "attachment" in part.get("Content-Disposition", "").lower():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                plain_parts.append(part.get_content())
            except (KeyError, LookupError):
                payload = part.get_payload(decode=True) or b""
                plain_parts.append(payload.decode("utf-8", errors="replace"))
        elif ctype == "text/html":
            try:
                html_parts.append(part.get_content())
            except (KeyError, LookupError):
                payload = part.get_payload(decode=True) or b""
                html_parts.append(payload.decode("utf-8", errors="replace"))
    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        return html_to_text("\n".join(html_parts)).strip()
    return ""


def load_mail(msg_id: str) -> MailRow | None:
    """Fetch a MailRow for the given message_id. Returns None on fetch failure
    (printed to stderr so the caller can keep looping)."""
    try:
        raw = fetch_raw(msg_id)
    except Exception as e:
        print(f"!! fetch failed for id:{msg_id}: {e}", file=sys.stderr)
        return None
    msg = email.message_from_bytes(raw, policy=email_default)
    return MailRow(
        msg_id=msg_id,
        subject=str(msg.get("Subject", "")).strip(),
        sender=str(msg.get("From", "")).strip(),
        body=extract_body(msg),
        tags=get_tags(msg_id),
    )


# ---------- Triage

def triage_skip(row: MailRow) -> str | None:
    """Return a skip-reason if the mail bypasses the classifier, else None."""
    for tag in SKIP_TAGS:
        if tag in row.tags:
            return f"tag {tag}"
    sender_lower = row.sender.lower()
    for sub in SKIP_SENDER_SUBSTRINGS:
        if sub in sender_lower:
            return f"sender {sub}"
    return None


# ---------- Classifier

def _strip_code_fence(s: str) -> str:
    """Qwen sometimes wraps JSON in ```json ... ``` despite being told not to."""
    s = s.strip()
    if not s.startswith("```"):
        return s
    s = s.strip("`").strip()
    if s.lower().startswith("json"):
        s = s[4:].lstrip()
    return s


def classify(row: MailRow, client: httpx.Client) -> tuple[bool, str]:
    """Call MLX. Returns (significant, reason). Raises on transport / JSON error."""
    body = row.body[:BODY_MAX_CHARS]
    user_content = f"Subject: {row.subject}\nFrom: {row.sender}\n\n{body}"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = client.post(f"{MLX_BASE}/v1/chat/completions", json=payload, timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(_strip_code_fence(content))
    return bool(parsed["significant"]), str(parsed.get("reason", ""))[:200]


# ---------- Main

def resolve_query(args: argparse.Namespace) -> tuple[str, int | None]:
    """Build the notmuch query and limit from CLI args."""
    if args.since_cursor:
        cursor = load_cursor()
        extra = " ".join(args.query) if args.query else None
        return cursor_query(cursor, extra), args.limit
    if args.query:
        return " ".join(args.query), args.limit
    return "tag:inbox and date:1w..", args.limit or 50


def format_line(idx: int, total: int, tag: str, row: MailRow, reason: str = "") -> str:
    sender = (row.sender or "").replace("\n", " ")[:40]
    subject = (row.subject or "").replace("\n", " ")[:80]
    head = f"[{idx:3}/{total}] {tag}  {sender:<40}  {subject}"
    if reason:
        head += f"\n             reason: {reason}"
    return head


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since-cursor", action="store_true",
                    help="Start from memory/mail-state.json:last_successful_run")
    ap.add_argument("--limit", type=int, help="Cap number of mails")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print first 200 chars of body for each classified mail")
    ap.add_argument("query", nargs="*",
                    help="notmuch query (default: tag:inbox and date:1w..)")
    args = ap.parse_args(argv)

    query, limit = resolve_query(args)
    ids = notmuch_search_ids(query, limit)
    if not ids:
        print(f"# no mail matched: {query}", file=sys.stderr)
        return 0
    print(f"# {len(ids)} mail(s) to triage  (query: {query})", file=sys.stderr)

    n_sig = n_skip_triage = n_classified = n_error = 0
    with httpx.Client() as client:
        for i, msg_id in enumerate(ids, 1):
            row = load_mail(msg_id)
            if row is None:
                n_error += 1
                continue

            t_skip = triage_skip(row)
            if t_skip:
                n_skip_triage += 1
                print(format_line(i, len(ids), "SKIP  ", row, t_skip))
                continue

            try:
                sig, reason = classify(row, client)
            except Exception as e:
                n_error += 1
                print(f"[{i:3}/{len(ids)}] ERROR  id:{msg_id}: {e}", file=sys.stderr)
                continue

            n_classified += 1
            if sig:
                n_sig += 1
            label = "SIG   " if sig else "skip  "
            print(format_line(i, len(ids), label, row, reason))
            if args.verbose and row.body:
                body_snip = row.body[:200].replace("\n", " ")
                print(f"             body:   {body_snip}…")

    print(
        f"\n# done: {len(ids)} candidates, "
        f"{n_skip_triage} triage-skipped, "
        f"{n_classified} classified, "
        f"{n_sig} significant, "
        f"{n_error} error(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
