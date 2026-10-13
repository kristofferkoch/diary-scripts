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

**Phase A — freshness (parallel):** checks when `mail-sync.service` last
ran (`systemctl --user show mail-sync.service -p ExecMainExitTimestamp
--value`); if older than ~15 min (or `--force-sync`), the unit is started
and awaited (300 s cap) before any mail is read. In parallel,
`spond-sync --once` fetches new Spond activity (idempotent, auto-bumps its
own cursor), and `vigilo-sync --once` / `skolemelding-sync --once` fetch
new school messages (idempotent; triage cursor stays with the reader's
`--bump`; auth loss exits 0 with `auth_required` set).

**Phase B — collect (parallel):** one output file per track, written to
`memory/sjekk/<UTC-timestamp>/` with a `latest` symlink repointed at the
new run:

| file | command |
|---|---|
| `mail.txt` | `mailshow --since-cursor --headers-only` |
| `spond.txt` | `spondshow --since-cursor --headers-only` |
| `spond-events.txt` | `spondshow --kind event --future --headers-only` |
| `signal.txt` | `signalshow --since-cursor --headers-only` (cursor bump stays agent-side) |
| `vigilo.txt` | `vigiloshown --since-cursor --headers-only` |
| `skolemelding.txt` | `skolemeldingshow --since-cursor --headers-only` |
| `calendar-retire.txt` | `retire-calendar` (real run — idempotent; moved lines captured for enrichment) |
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
bumps (`memory/mail-state.json`, `signalshow --bump`), calendar
enrichment, daily-note / topic-file filing, receipt digesting into
`RECEIPTS.md`.
