# scripts/

Local helper scripts for the workspace. Python deps live in a **uv project
rooted at `diary-scripts/`** (`pyproject.toml` + `uv.lock`). Scripts are
exposed as console entry points — invoke them with

```bash
# From inside the diary-scripts/ submodule:
uv run <entry-point> …

# From the diary repo root (passes --project so uv finds the right pyproject):
uv run --project diary-scripts <entry-point> …
```

Entry-point names: `mailshow`, `search-mail`, `embed-mail`, `embed-messages`,
`archive-inbox`, `notmuch-sync-tags`, `spond-sync`, `spondshow`, `finance-ingest`,
`retire-calendar`, `sjekk-collect`, `kindle-dashboard`, `immich-recent`,
`dsh-web-notify`, `vigilo-api`, `vigilo-login`, `vigilo-sync`, `vigiloshown`,
`skolemelding-api`, `skolemelding-login`, `skolemelding-sync`,
`skolemeldingshow`. Server modules are
invoked as `uv run python -m mail_reader.server` and
`uv run python -m scripts.kindle_dashboard.serve`.

`uv run` creates/syncs `.venv/` on demand; you never need to activate
anything. Tests: `uv run pytest scripts/` (doctests + `test_embed_mail.py`).

Two exceptions to the entry-point rule: **`sudo-ask`** and **`sudo-broker`** are
installed by `scripts/install_sudo_broker` to `/usr/local/bin` and
`/usr/local/sbin` as root-owned stdlib-only files, and are **not** console
entry points. Root must never execute code out of the agent-writable venv, so
the daemon is run from the installed copy, never via `uv run`.

**Config split.** Real infra/PII values (hostnames, paths, home coordinates,
family names, Spond IDs, the important-senders list) live in a private
`config/local.toml` + `config/important_senders.txt` in the diary repo, read
via `mail_reader.config` (`$DIARY_CONFIG` env var or the
`~/.config/diary/config.toml` symlink). The submodule ships only placeholder
defaults. New code must resolve the workspace data root (where `CALENDAR.md`,
`memory/`, topic files live) via `mail_reader.config.workspace_root()` (from
config `paths.project`) — not by walking `__file__`.

## Docs — open the one you need

Per-tool detail and the triage SOPs live in `docs/`. This file is the index.

| Doc | Covers |
| --- | --- |
| [`docs/sjekk-collect.md`](docs/sjekk-collect.md) | the parallel collector every sjekk opens with: freshness phase, per-track output files, `INDEX.md` |
| [`docs/mail.md`](docs/mail.md) | notmuch/Proton sync, `mailshow`, the **mail triage SOP**, sent-mail caveat, semantic search, `embed-mail`, image descriptions, `archive-inbox`, maildir gotchas, attachments |
| [`docs/spond.md`](docs/spond.md) | `spond-sync` / `spondshow`, the **Spond triage SOP**, auth |
| [`docs/vigilo.md`](docs/vigilo.md) | `vigilo-sync` / `vigiloshown`, the **Vigilo triage SOP** (AKS/school), auth |
| [`docs/skolemelding.md`](docs/skolemelding.md) | `skolemelding-sync` / `skolemeldingshow`, the **Skolemelding triage SOP** (Skoleplattform Oslo), auth, `skolemelding-api` probe |
| [`docs/signal.md`](docs/signal.md) | `signal-capture` + `signalshow`, the **Signal triage SOP**, receipt photos in Note-to-Self |
| [`docs/finance.md`](docs/finance.md) | `finance-ingest`, receipt routes (Signal vs mail), unidentified-transaction checks |
| [`docs/calendar.md`](docs/calendar.md) | `retire-calendar` mechanical step + manual enrichment, and the Kindle-refresh rule |
| [`docs/webapp.md`](docs/webapp.md) | the `mail_reader` webapp |
| [`docs/kindle-dashboard.md`](docs/kindle-dashboard.md) | the Kindle wall-display PNG generator |
| [`docs/immich.md`](docs/immich.md) | `immich-recent` (Immich API reader) |
| [`docs/dsh-web-notify.md`](docs/dsh-web-notify.md) | dsh web login URL → Signal notifier |
| [`docs/signal-notify.md`](docs/signal-notify.md) | `signal-notify-proxy`: write-only note-to-self proxy in front of signal-cli (agent-facing) |
| [`docs/sudo-broker.md`](docs/sudo-broker.md) | `sudo-broker` / `sudo-ask`: per-command root approval from an open terminal, replacing standing `NOPASSWD` |
| [`docs/retired-notes-shopping.md`](docs/retired-notes-shopping.md) | removed 2026-10: the `/notes/` and `/shopping/` pages |

> **`scripts/` is a stopgap name.** Once we add scripts that aren't
> mail-related, this whole folder will get split by subsystem (e.g.
> `mail/`, …). Don't write naming around the current layout.

---
