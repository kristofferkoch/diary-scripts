#!/usr/bin/env python3
"""Advance the sjekk triage cursor after a triage pass.

The only writer of ``memory/sjekk/cursor.json`` (see ``scripts/sjekk_cursor.py``).
Run it per track once that track is actually triaged:

    uv run sjekk-advance mail signal
    uv run sjekk-advance --manifest memory/sjekk/2026-11-02T08-05-00Z vigilo

The new value is the **upper bound of the bundle window** recorded in the
run's ``manifest.json`` — not "the newest record printed", which silently
skipped anything the reader filtered out and anything fetched after the
reader looked. Advancing is monotonic, so a repeated or concurrent run cannot
move a track backwards.

A track whose bundle failed, or a track missing from the manifest, is refused
(and the command exits 1) — that window was not fully shown, so it must not be
marked done. Partial triage is expressed by naming only the finished tracks.

``--init`` is the one-time migration off the old tracked cursors
(``memory/mail-state.json``, ``memory/signal-state.json``): it seeds the
cursor document from them and refuses to touch an existing one unless
``--force`` is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mail_reader.config import legacy_state_root
from scripts import sjekk_cursor

MANIFEST_NAME = "manifest.json"


def _manifest_path(args: argparse.Namespace) -> Path:
    if args.manifest:
        path = Path(args.manifest)
        return path / MANIFEST_NAME if path.is_dir() else path
    return sjekk_cursor.state_dir(args.state_dir) / "latest" / MANIFEST_NAME


def _ids_from(manifest_dir: Path, entry: dict) -> list[str]:
    name = entry.get("ids_file")
    if not isinstance(name, str) or not name:
        return []
    try:
        return [line.strip() for line in
                (manifest_dir / name).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except OSError:
        return []


def _seed(cursor: dict, track: str, source: Path, key: str) -> bool:
    try:
        raw = json.loads(source.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError):
        return False
    if not isinstance(raw, str) or not raw:
        return False
    try:
        until = sjekk_cursor.parse_iso(raw)
    except ValueError:
        return False
    sjekk_cursor.advance(cursor, track, until)
    print(f"{track}: seeded {sjekk_cursor.format_iso(until)} from {source}")
    return True


def _init(args: argparse.Namespace) -> int:
    path = sjekk_cursor.store_path(args.state_dir)
    if path.exists() and not args.force:
        print(f"!! {path} already exists — refusing to overwrite (use --force)",
              file=sys.stderr)
        return 1

    memory = sjekk_cursor.state_dir(args.state_dir).parent
    cursor: dict = {}
    seeds = (
        ("mail", memory / "mail-state.json", "last_successful_run"),
        ("signal", memory / "signal-state.json", "cursor"),
        ("vigilo", legacy_state_root() / "vigilo-state.json", "cursor"),
        ("skolemelding", legacy_state_root() / "skolemelding-state.json", "cursor"),
    )
    for track, source, key in seeds:
        _seed(cursor, track, source, key)

    sjekk_cursor.save(cursor, path)
    print(f"wrote {path}")
    print("next: remove memory/mail-state.json and memory/signal-state.json "
          "in the next MEM: commit")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="*", choices=list(sjekk_cursor.TRACKS),
                    metavar="TRACK",
                    help=f"tracks to advance ({', '.join(sjekk_cursor.TRACKS)})")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="run directory (or manifest.json) to advance from; "
                         "default <state-dir>/latest/manifest.json")
    ap.add_argument("--state-dir", type=Path, default=None,
                    help="sjekk runtime directory (default <workspace>/memory/sjekk)")
    ap.add_argument("--init", action="store_true",
                    help="seed cursor.json from the old tracked cursors and exit")
    ap.add_argument("--force", action="store_true",
                    help="with --init: overwrite an existing cursor.json")
    args = ap.parse_args(argv)

    if args.init:
        return _init(args)

    if not args.tracks:
        ap.error("name at least one track (or use --init)")

    manifest_path = _manifest_path(args)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"!! cannot read manifest {manifest_path}: {exc}", file=sys.stderr)
        return 1

    tracks = manifest.get("tracks") or {}
    cursor_path = sjekk_cursor.store_path(args.state_dir)
    cursor = sjekk_cursor.load(cursor_path)
    rc = 0
    for track in args.tracks:
        entry = tracks.get(track)
        if not isinstance(entry, dict):
            print(f"!! {track}: not in {manifest_path}", file=sys.stderr)
            rc = 1
            continue
        if not entry.get("ok"):
            print(f"!! {track}: bundle failed — not advancing", file=sys.stderr)
            rc = 1
            continue
        raw = entry.get("until")
        try:
            until = sjekk_cursor.parse_iso(str(raw))
        except (TypeError, ValueError):
            print(f"!! {track}: manifest has no usable until ({raw!r})",
                  file=sys.stderr)
            rc = 1
            continue
        ids = _ids_from(manifest_path.parent, entry)
        moved = sjekk_cursor.advance(cursor, track, until, ids)
        if moved:
            print(f"{track}: {sjekk_cursor.format_iso(until)}"
                  + (f" (+{len(ids)} overlap ids)" if ids else ""))
        else:
            print(f"{track}: already at or after {sjekk_cursor.format_iso(until)}")

    if rc == 0:
        sjekk_cursor.save(cursor, cursor_path)
        print(f"wrote {cursor_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
