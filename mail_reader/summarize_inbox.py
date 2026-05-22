"""Bulk-enqueue messages for summary generation.

Hook for `~/.local/bin/mail-sync.sh`: after `embed_mail.py --all` runs,
push new inbox mail onto the summary worker queue so it'll be ready when
the user opens it. Idempotent — `schedule_all_passes` skips messages
that already have an in-flight row.

This script ONLY enqueues. The actual generation happens in the webapp's
background workers (see `mail_reader.workers`). If the webapp is down,
items pile up at `status='pending'` and the workers drain them on next
startup.

Usage:

    uv run python -m mail_reader.summarize_inbox 'tag:inbox AND date:7d..'
    uv run python -m mail_reader.summarize_inbox --limit 30 'tag:inbox'

Suggested tail of mail-sync.sh:

    mbsync -a && \\
    notmuch new && \\
    uv run --frozen --no-sync scripts/embed_mail.py --all --quiet && \\
    uv run --project /home/user/diary --frozen --no-sync \\
        python -m mail_reader.summarize_inbox \\
        --quiet 'tag:inbox AND date:7d..'
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from . import db, summarize


def _notmuch_ids(query: str, limit: int) -> list[str]:
    out = subprocess.run(
        ["notmuch", "search", "--format=json", "--output=messages",
         f"--limit={limit}", "--sort=newest-first", query],
        check=True, capture_output=True, text=True,
    ).stdout
    import json
    return json.loads(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="notmuch query, e.g. 'tag:inbox AND date:1d..'")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ids = _notmuch_ids(args.query, args.limit)
    if not ids:
        return 0
    if not args.quiet:
        print(f"[summarize_inbox] {len(ids)} candidates "
              f"(model={summarize.SUMMARY_MODEL}, "
              f"prompt={summarize.PROMPT_VERSION})", file=sys.stderr)

    enqueued = idle = 0
    t0 = time.time()
    with db.connect() as conn:
        for mid in ids:
            new_claims = summarize.schedule_all_passes(conn, mid)
            if new_claims:
                enqueued += new_claims
            else:
                idle += 1

    if not args.quiet:
        elapsed = time.time() - t0
        print(f"[summarize_inbox] done in {elapsed:.1f}s — "
              f"enqueued={enqueued} already-known={idle} "
              f"(processed by webapp workers; see `journalctl --user -u mail-reader`)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
