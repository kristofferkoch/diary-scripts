#!/usr/bin/env python3
"""
Read mail bodies (and headers) from notmuch as plain text.

Examples:
    scripts/mailshow.py thread:00000000000349df
    scripts/mailshow.py id:abcdef@example.com
    scripts/mailshow.py --limit=5 'tag:inbox and date:today..'
    scripts/mailshow.py --headers-only 'from:gonordic'
    scripts/mailshow.py --max-chars=8000 thread:0000000000000018
    scripts/mailshow.py --attachment-text id:abcdef@example.com
    scripts/mailshow.py --attachments=/tmp/foo id:abcdef@example.com
    scripts/mailshow.py --since-cursor --headers-only   # everything since memory/mail-state.json
    scripts/mailshow.py --since-cursor 'from:astrid'     # narrow the since-cursor sweep

Notes:
  - Mail is READ-ONLY (see TOOLS.md). This script never sends or modifies mail.
"""

from __future__ import annotations

import argparse
import email
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from email.policy import default as email_default
from pathlib import Path

from mail_reader.config import workspace_root

MAIL_STATE_PATH = workspace_root() / "memory" / "mail-state.json"


def load_cursor(state_path: Path = MAIL_STATE_PATH) -> datetime:
    """Read `last_successful_run` from the mail-state JSON and return as a UTC datetime."""
    data = json.loads(state_path.read_text())
    raw = data["last_successful_run"]
    # fromisoformat in older Pythons rejects the trailing "Z"; normalize it.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cursor_query(cursor: datetime, extra: str | None) -> str:
    """Build a notmuch query for inbox mail strictly newer than `cursor`, optionally AND-ed with `extra`.

    notmuch's `date:@N..` is inclusive at the lower bound, so we add 1 second to
    skip the cursor message itself (the cursor records the Date of the last
    successfully processed mail; that mail is, by definition, done).
    """
    base = f"tag:inbox and date:@{int(cursor.timestamp()) + 1}.."
    if extra:
        return f"({base}) and ({extra})"
    return base

from scripts.embed_mail import iter_attachments  # noqa: E402


def notmuch_search_ids(query: str, limit: int | None) -> list[str]:
    cmd = ["notmuch", "search", "--output=messages"]
    if limit is not None:
        cmd.append(f"--limit={limit}")
    cmd.append(query)
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            ids.append(line[3:])
    return ids


def fetch_raw(message_id: str) -> bytes:
    return subprocess.run(
        ["notmuch", "show", "--format=raw", f"id:{message_id}"],
        check=True, capture_output=True,
    ).stdout


_HTML_STYLE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&[a-zA-Z#0-9]+;")
_WS_RUN = re.compile(r"[ \t]+")
_BLANKLINES = re.compile(r"\n\s*\n+")


def html_to_text(html: str) -> str:
    text = _HTML_STYLE.sub(" ", html)
    text = _HTML_TAG.sub(" ", text)
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'"))
    text = _HTML_ENTITY.sub("", text)
    text = _WS_RUN.sub(" ", text)
    text = _BLANKLINES.sub("\n\n", text)
    return text


_UNSAFE_FN = re.compile(r"[/\x00]")


def safe_filename(fn: str | None, fallback_idx: int) -> str:
    """Strip path separators and leading dots; fall back if empty.

    >>> safe_filename("foo.pdf", 0)
    'foo.pdf'
    >>> safe_filename("../etc/passwd", 0)
    '_etc_passwd'
    >>> safe_filename(".hidden", 0)
    'hidden'
    >>> safe_filename("..", 4)
    'unnamed_4'
    >>> safe_filename(None, 3)
    'unnamed_3'
    >>> safe_filename("", 1)
    'unnamed_1'
    """
    if not fn:
        return f"unnamed_{fallback_idx}"
    cleaned = _UNSAFE_FN.sub("_", fn).lstrip(".")
    return cleaned or f"unnamed_{fallback_idx}"


def save_attachments(raw: bytes, outdir: Path) -> list[Path]:
    """Write each attachment to outdir, returning the list of paths written.

    Filename collisions get a `.N` suffix. Creates outdir if missing.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, (fn, _mime, data, _text) in enumerate(iter_attachments(raw)):
        name = safe_filename(fn, idx)
        dest = outdir / name
        n = 1
        while dest.exists():
            dest = outdir / f"{name}.{n}"
            n += 1
        dest.write_bytes(data)
        written.append(dest)
    return written


def render_attachment_text(raw: bytes, max_chars: int) -> list[str]:
    """Per-attachment header + extracted text block."""
    blocks: list[str] = []
    for fn, mime, data, text in iter_attachments(raw):
        header = f"----- attachment: {fn} [{mime}] ({len(data)} bytes) -----"
        if text:
            if max_chars and len(text) > max_chars:
                text = text[:max_chars] + f"\n…[attachment truncated at {max_chars} chars]"
            blocks.append(header + "\n" + text.rstrip())
        else:
            blocks.append(header + "\n(no text extractable — binary or unsupported MIME)")
    return blocks


def render_message(
    msg_id: str,
    headers_only: bool,
    max_chars: int,
    attachment_text: bool = False,
    attachments_dir: Path | None = None,
) -> str:
    raw = fetch_raw(msg_id)
    msg = email.message_from_bytes(raw, policy=email_default)

    lines = [
        f"===== id:{msg_id} =====",
        f"Date:    {msg.get('Date', '')}",
        f"From:    {msg.get('From', '')}",
        f"To:      {msg.get('To', '')}",
        f"Subject: {msg.get('Subject', '')}",
    ]

    # Attachment summary
    attachments = []
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp or part.get_filename():
            fn = part.get_filename() or "(unnamed)"
            attachments.append(f"{fn} [{part.get_content_type()}]")
    if attachments:
        lines.append("Attachments: " + "; ".join(attachments))

    if attachments_dir is not None:
        for path in save_attachments(raw, attachments_dir):
            lines.append(f"Saved: {path}")

    if not headers_only:
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is None:
            lines.append("\n(no readable body part)")
        else:
            content = body.get_content()
            if body.get_content_type() == "text/html":
                content = html_to_text(content)

            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + f"\n…[truncated at {max_chars} chars]"

            lines.append("")
            lines.append(content.rstrip())

    if attachment_text:
        for block in render_attachment_text(raw, max_chars):
            lines.append("")
            lines.append(block)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?",
                    help="A notmuch query (id:..., thread:..., or any search expression). "
                         "Optional when --since-cursor is given; if both are given, they are AND-ed.")
    ap.add_argument("--since-cursor", action="store_true",
                    help="Start from memory/mail-state.json's last_successful_run "
                         "(builds `tag:inbox and date:@<unix>..`). The cursor is hand-maintained "
                         "— see CLAUDE.md 'How to process new mail'.")
    ap.add_argument("--limit", type=int, default=20, help="Max messages to render for non-id queries (default: 20).")
    ap.add_argument("--max-chars", type=int, default=4000, help="Truncate each body at N chars (default: 4000, 0 = no truncation).")
    ap.add_argument("--headers-only", action="store_true", help="Print headers + attachment list, skip body.")
    ap.add_argument("--attachment-text", action="store_true",
                    help="Render extracted attachment text (PDF/DOCX/ODT/text) inline after body.")
    ap.add_argument("--attachments", type=Path, metavar="DIR", default=None,
                    help="Save raw attachment bytes to DIR (created if missing).")
    args = ap.parse_args(argv)

    if args.since_cursor:
        cursor = load_cursor()
        q = cursor_query(cursor, args.query.strip() if args.query else None)
        print(f"(cursor: {cursor.isoformat()} → {q})", file=sys.stderr)
    elif args.query:
        q = args.query.strip()
    else:
        ap.error("query is required unless --since-cursor is given")
    if q.startswith("id:"):
        msg_ids = [q[3:]]
    else:
        msg_ids = notmuch_search_ids(q, args.limit)
    if not msg_ids:
        print("(no matching messages)", file=sys.stderr)
        return 1

    for mid in msg_ids:
        try:
            print(render_message(
                mid,
                args.headers_only,
                args.max_chars,
                attachment_text=args.attachment_text,
                attachments_dir=args.attachments,
            ))
            print()
        except subprocess.CalledProcessError as e:
            print(f"!! failed to read id:{mid}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
