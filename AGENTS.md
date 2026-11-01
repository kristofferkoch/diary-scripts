# AGENTS.md — diary-scripts

Public, sanitized helper-code repo for the diary workspace (the private parent repo lives at `~/diary` and embeds this one as a submodule). Tools are console entry points run under `uv run` — see `README.md` and the `scripts/README.md` doc index (per-tool docs live in `scripts/docs/`).

## Worktrees

**All edits happen in a worktree.** The integration checkout at `~/diary/diary-scripts` is reserved for owner-approved synchronization — never edit tracked files there. Use the agent-owned standalone source and a sibling scripts worktree under the one agent-writable Git root. Source and integration have independent `.git` directories, objects, refs, logs, worktree registrations, and config.

The installer normally creates the source. If bootstrapping it manually, clone once without hardlinks:

    install -d -m 2775 ~/diary-worktrees/_sources
    git clone --no-hardlinks ~/diary/diary-scripts ~/diary-worktrees/_sources/diary-scripts

Fetch from the read-only integration origin, rebase the worktree, and create new worktrees only from `_sources`:

    git -C ~/diary-worktrees/_sources/diary-scripts fetch --no-tags origin
    git -C ~/diary-worktrees/<slug>-scripts rebase origin/master
    (umask 002; git -C ~/diary-worktrees/_sources/diary-scripts worktree add ~/diary-worktrees/<slug>-scripts -b dev/<slug> origin/master)

Do not use `~/diary-scripts-worktrees`: the web agents' systemd confinement makes it read-only. Leave the diary branch's submodule uninitialized and run code and tests in this sibling worktree. The corresponding diary source is `~/diary-worktrees/_sources/diary`; its worktree is `<slug>`. Stage a reviewed gitlink in that diary worktree without initializing the submodule:

    git -C ~/diary-worktrees/<slug> update-index --cacheinfo "160000,<sha>,diary-scripts"

Full policy (branch naming, review-before-merge, publication + cleanup): `~/diary/AGENTS.md` §Worktrees. `agent-merge-request` performs the source clean precheck, resolves the full expected OID, creates a bundle, and removes the bundle, source worktree, and source branch after success. The kek service reads only the bundle and never runs Git in agent sources. After approval here, bump the submodule pointer on the diary side — and always push **this repo before** pushing diary, so the pointer never dangles.

## Red lines

- **No PII, ever.** Real hostnames, family names, and addresses must never appear here — not in code, and not in comments, docstrings, or examples either. Non-secret hosts come from fixed root-owned world-readable `/etc/diary/hosts.toml` via the host URL helpers; privileged collector settings come from fixed `root:kek` `0640` `/etc/diary/trusted.toml` via `mail_reader.config.trusted_cfg`. Examples use the `gpu-host` / `server` placeholders. New code that needs a host adds an approved `hosts.*` key plus a config helper — never a hardcoded default, env override, or agent-publishable fallback.
- Code that needs the workspace path calls `mail_reader.config.workspace_root()` — never walk `__file__`.
- Ordinary private runtime values (family names, coordinates) belong in the diary repo's `config/local.toml`; real non-secret hosts belong in `/etc/diary/hosts.toml`, while privileged collector settings belong in `/etc/diary/trusted.toml`, not here. Spond/Vigilo/Skolemelding legacy cursors, JSONL, and attachments belong in `/var/lib/diary/legacy-state`; trusted selection state belongs in `/var/lib/diary/collector-state`, and immutable events in `/var/lib/diary/events`. Never write those legacy files into tracked `memory/`. See `~/diary/AGENTS.md` for the public/private split.

## Working here

- Run everything as `uv run <entry-point>` from the repo root — never bare `python3` (it misses the venv deps like psycopg).
- Tests: `uv run pytest` (doctests run too, via `--doctest-modules`). Run them before merging. PostgreSQL integration tests opt in with an administrative `DIARY_TEST_PG_DSN` that can create databases and install `vector`; they create, migrate, and drop a unique database, never use an application database.
- New CLI tools: entry in `[project.scripts]`, a docstring naming the purpose, sensible flags, and a doc under `scripts/docs/` listed in the `scripts/README.md` index.
- Commit convention: `TOOLS:` prefix + a `Generated-with:` trailer naming harness, model, and settings (see `~/diary/AGENTS.md` §Commits).
