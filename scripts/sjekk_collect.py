#!/usr/bin/env python3
"""sjekk_collect.py — parallel front-load of the sjekk's mechanical work.

One run of `sjekk-collect` replaces the per-track "first commands" of every
sjekk SOP. It runs in two phases:

  Phase A — freshness (parallel):
    * If the last `mail-sync.service` run is older than ~15 min (or
      --force-sync), start the unit and wait for it (blocking, 300 s cap)
      so the mail triage reads a fresh index.
    * `spond-sync --once` (idempotent; auto-bumps its own cursor).

  Phase B — collect (parallel, one output file per track):
    mail.txt, spond.txt, spond-events.txt, signal.txt,
    calendar-retire.txt, backup-health.txt, embed-health.txt, services.txt

Sibling tools are invoked by name (`mailshow`, `spondshow`, …) — they are on
PATH when this runs under `uv run --project diary-scripts`.

Each Phase-B command has its own timeout (default 120 s, `--timeout`). A
failure or timeout never aborts the run: the track's file gets whatever
partial output arrived plus a `!! FAILED (exit N)` / `!! TIMEOUT` marker.

Output lands in `memory/sjekk/<UTC-timestamp>/` under the workspace root
(`mail_reader.config.workspace_root()`), with a `latest` symlink repointed
at the new directory. An `INDEX.md` summarizes each track (size, EMPTY,
failures) so the agent reads INDEX first and only opens files with content.
`memory/sjekk/` is git-ignored in the diary repo — mail/signal content
stays local, like `memory/signal/`.

Cursor bumps (`memory/mail-state.json`, `signalshow --bump`), calendar
enrichment, and filing into daily notes / topic files stay agent-side —
this tool only collects.

Usage::

    uv run sjekk-collect                    # freshness check + collect
    uv run sjekk-collect --force-sync       # always restart mail-sync first
    uv run sjekk-collect --no-sync          # skip Phase A entirely
    uv run sjekk-collect --timeout 60       # per-command cap, seconds
    uv run sjekk-collect --out /tmp/sjekk   # override output root
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mail_reader.config import workspace_root

DEFAULT_TIMEOUT = 120
SYNC_TIMEOUT = 300
MAIL_SYNC_MAX_AGE = dt.timedelta(minutes=15)

# The *show tools signal "nothing new" as exit 1 with only cursor /
# "(no matching …)" lines on stderr — that's a quiet track, not a failure.
_NOTHING_NEW_RE = re.compile(r"^\((cursor: .+|no matching .+)\)$")


def _is_nothing_new(output: str) -> bool:
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    return bool(lines) and all(_NOTHING_NEW_RE.match(l) for l in lines)

# Phase B: one entry per output file. A track may run several commands; their
# outputs are concatenated in order.
TRACKS: dict[str, list[list[str]]] = {
    "mail.txt": [["mailshow", "--since-cursor", "--headers-only"]],
    "spond.txt": [["spondshow", "--since-cursor", "--headers-only"]],
    "spond-events.txt": [["spondshow", "--kind", "event", "--future",
                          "--headers-only"]],
    "signal.txt": [["signalshow", "--since-cursor", "--headers-only"]],
    "calendar-retire.txt": [["retire-calendar"]],
    "backup-health.txt": [["systemctl", "show", "restic-backup.service",
                           "-p", "Result", "-p", "ExecMainExitTimestamp",
                           "--value"]],
    "embed-health.txt": [["psql", "-d", "mailvec", "-t", "-c",
                          "SELECT now() - max(embedded_at) FROM messages;"]],
    "services.txt": [
        ["systemctl", "--user", "is-active", "signal-capture.service"],
        ["systemctl", "--user", "show", "mail-sync.service",
         "-p", "ExecMainExitTimestamp", "--value"],
    ],
}


@dataclass
class TrackResult:
    """Outcome of one Phase-B track (one output file)."""
    filename: str
    output: str = ""
    exit_code: int | None = 0      # None = could not start at all
    timed_out: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.timed_out or self.exit_code != 0

    @property
    def empty(self) -> bool:
        return not self.output.strip()

    @property
    def nothing_new(self) -> bool:
        """Quiet-track signal: the command reported only 'no matching …'."""
        return not self.failed and _is_nothing_new(self.output)


def _run_one(argv: list[str], timeout: int) -> TrackResult:
    """Run a single command, capturing combined stdout+stderr. Never raises."""
    res = TrackResult(filename="", exit_code=0)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
        res.output = proc.stdout + proc.stderr
        res.exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        res.output = out + err
        res.exit_code = None
        res.timed_out = True
    except OSError as e:
        res.exit_code = None
        res.errors.append(f"could not start {argv[0]!r}: {e}")
    return res


def run_tracks(tracks: dict[str, list[list[str]]],
               timeout: int) -> list[TrackResult]:
    """Run every track in parallel; one TrackResult per output file.

    Per-command failure degrades to a `!! FAILED` / `!! TIMEOUT` marker in
    the track's output — a bad track never aborts the others.
    """
    jobs: dict[concurrent.futures.Future[TrackResult], tuple[str, int]] = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, sum(len(c) for c in tracks.values()))) as pool:
        for filename, commands in tracks.items():
            for i, argv in enumerate(commands):
                jobs[pool.submit(_run_one, argv, timeout)] = (filename, i)
        partial: dict[str, dict[int, TrackResult]] = {f: {} for f in tracks}
        for fut in concurrent.futures.as_completed(jobs):
            filename, i = jobs[fut]
            partial[filename][i] = fut.result()

    results = []
    for filename, commands in tracks.items():
        merged = TrackResult(filename=filename)
        for i, argv in enumerate(commands):
            part = partial[filename][i]
            if part.exit_code == 1 and _is_nothing_new(part.output):
                part.exit_code = 0  # quiet track, not a failure
            merged.output += part.output
            if part.timed_out:
                merged.output += "!! TIMEOUT\n"
                merged.timed_out = True
            elif part.exit_code is None:
                note = part.errors[0] if part.errors else "could not start"
                merged.output += f"!! FAILED ({note})\n"
            elif part.exit_code != 0:
                merged.output += f"!! FAILED (exit {part.exit_code})\n"
            if part.failed and merged.exit_code == 0:
                merged.exit_code = part.exit_code
        results.append(merged)
    return results


def render_index(stamp: str, results: list[TrackResult]) -> str:
    """One line per track: file, size, EMPTY / failure markers."""
    lines = [
        f"# sjekk-collect {stamp}",
        "",
        "Read this first; open only files with content. "
        "`!!` tracks need a manual look.",
        "",
    ]
    for res in results:
        size = len(res.output.encode("utf-8"))
        n_lines = res.output.count("\n")
        if res.failed:
            marker = res.output.strip().splitlines()[-1] if res.output.strip() \
                else "!! FAILED"
            status = f"{marker} — partial output below"
            if res.empty:
                status = marker
            lines.append(f"- **{res.filename}** — {status}")
        elif res.empty:
            lines.append(f"- {res.filename} — EMPTY")
        elif res.nothing_new:
            lines.append(f"- {res.filename} — nothing new")
        else:
            lines.append(f"- {res.filename} — {n_lines} lines, {size} B")
    lines.append("")
    return "\n".join(lines)


def write_run(out_root: Path, results: list[TrackResult],
              now: dt.datetime | None = None) -> Path:
    """Write <out_root>/<UTC-stamp>/ with one file per track + INDEX.md,
    then repoint the `latest` symlink. Returns the run directory."""
    now = now or dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = out_root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    for res in results:
        (run_dir / res.filename).write_text(res.output)
    (run_dir / "INDEX.md").write_text(render_index(stamp, results))
    latest = out_root / "latest"
    tmp = out_root / ".latest.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(run_dir.name)
    tmp.replace(latest)
    return run_dir


# --- Phase A: freshness ------------------------------------------------------


def _last_mail_sync() -> dt.datetime | None:
    """Wall-clock time of the last mail-sync.service run, or None."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", "mail-sync.service",
             "-p", "ExecMainExitTimestamp", "--value"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # "Sun 2026-10-11 12:34:56 CEST" — drop weekday, ignore the zone label
    # (host-local time either way; a freshness check tolerates that).
    parts = proc.stdout.strip().split()
    if len(parts) < 3:
        return None
    try:
        naive = dt.datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone()


def mail_sync_stale(max_age: dt.timedelta = MAIL_SYNC_MAX_AGE) -> bool:
    """True when the mail index may be stale (or the check itself failed —
    sync first, ask questions later)."""
    last = _last_mail_sync()
    if last is None:
        return True
    return dt.datetime.now(dt.UTC) - last > max_age


def phase_a(*, force_sync: bool, no_sync: bool) -> list[str]:
    """Freshness passes, run in parallel. Returns human log lines."""
    if no_sync:
        return ["phase A skipped (--no-sync)"]
    log: list[str] = []

    def _mail() -> None:
        if force_sync or mail_sync_stale():
            res = _run_one(
                ["systemctl", "--user", "start", "mail-sync.service"],
                timeout=SYNC_TIMEOUT)
            if res.failed:
                log.append(f"!! mail-sync.service restart failed: "
                           f"{res.output.strip()[-200:]}")
            else:
                log.append("mail-sync.service restarted (index was stale)")
        else:
            log.append("mail-sync fresh — no restart needed")

    def _spond() -> None:
        res = _run_one(["spond-sync", "--once"], timeout=SYNC_TIMEOUT)
        tail = res.output.strip().splitlines()[-1] if res.output.strip() else ""
        log.append(f"!! spond-sync failed: {tail}" if res.failed
                   else f"spond-sync: {tail or 'ok'}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_mail)
        pool.submit(_spond)
    return log


def main() -> None:
    parser = argparse.ArgumentParser(
        description="parallel collector for the sjekk (see module docstring)")
    parser.add_argument("--force-sync", action="store_true",
                        help="restart mail-sync.service even if it looks fresh")
    parser.add_argument("--no-sync", action="store_true",
                        help="skip Phase A (no mail-sync / spond-sync)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"per-command timeout in Phase B "
                             f"(default {DEFAULT_TIMEOUT} s)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output root (default <workspace>/memory/sjekk)")
    args = parser.parse_args()

    out_root = args.out or workspace_root() / "memory" / "sjekk"

    for line in phase_a(force_sync=args.force_sync, no_sync=args.no_sync):
        print(line)

    results = run_tracks(TRACKS, timeout=args.timeout)
    run_dir = write_run(out_root, results)
    print(f"wrote {run_dir} (latest -> {run_dir.name})")
    n_failed = sum(1 for r in results if r.failed)
    if n_failed:
        print(f"!! {n_failed} track(s) failed — see INDEX.md",
              file=sys.stderr)


if __name__ == "__main__":
    main()
