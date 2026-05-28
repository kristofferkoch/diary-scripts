#!/usr/bin/env python3
"""pr_compose.py — per-mail PR composer.

Two phases, both invoked in a single run:

  1. CLASSIFY new candidate mail (notmuch query or --since-cursor). Triage
     filter + MLX classifier on gpu-host:8080. Tags significant threads
     with `pr::significant`, non-significant with `pr::skip`, all with
     `pr::triaged` (so future runs skip them). Dry-run unless --apply.

  2. FILE PRs for pr::significant threads not yet pr::filed. Re-calls the
     model in writer mode, produces a memory-section draft, creates a
     feature branch via `git worktree`, commits as exampleuser-bot,
     pushes via embedded-PAT URL, opens a PR via `gh pr create`. Dry-run
     unless --apply.

Common invocations:
    # Just see what would happen on recent mail:
    uv run scripts/pr_compose.py --since-cursor

    # Classify and tag (no PRs):
    uv run scripts/pr_compose.py --apply --since-cursor

    # Full pipeline — classify, tag, file PRs:
    uv run scripts/pr_compose.py --apply --file-prs --since-cursor

    # File PRs only (skip classify), capped at 2:
    uv run scripts/pr_compose.py --file-prs --apply --limit-prs 2 'id:none'

Env overrides:
    MLX_BASE                      (default http://gpu-host:8080)
    PR_COMPOSE_CLASSIFIER_MODEL   (default Qwen3.6-35B-A3B-4bit-DWQ)
    PR_COMPOSE_WRITER_MODEL       (default Qwen3.6-35B-A3B-4bit-DWQ — bump
                                   to -6bit or -8bit-DWQ for better tool
                                   routing + prose quality at writer time)
"""

from __future__ import annotations

import argparse
import email
import json
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
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

# Two-tier model setup. The classifier runs on every new mail — pick speed.
# The writer only runs on the ~5% deemed significant — pick quality. Quants
# above 4-bit help the writer's tool-call routing + Norwegian prose; the
# classifier's binary "significant?" decision doesn't need extra bits.
# `4bit-DWQ` avoids the standard-4-bit tool-use degradation (mlx-lm #1011).
# Both default to the same DWQ build if you only want to download one.
CLASSIFIER_MODEL = os.environ.get(
    "PR_COMPOSE_CLASSIFIER_MODEL",
    "mlx-community/Qwen3.6-35B-A3B-4bit-DWQ",
)
WRITER_MODEL = os.environ.get(
    "PR_COMPOSE_WRITER_MODEL",
    "mlx-community/Qwen3.6-35B-A3B-4bit-DWQ",
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

DIARY_REPO = "exampleuser/diary"
BOT_NAME = "exampleuser-bot"
BOT_EMAIL = "bot@example.com"
BOT_PASS_ENTRY = "github/mailbot-pat"

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


# Typographic canonicalization for LLM inputs. Qwen3.6 thinking-mode has
# been observed to lock into an infinite "Wait, is it 'Spiker`n' or
# 'Spiker'n'?" loop when the source mail contains non-canonical apostrophes
# (backtick, acute, smart-quotes). Folding to a single canonical form
# removes the ambiguity the model can't commit on. NFKC handles composed
# vs decomposed forms first; the translate table covers char swaps that
# NFKC leaves alone (backtick is not an apostrophe variant in unicode).
_LLM_CHAR_MAP = str.maketrans({
    "`":      "'",  # backtick → straight apostrophe (the Spiker'n trigger)
    "´": "'",  # acute accent
    "‘": "'",  # left single quote
    "’": "'",  # right single quote (Norwegian smart quote)
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "–": "-",  # en dash → hyphen-minus
    "—": "-",  # em dash → hyphen-minus
    " ": " ", # non-breaking space → space
})


def canonicalize_for_llm(text: str) -> str:
    """Normalize typographic variants that confuse LLMs into commitment loops.

    Targets a Qwen3.6 thinking-mode failure where input with non-canonical
    apostrophes (backtick `, acute ´, smart quotes ' ') causes the model to
    fixate on "which form is correct?" and loop indefinitely in reasoning.
    Folding to a single canonical form (straight ASCII apostrophe / quote /
    hyphen) removes the ambiguity. Applied at the writer input boundary.
    """
    import unicodedata
    return unicodedata.normalize("NFKC", text).translate(_LLM_CHAR_MAP)


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
        "model": CLASSIFIER_MODEL,
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


# ---------- Writer

WRITER_SYSTEM = """\
You draft a section for Example's daily memory file (`memory/YYYY-MM-DD.md`)
based on an email he received. **The drafted content (pr_title, headings,
body, candidate titles) must be in Norwegian.** JSON keys are English.

Respond with ONLY this JSON (no prose, no code fences):

{
  "pr_title": "<kort norsk tittel, max 70 tegn>",
  "branch_keyword": "<short-kebab-case-ascii-slug, 2-5 words>",
  "memory_heading": "<norsk overskrift uten '## ' prefiks>",
  "memory_body": "<markdown-innhold på norsk, 2-6 setninger; bruk kulepunkter for handlingspunkter>",
  "calendar_candidates": [
    {
      "date": "YYYY-MM-DD or YYYY-MM-DD – YYYY-MM-DD",
      "title": "<kort norsk event-tittel>",
      "evidence": "<kort sitat eller henvisning til e-post-kontekst>"
    }
  ]
}

Requirements:
- `pr_title` action-focused. Examples (Norwegian):
  - "Streik-varsel fra Eksempeldalen barnehage"
  - "Tilbud fra Eksempel Elektriske — oppussings-strøm"
- `branch_keyword`: ASCII a-z, 0-9, hyphens only.
- `memory_heading` becomes `## <heading>` in the daily note.
- `memory_body` covers the key facts and follow-ups — dates, deadlines,
  amounts, contact people — so Example can recall the context later.
- `calendar_candidates`: every concrete date or deadline mentioned in the
  email, even if you suspect it's already on the calendar. Use ISO dates;
  resolve relative references ("neste fredag", "uke 24") to absolute dates.
  Empty list `[]` if the email has no dates. **You don't need to check
  whether candidates are already on the calendar — Python does that
  deduplication after you respond.**
"""


# ---------- Tools the writer can call

def _tool_get_calendar_events(start_date: str, end_date: str,
                              repo_root: Path | None = None) -> str:
    """Tool executor. JSON list of events in [start, end] from CALENDAR.md and
    CALENDAR-PAST.md (one-off events only — the recurring sections don't have
    parseable per-occurrence dates yet)."""
    import datetime as _dt
    from scripts.retire_calendar import parse as _parse_calendar, event_dates
    try:
        start = _dt.date.fromisoformat(start_date)
        end = _dt.date.fromisoformat(end_date)
    except ValueError as e:
        return json.dumps({"error": f"invalid date: {e}"})
    if start > end:
        return json.dumps({"error": "start_date after end_date"})

    root = repo_root or Path(__file__).resolve().parent.parent
    events: list[dict[str, str]] = []
    for fname in ("CALENDAR.md", "CALENDAR-PAST.md"):
        path = root / fname
        if not path.exists():
            continue
        try:
            doc = _parse_calendar(path.read_text())
        except Exception as e:  # pragma: no cover — guard against parser drift
            events.append({"error": f"failed to parse {fname}: {e}"})
            continue
        for section in doc.sections:
            for line in section.lines:
                dates = event_dates(line)
                if dates is None:
                    continue
                ev_start, ev_end = dates
                if ev_end < start or ev_start > end:
                    continue
                events.append({
                    "start": ev_start.isoformat(),
                    "end": ev_end.isoformat(),
                    "line": line.strip(),
                    "source": fname,
                })
    return json.dumps({"range": [start_date, end_date], "events": events},
                      ensure_ascii=False)


_SLUG_FOLDS = str.maketrans({
    "æ": "ae", "ø": "o", "å": "a",
    "Æ": "ae", "Ø": "o", "Å": "a",
})


def _slugify(s: str) -> str:
    """ASCII kebab-case slug for branch names. Norwegian chars folded to their
    conventional Latin form (æ→ae, ø→o, å→a); other diacritics stripped via
    NFKD (é→e, ñ→n, …) so foreign names slug cleanly too."""
    s = s.lower().translate(_SLUG_FOLDS)
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:50]


def _validate_writer_output(d: dict) -> dict:
    """Verify required str fields + optional `calendar_candidates` list, normalise.
    Returns a dict with the str fields and a `calendar_candidates: list[dict]`
    (possibly empty)."""
    required = ("pr_title", "branch_keyword", "memory_heading", "memory_body")
    for k in required:
        if k not in d or not isinstance(d[k], str):
            raise ValueError(f"writer output missing/invalid {k!r}: {d!r}")
    out: dict = {k: d[k] for k in required}
    out["branch_keyword"] = _slugify(out["branch_keyword"]) or "mail"
    out["pr_title"] = out["pr_title"][:70].strip()
    out["memory_heading"] = out["memory_heading"].lstrip("# ").strip()
    out["memory_body"] = out["memory_body"].rstrip() + "\n"

    candidates = d.get("calendar_candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    validated: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        validated.append({
            "date":   str(c.get("date", "")).strip(),
            "title":  str(c.get("title", "")).strip(),
            "evidence": str(c.get("evidence", "")).strip(),
            "already_in_calendar": bool(c.get("already_in_calendar", False)),
        })
    out["calendar_candidates"] = validated
    return out


def compose_pr(row: MailRow, client: httpx.Client) -> dict:
    """Call MLX writer for one mail. Returns validated writer dict with
    `calendar_candidates` enriched by deterministic Python-side calendar
    lookup (model only proposes candidates; Python sets
    `already_in_calendar`)."""
    body = row.body[:BODY_MAX_CHARS]
    user_content = f"Subject: {row.subject}\nFrom: {row.sender}\n\n{body}"
    payload = {
        "model": WRITER_MODEL,
        "messages": [
            {"role": "system", "content": WRITER_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = client.post(f"{MLX_BASE}/v1/chat/completions", json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    out = _validate_writer_output(json.loads(_strip_code_fence(content)))
    out["calendar_candidates"] = _verify_candidates(out["calendar_candidates"])
    return out


def _verify_candidates(candidates: list[dict]) -> list[dict]:
    """For each candidate, look up the date(s) in CALENDAR.md and set
    `already_in_calendar` based on whether any existing event overlaps the
    candidate's date or date range. Deterministic alternative to having
    the model do this via tool calls (which is unreliable in MLX as of
    May 2026 — see git history for the agent-loop attempt)."""
    import datetime as _dt
    if not candidates:
        return []
    enriched: list[dict] = []
    for c in candidates:
        date_str = c.get("date", "").strip()
        start, end = _parse_candidate_date(date_str)
        if start is None or end is None:
            # Can't parse — pass it through with already_in_calendar=False
            # so the human reviewer sees the candidate and can act on it.
            enriched.append({**c, "already_in_calendar": False})
            continue
        # Tight range matching candidate's own span — no need to look wider.
        result = json.loads(_tool_get_calendar_events(
            start.isoformat(), end.isoformat(),
        ))
        events = result.get("events", [])
        # Title-fuzzy match is brittle; date overlap is enough signal that
        # the human reviewer should check whether it's the same event.
        already = len(events) > 0
        enriched.append({**c, "already_in_calendar": already})
    return enriched


_DATE_SINGLE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_DATE_SPAN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[–-]\s*(\d{4}-\d{2}-\d{2})$")


def _parse_candidate_date(s: str):
    """Parse the model's `date` field. Accepts a single ISO date or a span
    with an en-dash or hyphen separator. Returns (start, end) as
    datetime.date pairs, or (None, None) if unparseable."""
    import datetime as _dt
    m = _DATE_SINGLE.match(s)
    if m:
        d = _dt.date.fromisoformat(m.group(1))
        return d, d
    m = _DATE_SPAN.match(s)
    if m:
        return (_dt.date.fromisoformat(m.group(1)),
                _dt.date.fromisoformat(m.group(2)))
    return None, None


def make_branch_name(keyword: str, today: date | None = None,
                     rng: random.Random | None = None) -> str:
    """Construct `mail/<YYYY-MM-DD>-<keyword>-<rand4>` for the feature branch."""
    today = today or date.today()
    rng = rng or random.Random()
    suffix = "".join(rng.choices(string.ascii_lowercase + string.digits, k=4))
    return f"mail/{today.isoformat()}-{keyword}-{suffix}"


def _render_pr_body(row: MailRow, thread_id: str,
                    calendar_candidates: list[dict] | None = None) -> str:
    body_clip = row.body[:4000]
    truncated_note = " (truncated; full mail via `notmuch show`)" if len(row.body) > 4000 else ""

    candidates_section = ""
    if calendar_candidates:
        lines = ["", "## Calendar candidates", "",
                 "_⚠ overlap = same-day event exists in CALENDAR.md but may be "
                 "unrelated. Reviewer disambiguates._", ""]
        for c in calendar_candidates:
            marker = "⚠ overlap" if c.get("already_in_calendar") else "⊕ new"
            lines.append(
                f"- **{c.get('date', '')}** — {c.get('title', '')}  "
                f"_({marker})_  \n  evidence: {c.get('evidence', '')}"
            )
        candidates_section = "\n".join(lines) + "\n"

    return (
        f"**Source mail:** `thread:{thread_id}`  \n"
        f"**Subject:** {row.subject}  \n"
        f"**From:** {row.sender}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"Auto-generated by `scripts/pr_compose.py` from a notmuch-classified "
        f"significant mail. Review the proposed memory section, edit if needed, "
        f"and merge. To rollback: "
        f"`notmuch tag -pr::filed -pr::significant +pr::skip thread:{thread_id}` "
        f"then close the PR.\n"
        f"{candidates_section}"
        f"\n"
        f"<details>\n"
        f"<summary>Rendered mail body (first 4000 chars{truncated_note})</summary>\n"
        f"\n"
        f"~~~\n"
        f"{body_clip}\n"
        f"~~~\n"
        f"\n"
        f"</details>\n"
    )


# ---------- Git / PR harness

def _bot_pat() -> str:
    """Read the bot's PAT from `pass`. Returns the first line only."""
    out = subprocess.run(
        ["pass", "show", BOT_PASS_ENTRY],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.split("\n", 1)[0].strip()


def _latest_msg_in_thread(thread_id: str) -> str | None:
    """notmuch newest-first; return the newest message id in the thread."""
    out = subprocess.run(
        ["notmuch", "search", "--sort=newest-first",
         "--output=messages", f"thread:{thread_id}"],
        check=True, capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            return line.removeprefix("id:")
    return None


def file_pr_for_message(
    row: MailRow, thread_id: str, writer: dict[str, str],
    *, apply: bool, today: date | None = None,
    repo_root: Path | None = None,
) -> str | None:
    """File one PR. Returns PR URL on apply, None in dry-run.

    Side effects on apply: creates a feature branch via `git worktree`, writes
    the proposed memory section, commits as the bot, pushes via embedded-PAT
    URL, opens a PR via `gh pr create`. Worktree is removed in `finally`."""
    today = today or date.today()
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    branch = make_branch_name(writer["branch_keyword"], today)

    candidates = writer.get("calendar_candidates") or []
    cand_summary = ""
    if candidates:
        cand_lines = []
        for c in candidates:
            # ⚠ means "date overlaps an existing event" — not necessarily the
            # same event (CALENDAR.md may have unrelated fotballkamp etc. on
            # the same date). Reviewer disambiguates.
            marker = "⚠ overlap" if c.get("already_in_calendar") else "⊕ new   "
            cand_lines.append(f"             cal:    [{marker}] {c.get('date', '')} — {c.get('title', '')}")
        cand_summary = "\n" + "\n".join(cand_lines)

    print(
        f"  → branch: {branch}\n"
        f"     title:  {writer['pr_title']}\n"
        f"     edit:   memory/{today.isoformat()}.md  +## {writer['memory_heading']}\n"
        f"     body:   {writer['memory_body'].strip()[:300]}"
        f"{cand_summary}",
        file=sys.stderr,
    )

    if not apply:
        return None

    pat = _bot_pat()
    worktree_path = Path(tempfile.mkdtemp(prefix="prcomp-"))
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "add", "-b", branch,
             str(worktree_path), "master"],
            check=True, capture_output=True, text=True,
        )

        memory_rel = Path("memory") / f"{today.isoformat()}.md"
        memory_path = worktree_path / memory_rel
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        existing = memory_path.read_text() if memory_path.exists() else f"# {today.isoformat()}\n"
        new_content = (
            existing.rstrip()
            + f"\n\n## {writer['memory_heading']}\n\n{writer['memory_body']}"
        )
        memory_path.write_text(new_content)

        subprocess.run(
            ["git", "-C", str(worktree_path), "add", str(memory_rel)],
            check=True,
        )
        commit_msg = (
            f"MEM: {writer['pr_title']}\n\n"
            f"Auto-generert av scripts/pr_compose.py fra epost:\n"
            f"  thread:{thread_id}\n"
            f"  from:    {row.sender}\n"
            f"  subject: {row.subject}\n\n"
            f"Co-Authored-By: {BOT_NAME} <{BOT_EMAIL}>\n"
        )
        subprocess.run(
            ["git", "-C", str(worktree_path),
             "-c", f"user.name={BOT_NAME}",
             "-c", f"user.email={BOT_EMAIL}",
             "commit", "-m", commit_msg],
            check=True,
        )

        push_url = f"https://{BOT_NAME}:{pat}@github.com/{DIARY_REPO}.git"
        subprocess.run(
            ["git", "-C", str(worktree_path), "push",
             push_url, f"HEAD:refs/heads/{branch}"],
            check=True, capture_output=True, text=True,
        )

        env = {**os.environ, "GH_TOKEN": pat}
        result = subprocess.run(
            ["gh", "pr", "create",
             "--repo", DIARY_REPO,
             "--base", "master",
             "--head", branch,
             "--title", f"MEM: {writer['pr_title']}",
             "--body", _render_pr_body(row, thread_id, candidates)],
            env=env, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    finally:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove",
             "--force", str(worktree_path)],
            check=False, capture_output=True,
        )


def file_prs_for_significant_threads(args: argparse.Namespace,
                                      client: httpx.Client) -> tuple[int, int]:
    """Find pr::significant threads without pr::filed; write + open PRs.

    Returns (n_filed, n_error)."""
    query = "tag:pr::significant and not tag:pr::filed"
    out = subprocess.run(
        ["notmuch", "search", "--output=threads", query],
        check=True, capture_output=True, text=True,
    ).stdout
    thread_ids = [
        line.strip().removeprefix("thread:")
        for line in out.splitlines()
        if line.strip().startswith("thread:")
    ]

    if not thread_ids:
        print("# no significant threads to file", file=sys.stderr)
        return 0, 0

    if args.limit_prs and len(thread_ids) > args.limit_prs:
        print(f"# {len(thread_ids)} sig thread(s); capped at {args.limit_prs}",
              file=sys.stderr)
        thread_ids = thread_ids[:args.limit_prs]

    mode = "APPLY" if args.apply else "dry-run"
    print(f"\n# [{mode}] filing PRs for {len(thread_ids)} thread(s)",
          file=sys.stderr)

    n_filed = n_error = 0
    for i, tid in enumerate(thread_ids, 1):
        msg_id = _latest_msg_in_thread(tid)
        if not msg_id:
            print(f"!! thread:{tid} has no resolvable message", file=sys.stderr)
            n_error += 1
            continue
        row = load_mail(msg_id)
        if row is None:
            n_error += 1
            continue
        print(f"\n[{i}/{len(thread_ids)}] thread:{tid}  {row.sender[:40]}  {row.subject[:60]}",
              file=sys.stderr)
        try:
            writer = compose_pr(row, client)
        except Exception as e:
            print(f"!! writer failed: {e}", file=sys.stderr)
            n_error += 1
            continue
        try:
            pr_url = file_pr_for_message(row, tid, writer, apply=args.apply)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr if isinstance(e.stderr, str) else ""
            print(f"!! git/gh failed: {e}\n    {stderr[:300]}", file=sys.stderr)
            n_error += 1
            continue
        if args.apply and pr_url:
            apply_tags(f"thread:{tid}", ["+pr::filed"])
            print(f"  ✓ {pr_url}")
            n_filed += 1

    return n_filed, n_error


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
                    help="Persist decisions and (with --file-prs) actually "
                         "open PRs. Default is dry-run everywhere.")
    ap.add_argument("--file-prs", action="store_true",
                    help="After classifying, file PRs for pr::significant "
                         "threads not yet pr::filed.")
    ap.add_argument("--limit-prs", type=int, default=None,
                    help="Cap number of PRs filed per run (safety)")
    ap.add_argument("query", nargs="*",
                    help="notmuch query (default: tag:inbox and date:1w..)")
    args = ap.parse_args(argv)

    query, limit = resolve_query(args)
    ids = notmuch_search_ids(query, limit)
    if not ids:
        print(f"# no classifier candidates (query: {query})", file=sys.stderr)
        if not args.file_prs:
            return 0
        # Fall through to file-prs phase — there may still be already-tagged
        # significant threads to process even though no new mail to classify.
        kept_ids, dupes = [], {}
    else:
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
        f"\n# classify done: {len(ids)} candidate(s) "
        f"({n_thread_dup} thread-dup, {n_skip_triage} triage-skip, "
        f"{n_classified} classified, {n_sig} sig, {n_error} error(s))"
        + ("  [TAGS APPLIED]" if args.apply else "  [dry-run, no tags written]"),
        file=sys.stderr,
    )

    if args.file_prs:
        with httpx.Client() as client:
            n_filed, n_pr_err = file_prs_for_significant_threads(args, client)
        print(
            f"\n# file-prs done: {n_filed} PR(s) "
            + ("filed" if args.apply else "(dry-run)")
            + f", {n_pr_err} error(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
