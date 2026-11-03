# End-to-end harness for the owner merge console

> Index: [scripts/](../README.md)

The console was debugged live because its failures only appeared once a real
browser, a real audit daemon, and real Git repositories were involved. Layer 1
below reproduces that stack unprivileged and against scratch state.

## Isolation contract

- Everything a test writes lives under one `DIARY_E2E_ROOT` (a `tmp_path` per
  test). `Scratch.path()` refuses a path that resolves outside it, and
  `root_escapes()` refuses the production prefixes listed in
  `scripts/e2e/harness.py` (`/home/kek/diary*`, `/etc/diary`, `/run/diary-*`,
  `/var/lib/diary`, the installed `/usr/local` binaries, the system journal).
- No `sudo`, no `systemctl`, no `nft`, no `/etc`, no root. A component that
  cannot run unprivileged is documented as an L4 gap, never faked.
- The real code is imported from the worktree, not from `/usr/local`.

Run it with:

```bash
PYTHONPATH=. uv run pytest -q scripts/test_e2e_merge_console.py
```

The file runs in about two seconds and needs no network.

## What runs for real

| Piece | Reality |
| --- | --- |
| `diary_audit.AuditService` | validation, `DedupStore`, journal field map, native-journal datagram (through `JournalWriter` to a Unix-datagram sink) |
| `diary_merge_console_service.handle_request` | the executor's decide path, including the single-use grant check and `approve_queue` |
| Git publication | scratch queue/approved/integration checkouts with a bare origin, so fetch, fast-forward, synchronisation, and push are real |

Deliberately different from production, and why:

- **The audit socket is bypassed.** `AuditDaemon.authorized` refuses a writer
  whose UID resolves to `agent`; an unprivileged test cannot present another
  UID. The harness calls the same `AuditService.write` the handler calls, with
  the test process's real peer credentials.
- **The daemon wrapper is not started.** `daemon_main` requires root; the class
  it constructs is used directly.
- **No venv sync.** Publication uses `sync_environment=False`, matching what the
  console does for the diary repository (the CLI does the same per repository).

## Coverage against the production failures

| Production failure | Covered by |
| --- | --- |
| Audit daemon dropped `repository` (KeyError, empty reply, "unknown outcome") | **L1**: `test_audit_daemon_journals_the_decision_repository` fails with the exact `KeyError` on the pre-fix code and passes after the field map fix |
| Merge published nothing / stale pair accepted | **L1**: `test_merge_publishes_the_scratch_queue`, `test_stale_queue_oid_is_refused_without_publication` |
| Cross-repository grant accepted | **L1**: `test_scripts_grant_cannot_decide_the_diary_queue` |
| Audit state lost or refusing writes after a restart | **L1**: `test_audit_restart_keeps_state_and_accepts_writes` |
| Installed helper missing (`agent_merge_pii`), module-level `mail_reader` import | installer smoke test in `install-owner-ui-root` (deployment); L1 imports from the worktree and cannot see the installed layout |
| `AuthorizationError` rendered as 503 | **L2** (browser): needs the console server and the fake Authelia backend |
| `Origin` missing and `Origin: null` | **L2** (browser): only a real browser sends these |
| Submit button field (`confirm`) rejected | **L2** (browser): the rendered form must be submitted as a browser submits it |
| Stale verifier socket, installer ordering | **L4** (root rehearsal): the fresh service chowns its socket and the installers must run as root |

## Layer 2 must add

1. The console server (`diary_lock_console_ui.ConsoleServer`) behind a scratch
   HTTP origin, plus the fake Authelia forward-auth server already used by
   `test_diary_fresh_unlock.py`.
2. A browser driver (Playwright) that loads the rendered page and clicks the
   real forms, so `Origin`, `Sec-Fetch-Site`, the submit button, the two-click
   ceremony, and stale/replayed grants are exercised as a browser produces them.
3. A deployment rehearsal for the installers (L4) that runs the root-only
   wiring in a scratch tree or a container and checks the preflight ordering.
