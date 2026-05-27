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


_thread_cache: dict[str, str | None] = {}


def thread_of(msg_id: str) -> str | None:
    """notmuch thread id for a message (without the 'thread:' prefix), or None
    if the message isn't indexed. Cached per run."""
    if msg_id in _thread_cache:
        return _thread_cache[msg_id]
    out = subprocess.run(
        ["notmuch", "search", "--output=threads", f"id:{msg_id}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        _thread_cache[msg_id] = None
        return None
    first = out.splitlines()[0]
    tid = first.removeprefix("thread:") if first.startswith("thread:") else first
    _thread_cache[msg_id] = tid
    return tid


def apply_tags(target: str, tags: list[str]) -> None:
    """Apply +/- tag list to a notmuch query target (e.g. 'id:foo', 'thread:bar')."""
    if not tags:
        return
    subprocess.run(["notmuch", "tag"] + tags + ["--", target], check=True)


def dedupe_by_thread(ids: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Group `ids` by thread; keep only the first message encountered per
    thread (notmuch default sort is newest-first, so "first encountered" is
    the latest message — the one with the most context). Returns
    (kept_ids, {thread_id: [duplicate_msg_ids_dropped]})."""
    seen: set[str] = set()
    kept: list[str] = []
    dupes: dict[str, list[str]] = {}
    for msg_id in ids:
        tid = thread_of(msg_id)
        if tid and tid in seen:
            dupes.setdefault(tid, []).append(msg_id)
        else:
            if tid:
                seen.add(tid)
            kept.append(msg_id)
    return kept, dupes


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
    """Build the notmuch query and limit from CLI args.

    Always excludes mail we've already decided on:
      - tag:pr::triaged (per-message decision was made)
      - thread:"{tag:pr::significant}" (the whole thread was deemed significant
        — replies on the same thread shouldn't trigger a new PR)

    To re-classify a tagged mail, remove the tag first: `notmuch tag -pr::triaged …`
    """
    if args.since_cursor:
        cursor = load_cursor()
        extra = " ".join(args.query) if args.query else None
        base = cursor_query(cursor, extra)
        limit = args.limit
    elif args.query:
        base = " ".join(args.query)
        limit = args.limit
    else:
        base = "tag:inbox and date:1w.."
        limit = args.limit or 50

    base = f"({base}) and not (tag:pr::triaged or thread:\"{{tag:pr::significant}}\")"
    return base, limit


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
    ap.add_argument("--apply", action="store_true",
                    help="Persist decisions as notmuch tags (pr::triaged + "
                         "pr::significant|pr::skip). Default is dry-run.")
    ap.add_argument("query", nargs="*",
                    help="notmuch query (default: tag:inbox and date:1w..)")
    args = ap.parse_args(argv)

    query, limit = resolve_query(args)
    ids = notmuch_search_ids(query, limit)
    if not ids:
        print(f"# no mail matched: {query}", file=sys.stderr)
        return 0

    kept_ids, dupes = dedupe_by_thread(ids)
    n_thread_dup = sum(len(v) for v in dupes.values())
    mode = "APPLY" if args.apply else "dry-run"
    print(
        f"# [{mode}] {len(ids)} candidate(s); "
        f"{n_thread_dup} thread-dup → {len(kept_ids)} to classify  "
        f"(query: {query})",
        file=sys.stderr,
    )

    n_sig = n_skip_triage = n_classified = n_error = 0
    with httpx.Client() as client:
        for i, msg_id in enumerate(kept_ids, 1):
            row = load_mail(msg_id)
            if row is None:
                n_error += 1
                continue

            t_skip = triage_skip(row)
            if t_skip:
                n_skip_triage += 1
                print(format_line(i, len(kept_ids), "SKIP  ", row, t_skip))
                if args.apply:
                    # Triage decisions are per-message: a github noreply in a
                    # thread that later gets a human reply shouldn't taint the
                    # whole thread. The future human message will get its own
                    # classifier pass.
                    apply_tags(f"id:{msg_id}", ["+pr::triaged", "+pr::skip"])
                continue

            try:
                sig, reason = classify(row, client)
            except Exception as e:
                n_error += 1
                print(f"[{i:3}/{len(kept_ids)}] ERROR  id:{msg_id}: {e}",
                      file=sys.stderr)
                continue

            n_classified += 1
            if sig:
                n_sig += 1
            label = "SIG   " if sig else "skip  "
            print(format_line(i, len(kept_ids), label, row, reason))
            if args.verbose and row.body:
                body_snip = row.body[:200].replace("\n", " ")
                print(f"             body:   {body_snip}…")

            if args.apply:
                # Classifier decisions go on the THREAD — once a thread is
                # decided, replies on the same thread shouldn't trigger
                # another PR. dupes already in this batch get tagged for
                # free as part of the thread.
                tid = thread_of(msg_id)
                target = f"thread:{tid}" if tid else f"id:{msg_id}"
                decision = "+pr::significant" if sig else "+pr::skip"
                apply_tags(target, ["+pr::triaged", decision])

    print(
        f"\n# done: {len(ids)} candidate(s) "
        f"({n_thread_dup} thread-dup, {n_skip_triage} triage-skip, "
        f"{n_classified} classified, {n_sig} sig, {n_error} error(s))"
        + ("  [TAGS APPLIED]" if args.apply else "  [dry-run, no tags written]"),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
