# diary-scripts

Personal mail/calendar/notes automation tooling, extracted from a private
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
| `scripts/` | CLI helpers: mail embedding pipeline, notmuch tag sync, inbox auto-archive, mail/Spond viewers, calendar retirement, notes & shopping queues, a per-mail GitHub-PR composer, and the Kindle wall-dashboard generator. Start at [`scripts/README.md`](scripts/README.md). |
| `mail_reader/` | A FastAPI web app: inbox summaries, structured extraction, agenda/calendar views, a notes capture page, and priority scoring. See [`mail_reader/DESIGN.md`](mail_reader/DESIGN.md) and [`mail_reader/IDEAS.md`](mail_reader/IDEAS.md). |
| `migrations/` | Postgres schema (pgvector, attachments, summaries, queues, notes, shopping). |
| `pyproject.toml` / `uv.lock` | The [uv](https://docs.astral.sh/uv/) project that ties it together. Includes `[build-system]` (hatchling) and `[project.scripts]` console entry points. |

## Running

Everything runs under [uv](https://docs.astral.sh/uv/) — no manual venv needed.

The package exposes console entry points, so you invoke tools by name rather
than by path:

```bash
uv run mailshow                        # view a mail message
uv run notes                           # notes queue
uv run shopping                        # shopping list
uv run search-mail                     # full-text / semantic mail search
uv run embed-mail                      # embed new messages into pgvector
uv run archive-inbox                   # auto-archive processed inbox
uv run notmuch-sync-tags               # sync notmuch tags to/from Postgres
uv run spond-sync                      # sync Spond events
uv run spondshow                       # view Spond data
uv run finance-ingest                  # ingest bank/finance exports
uv run retire-calendar                 # retire past calendar entries
uv run kindle-dashboard                # regenerate Kindle wall display
uv run python -m mail_reader.server    # start the FastAPI web app
uv run pytest scripts/ mail_reader/   # the test suite (486 tests, all green)
```

**Configuration.** The code reads real deployment values from a private TOML
file, looked up via the `DIARY_CONFIG` environment variable or the default path
`~/.config/diary/config.toml`. Placeholder defaults are baked in, so everything
compiles and the tests pass without any config file present. To adapt to your
own setup, create a config file and override the relevant keys (mail store path,
Postgres DSN, LLM server URL, workspace root, etc.).

The scripts assume a local environment (a notmuch mail store, a Postgres
instance, a local LLM server) that isn't included here — they're published to
read and adapt, not to run turnkey.

## License

[MIT](LICENSE) © 2026 Kristoffer Koch
