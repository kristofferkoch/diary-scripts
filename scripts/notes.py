#!/usr/bin/env python3
"""notes.py — sjekk-side CLI for the notes queue.

The /notes/ page on server lets the user dump free-text reminders
("things I don't want to forget") into a Postgres queue. A check round
digests them with this tool:

    uv run scripts/notes.py            # list pending notes (default)
    uv run scripts/notes.py list --all # include already-done notes
    uv run scripts/notes.py add "text" # append a note from the terminal
    uv run scripts/notes.py done 42    # mark note 42 handled (drops off page)
    uv run scripts/notes.py rm 42      # hard-delete note 42

Typical sjekk flow: run with no args, act on each pending note (file a
calendar entry, update a topic file, etc.), then `done <id>` it so it
stops showing on the page but stays in the table as a record.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `from mail_reader …` work whether run as a script or imported.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mail_reader import db, notes  # noqa: E402


def _fmt(note: dict) -> str:
    when = note["created_at"].strftime("%Y-%m-%d %H:%M")
    flag = "" if note["status"] == "pending" else " [done]"
    body = note["body"].replace("\n", "\n      ")
    return f"#{note['id']} ({when}){flag}\n      {body}"


def cmd_list(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        if args.all:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, body, status, created_at, updated_at "
                    "FROM notes_queue ORDER BY created_at DESC"
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = notes.list_pending(conn)
    if not rows:
        print("(køen er tom)")
        return 0
    for note in rows:
        print(_fmt(note))
    print(f"\n{len(rows)} notat(er).")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        note = notes.add(conn, args.text)
    print(f"lagt til #{note['id']}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            notes.set_status(conn, args.id, "done")
        except KeyError:
            print(f"ingen notat med id {args.id}", file=sys.stderr)
            return 1
    print(f"#{args.id} markert som håndtert")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            notes.delete(conn, args.id)
        except KeyError:
            print(f"ingen notat med id {args.id}", file=sys.stderr)
            return 1
    print(f"#{args.id} slettet")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="notes queue CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="list notes (pending by default)")
    p_list.add_argument("--all", action="store_true", help="include done notes")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="add a note")
    p_add.add_argument("text", help="note body")
    p_add.set_defaults(func=cmd_add)

    p_done = sub.add_parser("done", help="mark a note handled")
    p_done.add_argument("id", type=int)
    p_done.set_defaults(func=cmd_done)

    p_rm = sub.add_parser("rm", help="delete a note")
    p_rm.add_argument("id", type=int)
    p_rm.set_defaults(func=cmd_rm)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare invocation = list pending.
        args.all = False
        return cmd_list(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
