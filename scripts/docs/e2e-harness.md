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

## Layer 2 — the browser

Layer 2 puts the console in the middle of the same scratch stack and drives it
with a real Chromium. It exists because three production failures were invisible
to hand-built requests: the submit button's own field, an `Origin` the browser
omits or sends as `null`, and a form rendered before the ceremony.

```bash
# one-time provisioning, outside the repo
npm install --prefix ~/.cache/diary-e2e playwright-core

E2E_PLAYWRIGHT_CORE=~/.cache/diary-e2e/node_modules/playwright-core/index.mjs \
E2E_CHROMIUM=~/.cache/ms-playwright/chromium-1223/chrome-linux/chrome \
PYTHONPATH=. uv run pytest -q scripts/test_e2e_console_browser.py
```

The file skips itself when either variable is unset, so the normal suite stays
network-free and browser-free. It runs in about 15 seconds.

What is real in this layer:

| Piece | Reality |
| --- | --- |
| Console host | the real `diary_lock_console_ui.ConsoleServer` on loopback, configured with the proxy origin |
| Executor transport | the real Unix-socket protocol (`ExecutorClient` ↔ a Unix stream server that calls `handle_request`) |
| Browser | Chromium via `playwright-core`; the rendered forms are clicked, not posted by hand |
| Same-origin front | a loopback proxy; it rewrites `Origin` only for the opaque-origin scenario |
| Ceremony | a stand-in service that issues the transaction cookie and accepts exactly one consume, so the two-click flow, replay, and stale grants are exercised |
| Authelia | a forward-auth stand-in answering only for a registered owner session |

Deliberately different, and why:

- **Real Authelia and Caddy are L3.** This layer checks the console's own
  browser-facing behaviour; redirects, cookies, and forward-auth seams are L3.
- **The genuine WebAuthn ceremony is not performed.** The stand-in plays the
  service, not the authenticator; the virtual-authenticator ceremony is an L3
  concern.
- **The audit transport is L1's.** The console writes through the same service
  the socket handler calls; the socket peer check is L4.

## Coverage against the production failures

| Production failure | Covered by |
| --- | --- |
| Audit daemon dropped `repository` (KeyError, empty reply, "unknown outcome") | **L1**: `test_audit_daemon_journals_the_decision_repository` fails with the exact `KeyError` on the pre-fix code and passes after the field map fix |
| Merge published nothing / stale pair accepted | **L1**: `test_merge_publishes_the_scratch_queue`, `test_stale_queue_oid_is_refused_without_publication` |
| Cross-repository grant accepted | **L1**: `test_scripts_grant_cannot_decide_the_diary_queue`; **L2** replays it through the console |
| Audit state lost or refusing writes after a restart | **L1**: `test_audit_restart_keeps_state_and_accepts_writes` |
| `AuthorizationError` rendered as 503 | **L2**: every scenario asserts the page, not an outage |
| `Origin` missing and `Origin: null` | **L2**: `test_opaque_origin_is_accepted`; the missing-header path is the same code branch |
| Submit button field (`confirm`) rejected | **L2**: `test_rendered_form_submits_the_button_field` |
| Two-click ceremony published twice or not at all | **L2**: `test_two_click_ceremony_publishes_once` |
| Stale form treated as an attack | **L2**: `test_stale_form_re_renders_instead_of_refusing` |
| Replayed grant accepted | **L2**: `test_replayed_grant_cannot_publish_twice` |
| Deny published something | **L2**: `test_deny_publishes_nothing_and_audits` |
| Installed helper missing (`agent_merge_pii`), module-level `mail_reader` import | installer smoke test in `install-owner-ui-root`; **L4** for the installed layout |
| Stale verifier socket, installer ordering | **L4** (root rehearsal) |

Evidence that the browser layer is sensitive to the real bugs: run against the
console module from `a5ad908` (before the `Origin` and submit-button fixes) and
six of the seven scenarios fail; they pass on the fixed module. The one that
passes either way is `test_foreign_origin_is_refused`, which asserts a refusal
that the pre-fix code produced for everything.


## Red-green rule

An e2e-discovered bug is not fixed until it has both:

1. **a unit-level regression test** at the lowest layer that can reproduce it —
   no browser, no subprocess where avoidable — shown failing before the fix and
   passing after; and
2. **the e2e scenario that found it**, kept as the end-to-end guard.

The browser/e2e test is the guard, not the regression test: it is slow, it needs
provisioning, and it says "something in this path is broken", not "this function
is wrong". The unit test is what pins the behaviour.

To demonstrate red, stash only the source change (keep the tests) and run both:

```bash
git stash push -- scripts/<fixed-module>.py     # fixes away, tests stay
uv run pytest -q scripts/test_<unit>.py -k <case>        # must fail
E2E_PLAYWRIGHT_CORE=... E2E_CHROMIUM=... \
  uv run pytest -q scripts/test_e2e_console_browser.py -k <case>   # must fail
git stash pop
```

Today's bugs and the tests that keep them fixed:

| Failure | Unit regression | E2E guard |
| --- | --- | --- |
| Audit daemon dropped `repository` in the journal map | `test_decision_repository_reaches_the_journal`, `test_every_accepted_event_field_has_a_journal_mapping` | **L1** `test_audit_daemon_journals_the_decision_repository` |
| Publication failure reported as "unknown outcome" (D2) | `test_audit_failure_before_publication_reports_nothing_published`, `test_publisher_failure_before_any_change_reports_not_published`, `test_publisher_failure_after_publication_reports_published`, `test_publisher_failure_after_the_queue_moved_reports_unknown`, `test_executor_replies_distinguish_publication_states` | **L1** `test_failed_publication_reports_the_repository_state` |
| Browser-facing authorization failure returned JSON (D4) | `test_merge_return_failure_renders_html_for_the_browser`, `test_merge_confirmation_page_states_the_expiry` | **L2** `test_expired_ceremony_renders_a_page_not_json` |
| `Origin` omitted or opaque `null` refused | `test_decide_accepts_a_browser_that_omits_origin`, `test_decide_accepts_an_opaque_null_origin`, `test_merge_begin_accepts_an_opaque_origin` | **L2** `test_opaque_origin_is_accepted` |
| Submit button's `confirm` field rejected | `test_decision_with_the_submit_button_field_is_accepted` | **L2** `test_rendered_form_submits_the_button_field` |
| Stale form after the ceremony | `test_stale_review_form_re_renders_instead_of_dead_ending` | **L2** `test_stale_form_re_renders_instead_of_refusing` |
| Stale verifier socket crash-loop | `test_stale_verify_socket_is_discarded`, `test_live_verify_socket_is_not_discarded` | **L1** restart scenario |
| Approval fetch required discarded submodule commits | `test_sync_does_not_require_historical_submodule_commits` | none (approval tooling, not the console path) |
| Preflight read raced journald | `test_preflight_polls_until_the_journal_flushes` | **L4** installer rehearsal |
| Console imports outside the deployed helper set | `test_console_and_executor_import_from_the_deployed_helper_set` | **L4** installed layout |

## Still missing

1. **L3**: real Caddy and Authelia, including a virtual-authenticator WebAuthn
   ceremony and the forward-auth redirect seams.
2. **L4**: the VM rehearsal runs the installers in order with a real kernel and
   SELinux enforcing, but socket peer authorization, the host's exact package
   set, and the live edge stay outside it (see the layer-4 gaps).

## Layer 3 — the real edge

Layers 1 and 2 stand in for the edge. This layer runs the edge itself: the
pinned Authelia build with a generated scratch configuration (file backend,
`two_factor` on `/lock`, the production cookie name and flags), real Caddy with
`forward_auth` in front of the console, a self-signed certificate for a scratch
host, and Chromium with a CDP virtual authenticator. The passkey is registered
through Authelia's own UI — including the one-time-code identity verification,
read from the scratch notifier file — so a real WebAuthn assertion reaches the
server and the session really becomes two-factor.

`scripts/e2e/edge.py` builds the stack; `scripts/e2e/edge_driver.mjs` drives the
browser; `scripts/test_e2e_edge.py` holds the scenarios:

- anonymous request to `/lock/merge/` → 302 to the portal, never the console;
- password-only session → still denied;
- session cookie carries `Secure`, `HttpOnly`, and `SameSite=Strict`;
- full password + passkey → the console page renders;
- a browser `Merge` through Caddy and Authelia → exactly one publication.

Provisioning: `caddy`, `authelia`, `openssl`, `node`, and `playwright-core`
against a cached Chromium. The tests skip when `E2E_PLAYWRIGHT_CORE` or
`E2E_CHROMIUM` are unset, and skip the whole module when the binaries are
missing.

Not covered: the installed units, socket peer authorization, SELinux labels,
installer ordering, and the fresh-authorization service's own session proof
against Authelia (the layer-3 stack uses the layer-2 ceremony stand-in, so the
grant is minted by the harness). Those are L4.

## Red-green rule

An e2e-discovered bug is not fixed until **both** exist:

1. a unit-level regression at the lowest layer that reproduces it and fails
   first, and
2. the e2e scenario that found it, as the end-to-end guard.

The unit test is the fast local proof and survives harness refactors; the e2e
scenario is what proves the seam. A fix with only the e2e scenario is not
accepted, and a bug found in production is not closed until the scenario that
would have caught it exists.

| production failure | unit regression | e2e scenario | both |
| --- | --- | --- | --- |
| installed helper list missing `agent_merge_pii` | none | none (L4) | no |
| `mail_reader` imported by the console review module | `test_console_deployment_layout` | L2 boots the console | yes |
| authorization failure reported as 503 | `test_anonymous_page_asks_for_authorization_instead_of_reporting_an_outage` | L3 first-factor denial | unit only |
| `Origin` omitted by the browser | `test_decide_accepts_a_browser_that_omits_origin` | L2 opaque-origin scenario | partial |
| `Origin: null` (Firefox) | `test_decide_accepts_an_opaque_null_origin`, `test_merge_begin_accepts_an_opaque_origin` | L2 opaque-origin scenario | yes |
| submit button's `confirm` field | `test_decision_requires_same_origin_explicit_form_and_current_token` | L2 rendered-form scenario | yes |
| audit field map dropped `repository` | `test_decision_repository_reaches_the_journal`, `test_every_accepted_event_field_has_a_journal_mapping` | L1 audit scenario | yes |
| preflight read the journal before the flush | `test_preflight_polls_until_the_journal_flushes` | none (L4) | unit only |
| `doctor` fell back to `default_paths()` | `test_doctor_requires_an_explicit_approved_checkout` | none (L4) | unit only |
| grant burned before the pair was checked | `test_stale_pair_is_refused_before_the_grant_is_consumed` | L2 stale-pair scenario | yes |
| transaction created after logout | `test_begin_capacity_failure_leaves_the_owner_signed_in` | none | unit only |

Rows without both columns are the L4 checklist: the installer rehearsal must
exercise the helper layout, the deployment order, the functional preflight, and
the client comparison against a real installation.

## Layer 4 — the installer rehearsal

Layers 1-3 run the code against scratch state. They cannot see an installed
layout, the system Python, systemd ordering, SELinux labels, or a preflight that
passes while the component it checks is broken — which is how the helper-list
crash and the `repository`-field deployment both reached production. Layer 4 runs
the real installers, in their documented order, **inside a Fedora guest**: real
systemd, SELinux enforcing, a real kernel and the real package set, none of which
a container reproduces.

```bash
scripts/e2e/installer_rehearsal.sh \
    --approved-diary   /home/kek/diary-approved \
    --approved-scripts /home/kek/diary-scripts-approved
```

It needs qemu on the host (`qemu-system-aarch64`, `qemu-img`, `cloud-utils`,
`edk2-aarch64`) and network access, because the fresh-authorization installer
downloads and builds pinned Authelia. Run it as the owner account: it reads the
approved checkouts' private `.git` metadata.

### Substrate

`scripts/e2e/vm.sh` boots a Fedora Cloud guest and drives it over ssh on a
loopback port. The approved trees are mounted **read-only as 9p shares** at the
exact paths the installers hardcode, so nothing is copied into the guest and
nothing is written back to the checkouts.

- **Base image, built once.** The Fedora Cloud Base qcow2 (checksum-verified
  against the published `CHECKSUM` file) is booted, provisioned with the toolchain
  the installers need, powered off, and converted into
  `$XDG_CACHE_HOME/diary-e2e/prepared-<release>.qcow2`. Every run after that is a
  throwaway qcow2 overlay of that image: created in milliseconds, no package
  installation per run.
- **Acceleration.** `--accel auto` (the default) uses KVM when `/dev/kvm` is
  writable and TCG otherwise. On arm64 KVM in the guest needs the outer
  hypervisor to put this host at EL2; without that there is no `/dev/kvm` and the
  run is TCG-only. The guest, the installers and the assertions are identical —
  only the runtime changes. Each run records `SUBSTRATE accel=…` in the summary
  so a slow TCG run is never mistaken for a hang.
- **Interface.** The rehearsal keeps `--approved-diary`, `--approved-scripts` and
  `--keep`, and adds `--accel` and `--cache-dir`.

What it does:

1. Refuses to start unless both checkouts are clean Git trees and their `.git` is
   readable by the invoking account.
2. Ensures the prepared base image exists, then boots a fresh overlay and waits
   for systemd to report `running` or `degraded`.
3. Mounts the approved trees read-only and asserts the substrate properties the
   container could not provide: SELinux `Enforcing`, a virtio block device (a real
   kernel), and a working nftables.
4. Runs, in order: `systemd/install-audit-root`,
   `systemd/install-controller-root`, `authelia/install`,
   `systemd/install-owner-ui-root` — from the approved tree, with each installer
   bounded by a timeout and its output kept in the scratch directory. The
   controller step is required because the console installer restarts and checks
   `diary-locked`, which the controller installer owns.
5. Asserts the audit daemon, controller and console are active; `/lock/health`
   answers 200; the unlock route answers 404; the UI enablement file and the
   enforcement arm file are absent; `diary-audit preflight` passes;
   `agent-merge-queue doctor` reports the installed client matches approved; and
   the boot has **no SELinux `avc: denied`** entries.
6. **Negative case**: builds a fixture from the approved scripts with only the
   `repository` journal mapping removed — the exact production defect, with the
   validator and the preflight subcommand intact — installs that audit daemon and
   the controller, then asserts that `install-owner-ui-root` refuses with "does
   not journal the repository field" and leaves the console unit unenabled. That
   is the assertion a grep-based preflight cannot make.

Every check prints `PASS` or `FAIL`; the exit status is non-zero if any installer
refused or any assertion failed. `--keep` leaves the guest disks, console logs
and scratch directory for inspection.

### Honest gaps

- **The guest is the Fedora Cloud image of the same release, not this host's
  install.** Package versions match the Fedora 44 line and the system Python is
  asserted at 3.11+, but the host's extra packages (and any RHEL-family
  difference) are not present.
- **SELinux is enforced, but with the guest's own policy.** Labels, file contexts
  and AVC denials are real; whether the host's policy build matches is not proven.
- **Socket peer authorization** is exercised only as root inside the guest; the
  host's UID mapping and group policy are not.
- **No host services are touched**: this is not a reboot, lock-transition, or
  tailnet test, and Caddy/Authelia's live routing stays with L3.
- The fresh-authorization service's own session proof against Authelia is L3's
  stand-in, not the installed unit.
- **TCG says nothing about performance.** Without `/dev/kvm` the guest runs in
  software emulation, so the runtime is not a production measurement.

### Manual checklist after a real deployment

The rehearsal cannot cover these; verify them by hand after deploying:

1. `systemctl is-active` for the audit daemon, controller, console and executor
   socket, then `/lock/health` through Caddy over the tailnet origin.
2. The console page renders behind two-factor Authelia, and an anonymous request
   is redirected, not served.
3. One real merge through the page: the root-served confirmation page names the
   repository and both OIDs; the trusted journal shows `merge_console.decision`
   with `repository`, the queue OID and the base OID.
4. `/etc/diary/lock-ui-actions-enabled` is still absent and a UI unlock is
   refused; `/lock/api/lock` returns 404.
5. SELinux labels on the dashboard socket and the Caddy connect rule.
6. A controlled reboot: services come back, the controller starts locked, and the
   lock audit records a `reason=boot` transition.
