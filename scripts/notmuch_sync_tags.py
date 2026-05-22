#!/usr/bin/env python3
"""
Reconcile notmuch tags to Proton maildir folders.

The bulk import marked ~205k messages with `tag:inbox`, regardless of which
Proton folder they actually live in. That makes `tag:inbox` useless for
"what's actually in my Inbox right now". This script enforces:

    folder:INBOX     ↔ tag:inbox
    folder:Spam      ↔ tag:spam   (+ remove tag:inbox)
    folder:Archive   ↔ tag:archive (+ remove tag:inbox)
    folder:Sent      ↔ tag:sent   (+ remove tag:inbox, tag:unread)
    folder:Drafts    ↔ tag:draft  (+ remove tag:inbox)
    folder:Trash     ↔ tag:trash  (+ remove tag:inbox)

Idempotent — safe to run repeatedly. Designed to also work as a notmuch
`post-new` hook so the mapping stays correct as new mail arrives.

Default mode is --dry-run. Pass --apply to write changes. A timestamped
`notmuch dump` backup is written to memory/notmuch-dumps/ before any
--apply run, so a botched mass-edit can be reversed with `notmuch restore`.

Usage:
    scripts/notmuch_sync_tags.py            # dry run, print summary
    scripts/notmuch_sync_tags.py --apply    # actually re-tag
    scripts/notmuch_sync_tags.py --apply --quiet   # for cron / hook
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import subprocess
import sys

# Each rule: (folder name, tag to add, tags to remove if present)
RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("INBOX",   "inbox",   ()),
    ("Spam",    "spam",    ("inbox",)),
    ("Archive", "archive", ("inbox",)),
    ("Sent",    "sent",    ("inbox", "unread")),
    ("Drafts",  "draft",   ("inbox",)),
    ("Trash",   "trash",   ("inbox",)),
]

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent
BACKUP_DIR = WORKSPACE / "memory" / "notmuch-dumps"

MAILDIR_ROOT = pathlib.Path.home() / "Mail" / "Proton"


def _folder_query(folder: str) -> str:
    """Format a folder: query, quoting when the path needs it."""
    if any(c in folder for c in ' "()[]{}\\'):
        escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
        return f'folder:"{escaped}"'
    return f"folder:{folder}"


def discover_label_rules() -> list[tuple[str, str, tuple[str, ...]]]:
    """Auto-discover `Labels/<name>` maildirs and emit additive label rules.

    Proton labels are additive: a message can carry many labels alongside its
    canonical folder (INBOX/Archive/…). So these rules only add `tag:<name>`
    and never strip `inbox` etc. Bridge-internal `[Imap]/*` namespaces are
    skipped.
    """
    labels_dir = MAILDIR_ROOT / "Labels"
    if not labels_dir.is_dir():
        return []
    rules: list[tuple[str, str, tuple[str, ...]]] = []
    for entry in sorted(labels_dir.iterdir()):
        if not (entry / "cur").is_dir():
            continue
        name = entry.name
        if name.startswith("[Imap]"):
            continue
        rules.append((f"Labels/{name}", name, ()))
    return rules


def nm_count(query: str) -> int:
    out = subprocess.run(
        ["notmuch", "count", query], check=True, capture_output=True, text=True
    ).stdout.strip()
    return int(out or "0")


def nm_tag(plus: list[str], minus: list[str], query: str) -> None:
    args = ["notmuch", "tag"]
    args += [f"+{t}" for t in plus]
    args += [f"-{t}" for t in minus]
    args += ["--", query]
    subprocess.run(args, check=True)


KEEP_DUMPS = 5


def backup_dump() -> pathlib.Path:
    """Write a gzipped tag dump and keep only the most recent KEEP_DUMPS."""
    import gzip
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = BACKUP_DIR / f"tags-{ts}.dump.gz"
    with gzip.open(path, "wb") as f:
        proc = subprocess.run(
            ["notmuch", "dump", "--format=batch-tag"],
            check=True, capture_output=True,
        )
        f.write(proc.stdout)
    # Rotate
    dumps = sorted(BACKUP_DIR.glob("tags-*.dump*"))
    for old in dumps[:-KEEP_DUMPS]:
        old.unlink()
    return path


def plan() -> list[dict]:
    """Compute deltas for each rule without changing anything."""
    out = []
    for folder, add_tag, remove_tags in RULES + discover_label_rules():
        folder_q = _folder_query(folder)
        to_add = nm_count(f"{folder_q} and not tag:{add_tag}")
        removals = {}
        for rt in remove_tags:
            removals[rt] = nm_count(f"{folder_q} and tag:{rt}")
        out.append({
            "folder": folder,
            "add_tag": add_tag,
            "remove_tags": remove_tags,
            "folder_total": nm_count(folder_q),
            "to_add": to_add,
            "removals": removals,
        })
    return out


def apply(quiet: bool = False) -> None:
    for folder, add_tag, remove_tags in RULES + discover_label_rules():
        folder_q = _folder_query(folder)
        add_q = f"{folder_q} and not tag:{add_tag}"
        if nm_count(add_q):
            if not quiet:
                print(f"  +{add_tag} on {folder}")
            nm_tag([add_tag], [], add_q)
        for rt in remove_tags:
            rm_q = f"{folder_q} and tag:{rt}"
            if nm_count(rm_q):
                if not quiet:
                    print(f"  -{rt} on {folder}")
                nm_tag([], [rt], rm_q)


def print_plan(rows: list[dict]) -> None:
    print(f"{'folder':<10} {'total':>8}  {'+tag (needed)':<24} {'-tags (counts)'}")
    for r in rows:
        rm = ", ".join(f"-{t}={c}" for t, c in r["removals"].items()) or "-"
        print(f"{r['folder']:<10} {r['folder_total']:>8}  "
              f"+{r['add_tag']} ({r['to_add']} needed)        {rm}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Actually modify tags. Default is dry-run.")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-rule output (for cron/hook).")
    args = ap.parse_args(argv)

    rows = plan()
    total_changes = sum(r["to_add"] + sum(r["removals"].values()) for r in rows)

    if not args.quiet:
        print_plan(rows)
        print(f"\nTotal tag operations needed: {total_changes}")

    if not args.apply:
        if not args.quiet:
            print("\nDry run only. Pass --apply to make changes.")
        return 0

    if total_changes == 0:
        if not args.quiet:
            print("Nothing to do.")
        return 0

    backup_path = backup_dump()
    if not args.quiet:
        print(f"\nBackup written: {backup_path}")
        print("Applying...")
    apply(quiet=args.quiet)
    if not args.quiet:
        print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
