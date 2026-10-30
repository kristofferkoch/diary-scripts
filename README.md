# diary-scripts

Personal mail/calendar/School automation tooling, extracted from a private
long-term-memory workspace and published as a reference for anyone building
similar local-first plumbing on top of [notmuch](https://notmuchmail.org/),
Postgres + [pgvector](https://github.com/pgvector/pgvector), and a local LLM.

> **Sanitized mirror.** This repo is split out of a private repository that
> doubles as the author's memory store. The full git history is preserved, but
> all personal data — names, email addresses, home address, employer, family,
> contractors, finances, infrastructure hostnames — has been replaced with
> placeholders (`Astrid`, `user@example.com`, `ExampleCorp`, `Eksempelveien`,
> `gpu-host`, `example.ts.net`, …). Norwegian bank account numbers and KIDs
> that appear in test fixtures are public-by-design payment references, not
> secrets. If you spot anything that looks like real personal data, please open
> an issue.

## What's here

| Path | What it is |
|------|------------|
| `scripts/` | CLI helpers and console entry points: mail viewers & embedding pipeline, notmuch tag sync, inbox auto-archive, Spond / Vigilo / Signal / Skoleplattform Oslo sync & viewers, calendar retirement, the sjekk collector, finance ingestion, Immich recent-photo reader, Kindle wall-dashboard generator, and dsh web login notifier. Start at [`scripts/README.md`](scripts/README.md). |
| `mail_reader/` | A FastAPI web app: inbox summaries, structured extraction, agenda/calendar views, priority scoring, and thread analysis. See [`mail_reader/DESIGN.md`](mail_reader/DESIGN.md) and [`mail_reader/IDEAS.md`](mail_reader/IDEAS.md). |
| `migrations/` | Postgres schema evolution — 16 migration files covering pgvector, attachments, summaries, queues, notes, shopping, VLM, sources, and more. |
| `qcow-builder/` | Docker-based QCOW2 image builder: turns a restic whole-`/` snapshot into a bootable VM image with original UUIDs, GPT layout, and SELinux labels intact. |
| `pyproject.toml` / `uv.lock` | The [uv](https://docs.astral.sh/uv/) project (Python ≥3.14). Includes `[build-system]` (hatchling) and `[project.scripts]` console entry points. |

## Console entry points

Everything runs under [uv](https://docs.astral.sh/uv/) — no manual venv needed.
The package exposes console entry points, so you invoke tools by name rather
than by path:

```bash
# From inside the diary-scripts/ submodule:
uv run <entry-point> …

# From the diary repo root (passes --project so uv finds the right pyproject):
uv run --project diary-scripts <entry-point> …
```

| Entry point | Purpose |
|---|---|
| `mailshow` | View a mail message (structured display) |
| `search-mail` | Full-text / semantic mail search (pgvector) |
| `embed-mail` | Embed new messages into pgvector |
| `embed-messages` | Batch embed messages (alternative pipeline) |
| `embed-images` | Embed images for VLM description generation |
| `archive-inbox` | Auto-archive processed inbox |
| `notmuch-sync-tags` | Sync notmuch tags to/from Postgres |
| `spond-sync` | Sync Spond events |
| `spondshow` | View Spond data |
| `signal-capture` | Capture Signal messages |
| `signalshow` | View Signal messages |
| `finance-ingest` | Ingest bank/finance exports |
| `retire-calendar` | Retire past calendar entries |
| `sjekk-collect` | Parallel sjekk collector (mail/Spond/Signal/Vigilo/health) |
| `vigilo-api` | Vigilo API probe |
| `vigilo-login` | Vigilo auth |
| `vigilo-sync` | Sync Vigilo (AKS/school) data |
| `vigiloshown` | View Vigilo data |
| `skolemelding-api` | Skoleplattform Oslo API probe |
| `skolemelding-login` | Skoleplattform Oslo auth |
| `skolemelding-sync` | Sync Skoleplattform Oslo data |
| `skolemeldingshow` | View Skoleplattform Oslo data |
| `kindle-dashboard` | Serve & regenerate Kindle wall display |
| `immich-recent` | Read recent photos from Immich API |
| `dsh-web-notify` | dsh web login URL → Signal notifier |

Server modules are invoked as:

```bash
uv run python -m mail_reader.server          # FastAPI web app
uv run python -m scripts.kindle_dashboard.serve  # Kindle dashboard server
```

## Testing

```bash
uv run pytest                                # doctests + unit tests
uv run pytest scripts/                       # scripts-only
```

## Configuration

Real infra/PII values (hostnames, paths, home coordinates, family names,
Spond/Vigilo/Skoleplattform member-ids, the important-senders list) live in a
private `config/local.toml` + `config/important_senders.txt` in the parent
diary repo, read via `mail_reader.config` (`$DIARY_CONFIG` env var or the
`~/.config/diary/config.toml` symlink). Placeholder defaults are baked in, so
everything compiles and the tests pass without any config file present. To adapt
to your own setup, create a config file and override the relevant keys (mail
store path, Postgres DSN, LLM server URL, workspace root, etc.).

New code that needs the workspace path must call
`mail_reader.config.workspace_root()` — never walk `__file__`.

The scripts assume a local environment (a notmuch mail store, a Postgres
instance, a local LLM server) that isn't included here — they're published to
read and adapt, not to run turnkey.

## License

[MIT](LICENSE) © 2026 Kristoffer Koch
