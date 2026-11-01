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
`skolemeldingshow`, `diary-sync`, `diary-sync-broker`, `diary-pipeline-triage`,
`agent-merge-review`, `diary-audit`, `diary-auditd`, `diary-production-baseline`, `diary-lock`, `diary-lockd`, `diary-network-auditd`, `diary-dns-auditd`, `diary-fresh-unlock`, `diary-lock-dashboard`. Server modules are invoked as `uv run python -m mail_reader.server` and
`uv run python -m scripts.kindle_dashboard.serve`.

`uv run` creates/syncs `.venv/` on demand; you never need to activate
anything. Tests: `uv run pytest scripts/` (doctests + `test_embed_mail.py`).

Two exceptions to the entry-point rule: **`sudo-ask`** and **`sudo-broker`** are
installed by `scripts/install_sudo_broker` to `/usr/local/bin` and
`/usr/local/sbin` as root-owned stdlib-only files, and are **not** console
entry points. Root must never execute code out of the agent-writable venv, so
the daemon is run from the installed copy, never via `uv run`.

**Config split.** Ordinary private values (paths, home coordinates, family
names and the important-senders list) live in `config/local.toml` +
`config/important_senders.txt` in the diary repo, read via `mail_reader.config`
(`$DIARY_CONFIG` env var or the `~/.config/diary/config.toml` symlink).
Non-secret host endpoints live in root-owned world-readable
`/etc/diary/hosts.toml`; privileged collector settings stay in `root:kek` `0640`
`/etc/diary/trusted.toml`. Fixed pipeline model settings use separate root-owned
`/etc/diary/pipeline.toml`. None has an env or local-config override. Legacy
Spond/Vigilo/Skolemelding cursors, JSONL, and attachments live only in
`/var/lib/diary/legacy-state`; trusted collector state and immutable events stay
in their separate `/var/lib/diary` roots. The submodule ships only placeholder
examples. New code must resolve the workspace data root (where `CALENDAR.md`,
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
| [`docs/signal-notify.md`](docs/signal-notify.md) | `signal-notify-proxy` + `signal-attachment`: note-to-self sends and read-only attachment downloads (agent-facing) |
| [`docs/diary-sync.md`](docs/diary-sync.md) | `diary-sync` / `diary-sync-broker`: allow-listed socket for the sjekk's privileged steps (spond/vigilo/skolemelding sync, `systemctl --user`) so the agent never holds those secrets |
| [`docs/event-spool.md`](docs/event-spool.md) | independent trusted collection, immutable event IDs, atomic spool writes, and pipeline read/agent denial permissions |
| [`docs/pipeline-identity.md`](docs/pipeline-identity.md) | PIPE-02 fixed-purpose `diary-pipeline` identity, root-owned numeric model endpoint, permanent UID egress, and live verification gates |
| [`docs/pipeline-triage.md`](docs/pipeline-triage.md) | PIPE-03 fixed-local-model triage, strict result validation, local review, and restart-safe state |
| [`docs/pipeline-notifications.md`](docs/pipeline-notifications.md) | fixed-template safe rendering, local review/digests, durable per-event and aggregate rate limits |
| [`docs/legacy-state.md`](docs/legacy-state.md) | manual collector runtime state and safe migration out of tracked `memory/` |
| [`docs/agent-merge.md`](docs/agent-merge.md) | `agent-merge-queue` / `agent-merge-approve`: separate automatic and approved trees, exact-OID owner publication |
| [`docs/agent-merge-review.md`](docs/agent-merge-review.md) | advisory local-model scripts-queue review, pre-queue allowlist rejection, owner-readable reports |
| [`docs/agent-merge-console.md`](docs/agent-merge-console.md) | prepared LATER-01 owner Merge/Deny UI extension, exact-OID decisions, audit, and CTL-07/09 integration gates |
| [`docs/sudo-broker.md`](docs/sudo-broker.md) | `sudo-broker` / `sudo-ask`: per-command root approval from an open terminal, replacing standing `NOPASSWD` |
| [`docs/audit.md`](docs/audit.md) | `diary-audit` / `diary-auditd`: trusted metadata-only journal writer, duplicate counters, baseline/review reports |
| [`docs/production-baseline.md`](docs/production-baseline.md) | `diary-production-baseline`: AUD-05 coverage, duration, finding classification, and owner-review evidence |
| [`docs/lock-controller.md`](docs/lock-controller.md) | `diary-lock` / `diary-lockd`: durable peer-authenticated state, failsafe units, watchdog, and local break-glass |
| [`docs/lock-dashboard.md`](docs/lock-dashboard.md) | `diary-lock-dashboard`: unprivileged read-only owner UI/API and least-privilege status/report access |
| [`docs/network-audit.md`](docs/network-audit.md) | `diary-network-auditd`: accept-only UID/NFLOG flow observation forwarded to the trusted audit writer |
| [`docs/dns-audit.md`](docs/dns-audit.md) | `diary-dns-auditd`: audit-only per-identity DNS forwarding and direct/alternate-resolution observation |
| [`docs/fresh-unlock.md`](docs/fresh-unlock.md) | `diary-fresh-unlock`: forced-login, browser-bound, single-use owner authorization boundary |
| [`docs/retired-notes-shopping.md`](docs/retired-notes-shopping.md) | removed 2026-10: the `/notes/` and `/shopping/` pages |

> **`scripts/` is a stopgap name.** Once we add scripts that aren't
> mail-related, this whole folder will get split by subsystem (e.g.
> `mail/`, …). Don't write naming around the current layout.

---
