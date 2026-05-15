#!/usr/bin/env python3
"""
Read mail bodies (and headers) from notmuch as plain text.

Examples:
    scripts/mailshow.py thread:00000000000349df
    scripts/mailshow.py id:abcdef@example.com
    scripts/mailshow.py --limit=5 'tag:inbox and date:today..'
    scripts/mailshow.py --headers-only 'from:gonordic'
    scripts/mailshow.py --max-chars=8000 thread:0000000000000018

Notes:
  - For attachments use `notmuch show --format=raw id:<id> | munpack -C <outdir>`,
    not this script. Bodies only.
  - Mail is READ-ONLY (see TOOLS.md). This script never sends or modifies mail.
"""

from __future__ import annotations

import argparse
import email
import re
import subprocess
import sys
from email.policy import default as email_default


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


def render_message(msg_id: str, headers_only: bool, max_chars: int) -> str:
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

    if headers_only:
        return "\n".join(lines)

    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        lines.append("\n(no readable body part)")
        return "\n".join(lines)

    content = body.get_content()
    if body.get_content_type() == "text/html":
        content = html_to_text(content)

    if max_chars and len(content) > max_chars:
        content = content[:max_chars] + f"\n…[truncated at {max_chars} chars]"

    lines.append("")
    lines.append(content.rstrip())
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="A notmuch query (id:..., thread:..., or any search expression).")
    ap.add_argument("--limit", type=int, default=20, help="Max messages to render for non-id queries (default: 20).")
    ap.add_argument("--max-chars", type=int, default=4000, help="Truncate each body at N chars (default: 4000, 0 = no truncation).")
    ap.add_argument("--headers-only", action="store_true", help="Print headers + attachment list, skip body.")
    args = ap.parse_args(argv)

    q = args.query.strip()
    if q.startswith("id:"):
        msg_ids = [q[3:]]
    else:
        msg_ids = notmuch_search_ids(q, args.limit)
    if not msg_ids:
        print("(no matching messages)", file=sys.stderr)
        return 1

    for mid in msg_ids:
        try:
            print(render_message(mid, args.headers_only, args.max_chars))
            print()
        except subprocess.CalledProcessError as e:
            print(f"!! failed to read id:{mid}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
