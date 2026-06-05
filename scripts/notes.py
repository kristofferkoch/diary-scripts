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
    uv run scripts/notes.py image 7    # dump attachment #7's image to a file

Typical sjekk flow: run with no args, act on each pending note (file a
calendar entry, update a topic file, etc.), then `done <id>` it so it
stops showing on the page but stays in the table as a record. A note can
carry a photo (taken/uploaded from the phone); the listing flags it as
`📎 bilde (vedlegg #N)`. To actually see the image, `image N <file>`
writes it out so it can be opened/read.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mail_reader import db, notes


def _fmt(note: dict, attachment: dict | None = None) -> str:
    when = note["created_at"].strftime("%Y-%m-%d %H:%M")
    flag = "" if note["status"] == "pending" else " [done]"
    out = f"#{note['id']} ({when}){flag}"
    if note["body"].strip():
        out += "\n      " + note["body"].replace("\n", "\n      ")
    if attachment is not None:
        dims = ""
        if attachment.get("width") and attachment.get("height"):
            dims = f", {attachment['width']}×{attachment['height']}"
        out += f"\n      📎 bilde (vedlegg #{attachment['id']}{dims})"
        if attachment.get("gps_lat") is not None and attachment.get("gps_lon") is not None:
            out += (
                f"\n        📍 {attachment['gps_lat']:.5f}, {attachment['gps_lon']:.5f}"
                f"  https://www.openstreetmap.org/?mlat={attachment['gps_lat']:.5f}"
                f"&mlon={attachment['gps_lon']:.5f}#map=16/{attachment['gps_lat']:.5f}"
                f"/{attachment['gps_lon']:.5f}"
            )
        if attachment.get("description"):
            desc = attachment["description"].replace("\n", "\n        ")
            out += f"\n        ↳ {desc}"
        else:
            out += (
                f"\n        ↳ (ingen beskrivelse — "
                f"`notes.py image {attachment['id']} <fil>` for å se bildet)"
            )
    return out


def cmd_list(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        if args.all:
            # list_pending only returns pending rows; for --all we need the
            # done ones too. Same attachment resolution as list_pending.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT n.id, n.body, n.status, n.created_at, n.updated_at, "
                    "a.id AS attachment_id "
                    "FROM notes_queue n "
                    "LEFT JOIN LATERAL (SELECT id FROM note_attachments "
                    "  WHERE note_id = n.id ORDER BY id LIMIT 1) a ON true "
                    "ORDER BY n.created_at DESC"
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = notes.list_pending(conn)
        if not rows:
            print("(køen er tom)")
            return 0
        for note in rows:
            attachment = None
            if note.get("attachment_id"):
                attachment = notes.get_attachment(conn, note["attachment_id"])
            print(_fmt(note, attachment))
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


def cmd_image(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        blob = notes.get_attachment_blob(conn, args.attachment_id)
    if blob is None:
        print(f"ingen vedlegg med id {args.attachment_id}", file=sys.stderr)
        return 1
    _mime, data = blob
    out = Path(args.out) if args.out else Path(f"/tmp/note-attachment-{args.attachment_id}.jpg")
    out.write_bytes(data)
    print(f"skrev {len(data)} bytes til {out}")
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

    p_img = sub.add_parser("image", help="dump an attachment's image to a file")
    p_img.add_argument("attachment_id", type=int, help="vedlegg # shown in the listing")
    p_img.add_argument("out", nargs="?", help="output path (default /tmp/note-attachment-<id>.jpg)")
    p_img.set_defaults(func=cmd_image)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare invocation = list pending.
        args.all = False
        return cmd_list(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
