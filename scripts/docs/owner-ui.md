# Independent owner review, unlock, and attendance UI

> **Split notice.** The deployed CTL-07 host is now the merge/deny console
> (`diary_lock_console_ui.py`, documented in `agent-merge-console.md`). It
> registers no unlock, acknowledgement, or attendance route. The workflow
> described below is retained as the source for CTL-11, which owns unlock and
> attendance, and requires the controller's root-only
> `/etc/diary/lock-ui-actions-enabled` file; the controller refuses UI
> unlock/extend while that file is absent. This module is no longer installed by
> `install-owner-ui-root`.

`diary-lock-ui` is the CTL-07 state-changing extension behind Caddy and Authelia.
It consumes the existing trusted audit reports and CTL-08 fresh authorization;
it does not implement a separate read-only dashboard or controller. General-agent
enforcement remains dark until its own cards and ACT-01 are complete.

## CTL-09 extension contract

CTL-09 owns `/lock/`, authoritative state/health/status, grouped report
visualization, and read-only API routes. CTL-07 exposes only `/lock/workflow.js`,
`/lock/api/lock`, `/lock/api/extend`, `/lock/unlock/acknowledge`, and
`/lock/unlock/confirm` in addition to the existing CTL-08 routes. It does not
register a page, status/report GET, stylesheet, or wildcard API route that could
shadow the dashboard.

The dashboard embeds an element and `/lock/workflow.js`, then calls
`DiaryLockWorkflow.mount({element, getReport, getState, refresh})`. `getReport`
returns the dashboard's current `{review_id, incomplete, required_groups}`;
`getState` returns its authoritative controller state; `refresh` reloads those
trusted read-only views after an action. The extension adds only lock, fresh
authorization, grouped acknowledgement, unlock confirmation, and attendance
controls. It never fetches the dashboard's read-only endpoints and never polls.
The server independently re-fetches trusted reports for authorization decisions,
so browser-supplied review IDs or fingerprints alone are not authority. The
shared `review_id` is SHA-256 of compact canonical JSON
`{"since": controller_review_since_ISO, "groups": [sorted_required_fingerprints]}`;
`required_groups` use the AUD-05 classification below, not every baseline group.

## Trust boundaries

The root-owned installed service runs as the dedicated unprivileged
`diary-lock-ui` identity. It has no firewall, unit-management, repository, DSH,
or Kimi authority. Its only privileged access is membership in `diary-audit` for
fixed reports and `diary-lock-control` for the peer-authenticated controller
socket. The controller accepts `unlock` and `extend` only from that kernel UID;
root and the local owner retain `lock` and initial break-glass but cannot use the
UI-only commands. Agent and pipeline identities are not authorized.

Caddy applies Authelia `two_factor` to the UI routes, but the service does not
trust client-supplied `Remote-*` headers. It independently sends only the
configured Authelia session cookie to the loopback forward-auth endpoint and
requires the configured owner and `lock-owner` group. A direct loopback request
with spoofed headers, a DSH/Kimi cookie, or a bearer token therefore has no
authority.

## Review and acknowledgement

For each authorization decision, the workflow asks the controller for the start
of the current lock window and requests the existing fixed `review` report from
`diary-auditd`; CTL-09 separately owns baseline/read-only visualization. Review
findings remain grouped by trusted audit fingerprint. The workflow calls the
AUD-05 `diary_production_baseline.classify_groups` implementation with the fixed
root-owned `/etc/diary/lock-expected-flows.json`. Its schema is the same as
`production-baseline.md`: expected destinations and routine expected failures
are excluded, while novel destinations, repeated denials, and suspicious broker
activity require acknowledgement. A novel destination cannot be hidden by a
routine-failure rule. The empty example classifies no exceptions and therefore
fails safe until the accepted AUD-05 classifications are installed. A truncated
review is incomplete and blocks both acknowledgement and unlock.

The owner begins a CTL-08 transaction, completes fresh password + user-verified
WebAuthn authentication, and then acknowledges each current attention group or
all listed groups together. Acknowledgements are in-memory, bounded to 32
transactions, expire within five minutes, and are keyed by the random transaction
ID. The browser secret remains only in the CTL-08 HttpOnly cookie. Acknowledging
a changed report starts a new acknowledgement set; a report is also re-fetched
at confirmation, so a new group invalidates earlier acknowledgement. Routine
count changes do not require dismissing individual events. A service restart
forgets acknowledgements and fails closed.

## Lock, unlock, and attendance

All state-changing browser routes require the exact HTTPS `Origin`, a
same-origin fetch metadata value, and a distinct explicit-action header. The
lock, extend, and confirm routes accept no body; acknowledgement accepts only
its strict JSON schema. There is no WebSocket, polling extension, automatic
retry, or blocked-operation replay queue.

- **Lock now** calls the controller's existing `lock` command. Fresh
  reauthentication is not required to make the system safer.
- **Unlock confirmation** first requires locked state, a complete current report,
  and acknowledgement of every required group for the same transaction. The UI
  then forwards only the Authelia and transaction cookies to CTL-08's
  `/lock/unlock/consume`. Only a 204 response permits the separate controller
  `unlock` request. A controller failure returns 502, leaves restrictions in
  place through the controller failsafe, and consumes the proof so it cannot be
  retried. The owner must start a new fresh transaction.
- **Extend attendance** is a separate explicit button available only in
  `ATTENDED`. It calls the UI-only controller `extend` command and resets the
  monotonic deadline to 900 seconds. Status/report GETs, static assets, active
  turns, model output, background requests, WebSockets, and DSH/Kimi activity
  never call it. Repeating local break-glass while attended is rejected rather
  than serving as another renewal path. Controller restart and expiry still lock.

The UI consumes but does not replace CTL-08 freshness proof. An ordinary
Authelia two-factor cookie can view reports, lock, or explicitly extend existing
attendance, but cannot unlock without the forced-login, session-bound,
single-use transaction.

## Isolated validation

```bash
uv run pytest scripts/test_diary_lock.py scripts/test_diary_lock_ui.py scripts/test_diary_fresh_unlock.py
```

The UI tests use loopback fake Authelia, the real CTL-08 transaction service,
and fake controller/report boundaries. They cover full grouped review, missing
and changed acknowledgement, truncated reports, stale/pending/substituted and
agent sessions, single-use replay, expiry, failed unlock, explicit lock and
extension, and the absence of extension from GET activity. They do not connect
to production sockets or unlock the host.

## Deployment and live gates

CTL-07 depends on AUD-05 and CTL-09. Do not deploy or perform a real UI unlock
until the baseline/classifications are accepted and the read-only dashboard
integration exists. After owner approval of both repositories and a separately
reviewed submodule-pointer change, deployment uses
`systemd/install-owner-ui-root` and `caddy/update_config`; those steps are not
part of isolated tests. Install the accepted AUD-05 classifications at the fixed
root-owned expected-flow path (the installer preserves an existing file and uses
the empty fail-safe example only initially). The CTL-09 page must load and call
the workflow asset using the contract above before live unlock verification.

The deployed acceptance checks must demonstrate:

1. anonymous, wrong-owner, spoofed-header, DSH/Kimi cookie, and stolen agent
   browser sessions cannot read or change UI state;
2. lock-now works and every current novel/suspicious network, DNS, and broker
   group blocks unlock until acknowledged, while routine groups need no
   individual dismissal;
3. an ordinary old two-factor cookie, pending/stale/substituted fresh session,
   replayed transaction, and expired transaction cannot unlock;
4. a real fresh owner ceremony plus explicit review and confirmation enters
   attended mode for approximately 15 minutes;
5. only the independent extend button changes the monotonic deadline; agent
   turns, model output, status/report refreshes, background requests, and any
   WebSocket activity do not;
6. injected controller/policy failure after CTL-08 consumption leaves locked
   restrictions active and the same proof cannot be replayed; and
7. no operation rejected while locked executes automatically after unlock.

Only after those live checks pass should the deployment change remove CTL-07
from the board and update the source design's implementation record.
