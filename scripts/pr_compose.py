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
                                  — classifier tier (mlx_lm.server)
    NUEXTRACT_BASE                (default http://gpu-host:8081)
                                  — extractor tier (mlx_vlm.server)
    PR_COMPOSE_CLASSIFIER_MODEL   (default Qwen3.6-35B-A3B-4bit-DWQ)
    PR_COMPOSE_WRITER_MODEL       (default NuExtract3-bf16 — schema-guided
                                   extractor on Qwen3.5-4B, ~5 GB. Override
                                   to mlx-community/numind-NuExtract-2.0-8B-MLX
                                   for the older 8 B model if needed. Both
                                   use the same chat-template-kwargs
                                   protocol; per-model sampling is auto-
                                   selected by family detection in
                                   `_writer_payload`. Replaces the Qwen3.6
                                   prose writer that lost a fight with
                                   thinking-mode tool loops, May 2026.)
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
NUEXTRACT_BASE = os.environ.get("NUEXTRACT_BASE", "http://gpu-host:8081")

# Two-tier model setup, two servers.
#
# Classifier (mlx_lm.server on MLX_BASE): runs on every new mail —
# pick a fast MoE quant. Qwen3.6-4bit-DWQ avoids the standard-4-bit
# tool-use degradation (mlx-lm #1011) and handles the binary
# significant/skip JSON reliably.
#
# Writer (mlx_vlm.server on NUEXTRACT_BASE): runs only on the ~5%
# deemed significant. We use NuExtract — purpose-built schema-guided
# extractor — instead of a general-purpose LLM, because thinking-mode
# Qwen3 chains loop indefinitely on ambiguous typography (see
# canonicalize_for_llm docstring) and non-thinking Qwen3 won't tool-
# call. NuExtract has no thinking loop to break (NuExtract-2.0) or
# keeps it opt-in (NuExtract3, off by default).
CLASSIFIER_MODEL = os.environ.get(
    "PR_COMPOSE_CLASSIFIER_MODEL",
    "mlx-community/Qwen3.6-35B-A3B-4bit-DWQ",
)
WRITER_MODEL = os.environ.get(
    "PR_COMPOSE_WRITER_MODEL",
    "mlx-community/NuExtract3-bf16",
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

_LEAKED_EOS_TOKENS = ("<|im_end|>", "<|endoftext|>", "<|end|>")


def _strip_code_fence(s: str) -> str:
    """Clean LLM JSON output: strip leaked EOS tokens (mlx_vlm.server doesn't
    strip them) and code fences (Qwen sometimes wraps JSON in ```json ...```
    despite being told not to)."""
    s = s.strip()
    for tok in _LEAKED_EOS_TOKENS:
        if s.endswith(tok):
            s = s[: -len(tok)].rstrip()
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


# ---------- Writer (NuExtract schema-guided extractor)

# NuExtract schema. Keys are output field names; values are the typed
# DSL — see https://huggingface.co/numind/NuExtract-2.0-8B and
# https://huggingface.co/numind/NuExtract3.
#
# Type choices:
#   "string"          — paraphrase OK (titles, headings, body prose)
#   "verbatim-string" — exact text from the input (quoted evidence,
#                       sender names — anything we want untouched)
#
# `date` and `date-time` types exist but we use "string" here because
# the model needs to produce date *ranges* ("2026-06-04 – 2026-06-05")
# and the typed `date` field rejects ranges.
#
# The model is not instructed in prose at all — the schema is the
# contract. Validation lives Python-side (`_validate_writer_output`).
WRITER_SCHEMA = {
    "pr_title": "string",
    "branch_keyword": "string",
    "memory_heading": "string",
    "memory_body": "string",
    "calendar_candidates": [{
        "date": "string",
        "title": "string",
        "evidence": "verbatim-string",
    }],
}


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


def _str_field(d: dict, key: str) -> str:
    """Coerce d[key] to a stripped string; treat None / non-str as empty.
    NuExtract returns `null` for any field it couldn't fill from the
    document (e.g. memory_body on a 187-char event invite), so we can't
    rely on isinstance(d[key], str)."""
    v = d.get(key)
    return v.strip() if isinstance(v, str) else ""


def _validate_writer_output(d: dict) -> dict:
    """Verify the model output and normalise into the dict shape consumed by
    `file_pr_for_message`. Tolerant of `null` field values (NuExtract returns
    those for fields it couldn't fill) — only `pr_title` is strictly
    required because without it the PR has no title.

    `calendar_candidates` is deduped by (date, title, evidence) so reply-
    chains that quote the same date multiple times don't produce N copies."""
    if not isinstance(d, dict):
        raise ValueError(f"writer output is not a dict: {d!r}")

    pr_title = _str_field(d, "pr_title")
    if not pr_title:
        raise ValueError(f"writer output missing pr_title: {d!r}")

    branch_keyword = _slugify(_str_field(d, "branch_keyword") or pr_title) or "mail"
    memory_heading = _str_field(d, "memory_heading") or pr_title
    raw_body = _str_field(d, "memory_body")
    memory_body = (
        raw_body
        or "_(modell trakk ikke ut detaljer — se PR-body for kildemailen)_"
    )

    out: dict = {
        "pr_title": pr_title[:70].strip(),
        "branch_keyword": branch_keyword,
        "memory_heading": memory_heading.lstrip("# ").strip(),
        "memory_body": memory_body.rstrip() + "\n",
        # Used by file_prs_for_significant_threads to decide whether the
        # writer's output is worth filing. A heading + placeholder body
        # is noise in the long-term memory file — better to tag the
        # thread `pr::nofile` and skip than to ship a non-diff that gets
        # closed unmerged.
        "memory_body_from_model": bool(raw_body),
    }

    candidates = d.get("calendar_candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    seen: set[tuple[str, str, str]] = set()
    validated: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        date = _str_field(c, "date")
        if not date:
            continue  # date-less candidate is useless for calendar dedup
        title = _str_field(c, "title")
        evidence = _str_field(c, "evidence")
        key = (date, title, evidence)
        if key in seen:
            continue
        seen.add(key)
        validated.append({
            "date": date,
            "title": title,
            "evidence": evidence,
            "already_in_calendar": bool(c.get("already_in_calendar", False)),
        })
    out["calendar_candidates"] = validated
    return out


def _writer_payload(model: str, document: str, schema: dict) -> dict:
    """Build the chat-completions payload for the extractor tier.

    mlx_vlm.server silently drops `chat_template_kwargs.template` (the
    standard NuExtract integration kwarg that vLLM honors). Workaround:
    inline the `# Template:` / `# Context:` blocks that NuExtract's
    chat template would normally produce, directly in the user message.
    Verified 2026-05-28 against NuExtract3-bf16.

    Branches on model family because NuExtract3 wants `temperature=0.2`
    + `enable_thinking=False` in fast mode, while NuExtract-2.0 wants
    greedy (`temperature=0`) and has no thinking knob.
    """
    template_str = json.dumps(schema, indent=4)
    user_text = f"# Template:\n{template_str}\n# Context:\n{document}"
    chat_template_kwargs: dict = {}
    if "NuExtract3" in model:
        temperature = 0.2
        chat_template_kwargs["enable_thinking"] = False
    else:
        temperature = 0  # NuExtract-2.0 → greedy
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
        "temperature": temperature,
        "max_tokens": 2048,
    }
    if chat_template_kwargs:
        payload["chat_template_kwargs"] = chat_template_kwargs
    return payload


def compose_pr(row: MailRow, client: httpx.Client) -> dict:
    """Two-stage extraction + generation for one mail.

    Stage 2 (extractor, NuExtract on NUEXTRACT_BASE): structured facts —
    calendar_candidates, pr_title, branch_keyword.

    Stage 3 (generator, Qwen3.6 on MLX_BASE): rewrites memory_heading +
    memory_body as concise Norwegian prose given the extraction + raw
    mail. Replaces NuExtract's prose fields (NuExtract often picks
    random sentences as "summary"); falls back to NuExtract output if
    the generator call fails so the pipeline doesn't deadlock on a
    flaky :8080.

    Returns the validated writer dict with `calendar_candidates`
    enriched by deterministic Python-side calendar lookup."""
    body = canonicalize_for_llm(row.body[:BODY_MAX_CHARS])
    subject = canonicalize_for_llm(row.subject)
    document = f"Subject: {subject}\nFrom: {row.sender}\n\n{body}"
    payload = _writer_payload(WRITER_MODEL, document, WRITER_SCHEMA)
    r = client.post(f"{NUEXTRACT_BASE}/v1/chat/completions", json=payload, timeout=300)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(_strip_code_fence(content))
    # Subject fallback for pr_title. NuExtract at temperature=0.2 occasionally
    # returns null for every field on terse mails (Spiker'n-class invites);
    # the validator hard-rejects null pr_title because a PR needs a title.
    # Subject is always present and for these mails IS the title — the
    # body-substance gate downstream still correctly skips when there's
    # nothing else to memorialise.
    if not (isinstance(parsed.get("pr_title"), str) and parsed["pr_title"].strip()):
        parsed["pr_title"] = row.subject or "(no subject)"
    out = _validate_writer_output(parsed)
    out["calendar_candidates"] = _verify_candidates(out["calendar_candidates"])

    # Tier 3: rewrite heading + body with Qwen3.6 on :8080. NuExtract is
    # purpose-built for extraction and treats summarisation as a
    # sentence-pick task ("Husk på å skriv en lapp" instead of a curated
    # entry). The generator gets the extraction as fact anchors so dates,
    # names, amounts don't drift.
    #
    # No fallback on failure — offensive programming. :8080 is shared
    # infrastructure with the classifier; if it's broken we want to see
    # it loudly rather than silently degrade to NuExtract sentence-
    # plucking. Errors propagate to file_prs_for_significant_threads
    # which logs + counts the thread as error and moves on.
    gen = generate_memory_prose(row, document, out, client)
    out["memory_heading"] = gen["heading"].lstrip("# ").strip()
    out["memory_body"] = gen["body"].rstrip() + "\n"
    out["memory_body_from_model"] = True
    return out


# Tier 3 — generator. Reuses the classifier model on MLX_BASE because
# it's already loaded. Qwen3.6 thinking-mode loops on extraction-under-
# uncertainty, but pure prose generation with structured facts as input
# is the kind of task it should handle — the failure was task fit, not
# capability.
GENERATOR_SYSTEM = """\
Du skriver en kort, faktuell norsk memory-entry for Examples daglige
fil basert på en mail. Du får mailen, og en JSON med strukturerte fakta
NuExtract har trukket ut (datoer, kalenderkandidater, foreslått tittel).

Output må følge dette eksakte formatet — to bokstavelige markører:

HEADING: <kort overskrift, 3-8 ord, ingen `##` prefiks>
BODY:
<2-4 setninger markdown. Inkluder nøkkelfakta: avsender (hvis bedrift),
datoer/frister, handling som kreves. Konsis, ikke prosaisk.>

`BODY:` markøren er obligatorisk på egen linje før brødteksten — ikke
hopp den over. Eksempel på korrekt output (ekte format, mellom -----):

-----
HEADING: Tilbud fra Eksempel Elektriske
BODY:
Pat Olsen sendte tilbudsbrev 20260510-1 på 95 000 NOK for
elektroarbeid før sommerferien. Astrid driver tråden. Frist for svar
ikke spesifisert.
-----

Bruk faktaene som ankerpunkter — datoer, beløp, navn skal være riktige.
Ingen prefiks, ingen kode-fence, ingen kommentarer etter BODY. Start
direkte med `HEADING:`.
"""


def _parse_generator_output(text: str) -> dict:
    """Pull HEADING / BODY out of the generator's line-format output.
    Strict: raises ValueError on missing markers or empty fields. The
    parser is the boundary between "model produced expected format" and
    "something is wrong" — silent partial results would hide prompt
    drift, model changes, or :8080 returning chatty prose."""
    text = _strip_code_fence(text)
    heading = ""
    body_lines: list[str] = []
    in_body = False
    saw_heading = False
    saw_body_marker = False
    for line in text.splitlines():
        if not in_body:
            m = re.match(r"^HEADING:\s*(.*)$", line)
            if m:
                heading = m.group(1).strip()
                saw_heading = True
                continue
            if line.strip().startswith("BODY:"):
                in_body = True
                saw_body_marker = True
                inline = line.split("BODY:", 1)[1].strip()
                if inline:
                    body_lines.append(inline)
                continue
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not saw_heading:
        raise ValueError(f"generator output missing HEADING: marker: {text[:200]!r}")
    if not saw_body_marker:
        raise ValueError(f"generator output missing BODY: marker: {text[:200]!r}")
    if not heading:
        raise ValueError(f"generator output has empty HEADING: {text[:200]!r}")
    if not body:
        raise ValueError(f"generator output has empty BODY: {text[:200]!r}")
    return {"heading": heading, "body": body}


def generate_memory_prose(row: MailRow, document: str, extraction: dict,
                          client: httpx.Client) -> dict:
    """Call the tier-3 generator. Returns {"heading": str, "body": str}.
    `document` is the canonicalized mail body already prepared by
    compose_pr; we reuse it so canonicalization rules don't drift."""
    facts = {
        "pr_title_suggested": extraction.get("pr_title", ""),
        "calendar_candidates": extraction.get("calendar_candidates", []),
    }
    user = (
        f"Mail:\n{document}\n\n"
        f"Strukturerte fakta:\n{json.dumps(facts, ensure_ascii=False, indent=2)}"
    )
    payload = {
        "model": CLASSIFIER_MODEL,  # Qwen3.6-4bit-DWQ, already on :8080
        "messages": [
            {"role": "system", "content": GENERATOR_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = client.post(f"{MLX_BASE}/v1/chat/completions", json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_generator_output(content)


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


def _is_future(date_str: str, today: date | None = None) -> bool:
    """True if the candidate's date range ends today or later. Past dates
    (NuExtract drags historical references like transaction dates out of
    e.g. accounting mails) shouldn't pollute the file diff."""
    today = today or date.today()
    end = _parse_candidate_date(date_str)[1]
    return end is not None and end >= today


def _future_candidates(candidates: list[dict],
                       today: date | None = None) -> list[dict]:
    """Subset of candidates whose date is in the future. Used by both the
    substance gate and the file-diff renderer."""
    today = today or date.today()
    return [c for c in candidates if _is_future(c.get("date", ""), today)]


def _render_memory_section(writer: dict, today: date | None = None) -> tuple[str, str]:
    """Build (heading, body) for the daily-memory-file diff. Prepends the
    earliest future date to the heading so the daily file groups by
    date naturally. Body is the writer's narrative untouched — the
    structured calendar list lives in CALENDAR.md (inserted by
    `_insert_calendar_events`), not the memory file."""
    heading = writer["memory_heading"]
    body = writer["memory_body"]
    future = sorted(
        _future_candidates(writer.get("calendar_candidates") or [], today),
        key=lambda c: c["date"],
    )
    if future:
        heading = f"{future[0]['date']} — {heading}"
    return heading, body


def _calendar_event_line(candidate: dict, thread_id: str) -> str:
    """Format one future candidate as a CALENDAR.md parseable event line
    per CALENDAR-RULES.md. The (Kilde: thread:...) tail gives the
    reviewer + future-me a direct link back to the source mail."""
    date_str = candidate["date"]
    title = candidate.get("title") or "(uten tittel)"
    return f"- **{date_str}** — {title}. (Kilde: mail thread:{thread_id}.)\n"


def _insert_calendar_events(candidates: list[dict], calendar_path: Path,
                            thread_id: str,
                            today: date | None = None) -> int:
    """Merge future candidates into CALENDAR.md under their month
    sections. Returns count of events inserted. Idempotent w.r.t.
    file-on-disk shape — uses retire_calendar's parser so the file
    stays well-formed for the daily sjekk-flow.

    All future candidates are inserted regardless of `already_in_calendar`
    — overlap markers in the PR description tell the reviewer which
    might be duplicates. Missing-from-diff is unrecoverable; redundant-
    in-diff is one delete."""
    from scripts.retire_calendar import parse as _parse_cal, ensure_section, MONTHS_EN
    future = _future_candidates(candidates, today)
    if not future:
        return 0
    doc = _parse_cal(calendar_path.read_text())
    inserted = 0
    for c in sorted(future, key=lambda x: x["date"]):
        start, _end = _parse_candidate_date(c["date"])
        if start is None:
            continue
        section = ensure_section(doc, MONTHS_EN[start.month - 1], start.year)
        section.insert_event_sorted(_calendar_event_line(c, thread_id))
        inserted += 1
    calendar_path.write_text(doc.render())
    return inserted


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


def _assert_single_commit_against_origin(worktree: Path,
                                         base_ref: str = "origin/master") -> None:
    """Refuse to push if the bot's branch contains anything other than
    exactly one commit ahead of `base_ref`. A clean bot PR has exactly
    one bot-authored MEM: commit on top of `origin/master`; anything
    else means the branch was contaminated (e.g. branched from a stale
    local `master` carrying unpushed work) and pushing would leak
    unrelated content into the PR.

    Raises RuntimeError loudly with the offending commit list so the
    cause is visible in the per-thread error log."""
    n_str = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base_ref}..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    n = int(n_str)
    if n == 1:
        return
    log = subprocess.run(
        ["git", "-C", str(worktree), "log", "--format=%h %an %s",
         f"{base_ref}..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    raise RuntimeError(
        f"bot branch has {n} commits against {base_ref}, expected 1.\n"
        f"refusing to push to avoid leaking unrelated work.\n"
        f"branch contents:\n{log}"
    )


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

    rendered_heading, rendered_body = _render_memory_section(writer, today)

    print(
        f"  → branch: {branch}\n"
        f"     title:  {writer['pr_title']}\n"
        f"     edit:   memory/{today.isoformat()}.md  +## {rendered_heading}\n"
        f"     body:   {rendered_body.strip()[:300]}"
        f"{cand_summary}",
        file=sys.stderr,
    )

    if not apply:
        return None

    pat = _bot_pat()
    worktree_path = Path(tempfile.mkdtemp(prefix="prcomp-"))
    try:
        # Branch from origin/master, not local master. The user's local
        # work-in-progress (unpushed commits) shouldn't ride along in
        # bot PRs — that conflates two streams. Fetch first so origin
        # ref is current; it's a no-op when nothing changed.
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", "master"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "add", "-b", branch,
             str(worktree_path), "origin/master"],
            check=True, capture_output=True, text=True,
        )

        memory_rel = Path("memory") / f"{today.isoformat()}.md"
        memory_path = worktree_path / memory_rel
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        existing = memory_path.read_text() if memory_path.exists() else f"# {today.isoformat()}\n"
        new_content = (
            existing.rstrip()
            + f"\n\n## {rendered_heading}\n\n{rendered_body}"
        )
        memory_path.write_text(new_content)

        files_to_add = [str(memory_rel)]
        calendar_rel = Path("CALENDAR.md")
        n_events = _insert_calendar_events(
            candidates, worktree_path / calendar_rel, thread_id, today,
        )
        if n_events:
            files_to_add.append(str(calendar_rel))

        subprocess.run(
            ["git", "-C", str(worktree_path), "add"] + files_to_add,
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

        _assert_single_commit_against_origin(worktree_path)

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
                                      client: httpx.Client) -> tuple[int, int, int]:
    """Find pr::significant threads without pr::filed; write + open PRs.

    Returns (n_filed, n_nofile, n_error). n_nofile = threads where the
    writer produced no substantive body (placeholder-only diff) — those
    are skipped + tagged `pr::nofile`."""
    query = "tag:pr::significant and not tag:pr::filed and not tag:pr::nofile"
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

    n_filed = n_nofile = n_error = 0
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

        # Substance gate: the file diff that lands in memory/YYYY-MM-DD.md
        # is `## heading + body + optional Datoer block`. Substance =
        # model wrote a real body, OR we have future-dated candidates
        # that _render_memory_section will pull into the diff as a date
        # anchor + Datoer list. Past-only candidates (Pia/Examplefund had 4×
        # 2025 transaction dates) don't count — they'd render the same
        # noise as null body. Skip + tag `pr::nofile` so the thread
        # doesn't come back next run.
        has_body = writer.get("memory_body_from_model", False)
        has_future = bool(_future_candidates(
            writer.get("calendar_candidates") or []))
        if not (has_body or has_future):
            print(f"  ⊘ skipping — no body and no future dates "
                  f"(would be placeholder-only diff)",
                  file=sys.stderr)
            if args.apply:
                apply_tags(f"thread:{tid}", ["+pr::nofile"])
            n_nofile += 1
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

    return n_filed, n_nofile, n_error


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
            n_filed, n_nofile, n_pr_err = file_prs_for_significant_threads(args, client)
        print(
            f"\n# file-prs done: {n_filed} PR(s) "
            + ("filed" if args.apply else "(dry-run)")
            + f", {n_nofile} skipped (no substance)"
            + f", {n_pr_err} error(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
