"""Heuristic-floor priority classifier for the summary worker queue.

Each enqueued `summaries` row carries a `priority` (0..3) used as the
primary `ORDER BY` key by the workers. This module computes that score
deterministically from the sender address — no LLM call, no body read.

Ladder:
    HIGH (3)    — known-important sender (family, active contractors,
                  barnehage). One-line regexes in `important_senders.txt`
                  (sibling file) so the user can extend without touching
                  code.
    DEFAULT (2) — unknown sender; behaves like today's FIFO.
    LOW (1)     — reserved; not produced by this floor.
    NOISE (0)   — automated notifications, newsletters, mass-send infra.

Noise is checked first so `noreply@somewhere-important.com` still ends
up at the bottom. Tier-1 may later refine the score from the body
(planned). For now this is the only writer.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import cfg_path

HIGH = 3
DEFAULT = 2
LOW = 1
NOISE = 0

# Substrings (case-insensitive) anywhere in the from_addr that mark a
# message as automated noise. Order doesn't matter — any match wins.
_NOISE_PATTERNS = re.compile(
    r"""(?ix)
    (?:
        no[\-\.]?reply
      | do[\-\.]?not[\-\.]?reply
      | donotreply
      | notifications? @
      | updates-noreply
      | nyhetsbrev @
      | newsletter @
      | mailer-daemon
      | postmaster @
      | bounce[a-z\-]* @
      | marketing @
      | hello @ email\.
      | hello @ mail\.
    )
    """
)

# Full domains (and any subdomain of) that are mass-send infrastructure
# or marketing — every mail from them is noise by construction.
_NOISE_DOMAINS = frozenset({
    "email.storytel.com",
    "news.proton.me",
    "linkedin.com",
    "filtermedia.no",
    "regnskogfondet.no",
    "medlem.tekna.no",
    "accounts.google.com",
    "amazonses.com",
    "sparkpost.com",
    "sparkpostmail.com",
    "sendgrid.net",
    "mailgun.org",
    "mtasv.net",
})


# Real list lives in the private config dir; falls back to the sanitized
# sidecar shipped with the repo.
_IMPORTANT_PATH = cfg_path("important_senders", Path(__file__).with_name("important_senders.txt"))


def _load_important() -> list[re.Pattern[str]]:
    """Load patterns from the sidecar text file. Each non-blank,
    non-comment line is a regex compiled case-insensitively. Missing
    file → empty list (default-only scoring)."""
    if not _IMPORTANT_PATH.exists():
        return []
    out: list[re.Pattern[str]] = []
    for raw in _IMPORTANT_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(re.compile(line, re.I))
    return out


_IMPORTANT = _load_important()


def _domain_of(from_addr: str) -> str:
    """Last `@…` token, lowercased, trailing `>` trimmed. Empty if no `@`."""
    if "@" not in from_addr:
        return ""
    tail = from_addr.rsplit("@", 1)[-1].strip().lower()
    return tail.rstrip(">").strip()


def score(from_addr: str | None, subject: str | None = None) -> int:
    """Return the priority (0..3) for an email based on its sender.

    Pure function — same inputs always produce the same output. Subject
    is accepted for forward compatibility but unused today; the body /
    subject signal is reserved for a planned tier-1-LLM refinement.

    Missing or empty `from_addr` → DEFAULT (don't penalize on missing
    metadata; the queue can still process the row in arrival order)."""
    if not from_addr:
        return DEFAULT
    lc = from_addr.lower()
    # Noise wins over importance so noreply@important-domain still sinks.
    if _NOISE_PATTERNS.search(lc):
        return NOISE
    domain = _domain_of(lc)
    if domain:
        if domain in _NOISE_DOMAINS:
            return NOISE
        for nd in _NOISE_DOMAINS:
            if domain.endswith("." + nd):
                return NOISE
    for pat in _IMPORTANT:
        if pat.search(lc):
            return HIGH
    return DEFAULT
