# Read-only lock dashboard

`diary-lock-dashboard` serves the CTL-09 owner dashboard at `/lock/` and its JSON
snapshot at `/lock/api/status`. Caddy forwards both only after the existing
Authelia `lock-owner` `two_factor` policy succeeds. There is no TCP listener:
Caddy connects to `/run/diary-lock-dashboard/http.sock`. The runtime directory is
`diary-lock-dashboard:caddy` mode `0750`, the socket is mode `0660`, and the
service verifies the kernel `SO_PEERCRED` UID against the actual `caddy` account
before trusting `Remote-User` and `Remote-Groups`. A non-Caddy local process
cannot forge those headers to read the dashboard, and even Caddy must supply the
owner headers established by Authelia. DSH/Kimi cookies and tokens are not
accepted.

The page prominently states **Enforcement: dark / audit-only**. Controller state
is not represented as active containment before ACT-01. The page shows the
controller's authoritative `LOCKING`/`LOCKED`/`ATTENDED` state, attendance expiry,
component availability, and the fixed 24-hour baseline report grouped into
network, DNS, broker, and other findings. Its report also contains the CTL-07
`review_id`, `incomplete` marker, and AUD-05-classified `required_groups` from the
same trusted review provider used for unlock decisions. `reason` and `unit` are
preserved in both baseline and required groups for owner context and
expected-flow matching.

The dashboard mounts `/lock/workflow.js` in `#lock-workflow` and loads
`/lock/dashboard.js`, which first fetches `/lock/api/status` and only then calls
`DiaryLockWorkflow.mount` with `getReport`, `getState`, and a GET-only snapshot
`refresh`. The fetch is deliberately independent of the workflow: a missing or
throwing workflow can only blank its own panel, never the status call. CSP permits scripts and
connections only to self; there is no inline workflow script, third-party asset,
or WebSocket. Page refresh is still read-only GET every 30 seconds and never
extends attendance. Controller or audit failures render as unavailable, never
as healthy or as an empty report. Empty categories refer only to returned
records; a truncated report produces a prominent warning that omitted records
may contain findings.

## Least-privilege read boundary

The service runs as the unprivileged `diary-lock-dashboard` account with no
capabilities, no durable writable state, and `AF_UNIX` as its only permitted
address family. Its supplementary `diary-lock-control` and `diary-audit`
memberships permit socket connections, not unrestricted commands. Each root
daemon checks the kernel peer identity and restricts this account independently:

- Combined `diary-lockd --dashboard-user diary-lock-dashboard --ui-user
  diary-lock-ui` allows the dashboard only the exact `{"cmd":"status"}` request
  while separately authorizing UI `unlock`/`extend`. Dashboard `lock`,
  `break_glass`, `unlock`, `extend`, and extra fields are rejected without a
  transition or failsafe.
- Combined `diary-auditd --dashboard-user diary-lock-dashboard --ui-user
  diary-lock-ui` allows both accounts only `baseline` and `review`. `write` and
  `ping` are rejected for each, so neither web identity can create audit events
  or heartbeat writes. Neither has native journal access.

The dashboard web process has fixed socket paths and fixed report arguments and
implements only GET/HEAD. HTTP clients cannot select commands, report
expressions, controller operations, or firewall operations. State changes exist
only in the separately authenticated CTL-07 workflow service and its explicit
browser actions, not in dashboard snapshot code.

## Installation and live verification

After both repositories are owner-approved and synchronized, install the service
and then the reviewed Caddy configuration. **Run the installer only in a
controlled lock window:** it restarts `diary-lockd`, whose startup always performs
a controlled relock and persists `LOCKED`. Any current `ATTENDED` interval ends
immediately; this is installer behavior, not a dashboard-initiated transition.

```bash
sudo-ask /home/kek/diary-approved/systemd/install-dashboard-root
sudo-ask /home/kek/diary-approved/caddy/update_config
```

The dashboard installer refreshes the approved controller/audit readers, checks
the loaded capability-free unit, and proves the service identity can read status
and reports but cannot lock, write an event, or ping the audit journal. It also
rejects forged owner headers from root and owner peers and rejects a Caddy peer
that lacks authenticated owner headers. Denial checks alone also pass when Caddy
cannot reach the socket at all (mode, SELinux label, or a dead snapshot reader),
so the installer additionally proves the positive Caddy path: as the `caddy`
account, with the owner headers Caddy forwards after Authelia, it must read
`/lock/api/status` and render `/lock/` with the dark enforcement label. The owner
peer probe proves filesystem denial, because that account cannot traverse the
runtime directory. That positive probe is still a DAC/functionality check: the
installer's `runuser` client runs in the installer's SELinux domain, not in
`httpd_t`, so it cannot reproduce Caddy's confined transition. SELinux is
asserted separately — the runtime directory must carry `httpd_var_run_t`, the
persistent file-context rule must exist so a reboot recreates it, and the
`diary_lock_dashboard_caddy` module must be loaded. The installer does not
configure or enforce firewall policy.

Before CTL-07 is deployed, `/lock/workflow.js` is not served. The page renders
its read-only snapshot server-side, `dashboard.js` still fetches
`/lock/api/status` first, and the workflow panel then states explicitly that the
owner workflow asset is unavailable. Neither a missing nor a throwing workflow
script can abort the status path or the rest of the page.

Complete these deployed checks before deleting CTL-09:

1. Complete password + user-verified WebAuthn authentication and open `/lock/`.
   Compare the displayed state, expiry, counts, and grouped metadata with
   `diary-lock status` and `diary-audit baseline --since 24h --json` run as the
   owner. Compare `/lock/api/status` with the same trusted responses.
2. Verify anonymous, first-factor-only, non-owner, DSH, and Kimi sessions cannot
   retrieve the page or API through Caddy. Confirm the existing Authelia policy
   still requires `lock-owner` and `two_factor` for every `/lock/` path. Repeat
   the installer's forged-header probes as unauthorized root/owner peers and
   verify a Caddy peer without owner headers is also denied.
3. Stop or disconnect each source in a controlled window. The corresponding
   component and every findings section must say unavailable; restoring it must
   restore the data. Do not interpret an unavailable source as locked or empty.
4. Verify `systemctl show diary-lock-dashboard -p User -p Group
   -p CapabilityBoundingSet -p AmbientCapabilities -p RestrictAddressFamilies`,
   confirm the Caddy-only runtime directory/socket modes, and repeat the
   installer's denied `lock`, `break-glass`, audit `write`, and audit heartbeat
   attempts as `diary-lock-dashboard`. Confirm no state transition or audit event
   occurred and no fresh-authorization endpoint was called.
5. Confirm the service has no firewall authority and no firewall command appears
   in its executable or unit. General-agent enforcement must remain dark.

Local regression tests use fake trusted readers and scratch sockets only; they
do not install units, change the host, or exercise real controller transitions.
