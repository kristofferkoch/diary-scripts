# Sjekk collector — `sjekk_collect.py`

> Index: [scripts/](../README.md)

Every **sjekk** opens with this: it front-loads the mechanical half of all
the per-track SOPs (sibling docs here) into one parallel run.

```bash
uv run sjekk-collect                 # freshness check + collect
uv run sjekk-collect --force-sync    # always restart mail-sync first
uv run sjekk-collect --no-sync       # skip the freshness phase
uv run sjekk-collect --timeout 60    # per-command cap (default 120 s)
```

**Phase A — freshness:** checks when `mail-sync.service` last
ran (`systemctl --user show mail-sync.service -p ExecMainExitTimestamp
--value`); if older than ~15 min (or `--force-sync`), the unit is started
and awaited (300 s cap) before any mail is read. In parallel,
`spond-sync --once` fetches new Spond activity (idempotent, auto-bumps its
own cursor). Then `vigilo-sync --once` / `skolemelding-sync --once` run —
**gated on the notification mail**: both services always mail when there is
new content, so the sync only runs when notmuch has a notification mail
(`subject:"Ny melding i Vigilo"` / `subject:"Ny Skolemelding"`) newer than
the sync's own `last_successful_run` in
`/var/lib/diary/legacy-state/<track>-state.json` (or trusted collector state,
for broker runs). A sync older than 7 days runs anyway (safety net if
notifications stop), as does any lookup failure, and `--force-sync` overrides
the gate. The syncs are idempotent; auth loss exits 0 with `auth_required`
set. Phase B still runs `vigiloshown` / `skolemeldingshow` every time — they
read local data only, so a skipped sync still surfaces untriaged backlog.

**Phase B — collect (parallel):** one output file per track, written to
`memory/sjekk/<UTC-timestamp>/` with a `latest` symlink repointed at the
new run. Every cursor-backed reader is given an **explicit window**
`(since, until]`: `until` is the moment Phase A finished, `since` is the
triaged cursor for that track from `memory/sjekk/cursor.json` (mail widened
by 24 h). Readers write nothing; the collector writes the run's
`manifest.json` with the window and each track's `ok` flag.

| file | command |
|---|---|
| `mail.txt` | `mailshow --since <cursor−24h> --until <T> --headers-only` (+ `--ids-out`, `--exclude-ids`) |
| `spond.txt` | `spondshow --since <cursor> --until <T> --headers-only` |
| `spond-events.txt` | `spondshow --kind event --future --headers-only` (no window — repeats every run) |
| `signal.txt` | `signalshow --since <cursor> --until <T> --headers-only` |
| `vigilo.txt` | `vigiloshown --since <cursor> --until <T> --headers-only` |
| `skolemelding.txt` | `skolemeldingshow --since <cursor> --until <T> --headers-only` |
| `calendar-retire.txt` | `retire-calendar --dry-run` (preview only — the move is applied in the mem worktree) |
| `backup-health.txt` | `systemctl show restic-backup.service -p Result -p ExecMainExitTimestamp --value` |
| `embed-health.txt` | `psql -d mailvec -t -c "SELECT source, now() - max(embedded_at) FROM messages GROUP BY source ORDER BY source;"` (per-source, so one stale source can't hide behind the others) |
| `services.txt` | `systemctl --user is-active signal-capture.service` + mail-sync timestamp |

Each command has its own timeout; a failure or timeout never aborts the
run — the track's file keeps whatever partial output arrived plus a
`!! FAILED (exit N)` / `!! TIMEOUT` marker. `INDEX.md` in the run dir has
one line per track (size / EMPTY / failure marker) — **read INDEX first,
open only files with content.**

`memory/sjekk/` is git-ignored in the diary repo (mail/signal content —
same treatment as `memory/signal/`).

Stays agent-side (the collector deliberately does none of this): cursor
**advance** (`sjekk-advance <track>` after triage), the **calendar move**
itself (`retire-calendar --src/--dst` against the mem worktree copies) and
its enrichment, daily-note / topic-file filing, receipt digesting into
`RECEIPTS.md`.

`calendar-retire.txt` is a preview: `--dry-run` reports the one-off events
whose end date has passed, and the sjekk applies the move in the worktree
that will be committed (SJEKK.md §5). A collector must never leave tracked
files dirty in the integration checkout.

## Cursor and window

The triage cursor is `memory/sjekk/cursor.json` (git-ignored), written only
by `sjekk-advance` and read only here. It records the **upper bound of the
last triaged window**, not the newest record shown.

```bash
uv run sjekk-advance mail signal          # after triaging those tracks
uv run sjekk-advance --manifest memory/sjekk/<stamp> vigilo
```

Advance only the tracks you finished: a failed bundle (`ok: false` in the
manifest) and an unknown track are refused, and the write is monotonic so a
repeated or concurrent advance cannot move the cursor backwards. Readers
never read or write it — `--since-cursor` and `--bump` are gone.

Why the window is fixed *after* Phase A: everything this run fetched has a
collection time ≤ `until`, so the next window `(until, …]` cannot miss or
duplicate it. Mail is the exception — it is filtered on the `Date:` header,
which can predate arrival — so mail windows overlap by 24 h and the ids
shown in the overlap travel in the cursor (`ids`) to be excluded next time.
