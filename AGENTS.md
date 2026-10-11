# AGENTS.md — diary-scripts

Public, sanitized helper-code repo for the diary workspace (the private parent repo lives at `~/diary` and embeds this one as a submodule). Tools are console entry points run under `uv run` — see `README.md` and `scripts/README.md` for per-tool docs and SOPs.

## Worktrees

**All edits happen in a worktree.** The checkout at `~/diary/diary-scripts` is the diary repo's submodule and is reserved for merges and pushes only — never edit tracked files there. Work beside the repo:

    git -C ~/diary/diary-scripts worktree add ~/diary-scripts-worktrees/<slug> -b dev/<slug>

Full policy (branch naming, review-before-merge, merge + cleanup): `~/diary/AGENTS.md` §Worktrees. After merging here, bump the submodule pointer on the diary side — and always push **this repo before** pushing diary, so the pointer never dangles.

## Red lines

- **No PII, ever.** Real hostnames, family names, and addresses must never appear here — not in code, and not in comments, docstrings, or examples either. Hosts come from `mail_reader.config` (`hosts.*` config keys + helper functions like `embed_url()`); examples use the `gpu-host` / `server` placeholders. New code that needs a host adds a `hosts.*` key plus a config helper — never a hardcoded default, not even a placeholder default that silently shadows a missing env var.
- Code that needs the workspace path calls `mail_reader.config.workspace_root()` — never walk `__file__`.
- Private runtime values (real hosts, family names, coordinates) belong in the diary repo's `config/local.toml`, not here. See `~/diary/CLAUDE.md` for the public/private split.

## Working here

- Run everything as `uv run <entry-point>` from the repo root — never bare `python3` (it misses the venv deps like psycopg).
- Tests: `uv run pytest` (doctests run too, via `--doctest-modules`). Run them before merging.
- New CLI tools: entry in `[project.scripts]`, a docstring naming the purpose, sensible flags, and a section in `scripts/README.md`.
- Commit convention: `TOOLS:` prefix + a `Generated-with:` trailer naming harness, model, and settings (see `~/diary/AGENTS.md` §Commits).
