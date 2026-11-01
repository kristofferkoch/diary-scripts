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
network, DNS, broker, and other findings. It refreshes only by GET every 30
seconds. Controller or audit failures render as unavailable, never as healthy or
as an empty report. Empty categories refer only to returned records; a truncated
report produces a prominent warning that omitted records may contain findings.

## Least-privilege read boundary

The service runs as the unprivileged `diary-lock-dashboard` account with no
capabilities, no durable writable state, and `AF_UNIX` as its only permitted
address family. Its supplementary `diary-lock-control` and `diary-audit`
memberships permit socket connections, not unrestricted commands. Each root
daemon checks the kernel peer identity and restricts this account independently:

- `diary-lockd --dashboard-user diary-lock-dashboard` allows only the exact
  `{"cmd":"status"}` request. `lock`, `break_glass`, extra fields, and attendance
  changes are rejected without invoking a transition or failsafe.
- `diary-auditd --dashboard-user diary-lock-dashboard` allows only `baseline` and
  `review`. `write` and `ping` are rejected, so the dashboard cannot create audit
  events or heartbeat writes. It has no native journal access.

The web process has fixed socket paths and fixed report arguments. HTTP clients
cannot select commands, report expressions, controller operations, or firewall
operations. Only GET and HEAD are implemented; there are no forms or lock,
unlock, acknowledgement, attendance-extension, fresh-authorization, or consume
controls. CTL-07 owns any later state-changing workflow.

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
that lacks authenticated owner headers. It does not configure or enforce
firewall policy.

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
