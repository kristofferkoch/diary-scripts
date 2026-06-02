#!/usr/bin/env python3
"""
Archive stale INBOX mail per rules. Acts on the maildir directly; the next
mbsync run pushes the move to Proton.

Rules live in scripts/archive_inbox_rules.json — each rule has a notmuch
query and a max_age_days. Anything matching the query in INBOX older than
that age gets moved to Archive.

Maildir gotchas (learned 2026-05-15, see TOOLS.md):
  - When moving a maildir file across folders, STRIP the `,U=<n>` IMAP-UID
    suffix from the filename. Leaving it on causes mbsync to abort with
    `Maildir error: duplicate UID N in /<folder>` — fatal for the whole sync.
  - `notmuch search --output=files` returns multiple paths per message
    (Proton's All Mail mirrors every message). Filter to `/INBOX/` paths
    only when picking what to move.
  - Use file PATH (not mtime) to identify moved files later — `mv` preserves
    mtime, so `find -mmin -N` won't catch them.

Usage:
    scripts/archive_inbox.py              # dry-run (default)
    scripts/archive_inbox.py --apply      # actually move
    scripts/archive_inbox.py --apply --quiet   # for timer
    scripts/archive_inbox.py --rules path/to/other.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

from mail_reader.config import workspace_root

# Rules file ships with the code (sibling of this module); logs go to the
# workspace data dir.
DEFAULT_RULES = pathlib.Path(__file__).resolve().parent / "archive_inbox_rules.json"
LOG_DIR = workspace_root() / "memory" / "mail"
MAIL_ROOT = pathlib.Path.home() / "Mail" / "Proton"
INBOX_DIR = MAIL_ROOT / "INBOX"
ARCHIVE_DIR = MAIL_ROOT / "Archive"
# Proton label applied to every auto-archived message. Created via webmail or
# IMAP CREATE Labels/autoarchived; visible in Proton web UI as a filterable tag.
LABEL_DIR = MAIL_ROOT / "Labels" / "autoarchived"

# Strip mbsync's `,U=<n>` UID tracker from a maildir filename.
_UID_SUFFIX = re.compile(r",U=\d+")


def strip_uid(name: str) -> str:
    return _UID_SUFFIX.sub("", name)


def notmuch_files(query: str) -> list[pathlib.Path]:
    out = subprocess.run(
        ["notmuch", "search", "--output=files", query],
        check=True, capture_output=True, text=True,
    ).stdout
    return [pathlib.Path(line) for line in out.splitlines() if line.strip()]


def inbox_files(query: str, max_age_days: int) -> list[pathlib.Path]:
    """Files currently in INBOX maildir, matching query, older than N days."""
    cutoff = f"..-{max_age_days}d"
    full_q = f"folder:INBOX and ({query}) and date:{cutoff}"
    files = notmuch_files(full_q)
    # Filter to actual INBOX paths (notmuch returns All Mail copies too).
    return [f for f in files if "/INBOX/" in str(f) and f.exists()]


def move_to_archive(src: pathlib.Path, apply: bool) -> pathlib.Path:
    """Move INBOX/{cur,new}/<name> → Archive/{cur,new}/<name-without-Uid>.

    Also copies the message to Labels/autoarchived/{cur,new}/ so it shows up
    in Proton as a filterable label. Both the Archive and Labels copies have
    their `,U=<n>` suffix stripped (per mbsync UID-collision constraint).
    """
    sub = src.parent.name  # 'cur' or 'new'
    stripped = strip_uid(src.name)
    dest = ARCHIVE_DIR / sub / stripped
    label_dest = LABEL_DIR / sub / stripped
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if LABEL_DIR.exists():
            label_dest.parent.mkdir(parents=True, exist_ok=True)
            # Copy first (preserves source until both copies exist), then move.
            shutil.copy2(str(src), str(label_dest))
        shutil.move(str(src), str(dest))
    return dest


def run_notmuch_new(quiet: bool) -> None:
    cmd = ["notmuch", "new"]
    if quiet:
        cmd.append("--quiet")
    subprocess.run(cmd, check=True)


def log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "archive-inbox.log.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", default=str(DEFAULT_RULES), help="Path to rules JSON.")
    ap.add_argument("--apply", action="store_true", help="Actually move files. Default is dry-run.")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-rule output.")
    args = ap.parse_args(argv)

    rules = json.loads(pathlib.Path(args.rules).read_text())["rules"]
    total_moved = 0
    per_rule = []

    for rule in rules:
        name = rule["name"]
        files = inbox_files(rule["query"], rule["max_age_days"])
        per_rule.append({"name": name, "matched": len(files)})
        if not args.quiet:
            print(f"[{name}] query={rule['query']!r} age>={rule['max_age_days']}d  →  {len(files)} file(s)")
        for src in files:
            dest = move_to_archive(src, apply=args.apply)
            if not args.quiet:
                action = "moved" if args.apply else "would move"
                print(f"  {action}: {src.name}  →  Archive/{dest.parent.name}/{dest.name}")
            if args.apply:
                total_moved += 1

    if not args.apply:
        if not args.quiet:
            print("\nDry run only. Pass --apply to actually archive.")
        return 0

    if total_moved:
        run_notmuch_new(quiet=args.quiet)
        # The post-new hook reconciles folder→tag mapping.
        log({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "moved": total_moved,
            "rules": per_rule,
        })
        if not args.quiet:
            print(f"\nArchived {total_moved} message(s). Next mail-sync run pushes to Proton.")
    else:
        if not args.quiet:
            print("Nothing matched.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
