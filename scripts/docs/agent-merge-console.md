# LATER-01 owner merge/deny console extension

> Index: [scripts/](../README.md)

`agent_merge_console` is the UI-neutral decision boundary for the independent
owner console. It is not a second agent-merge broker and does not replace
`agent-merge-approve`. The CTL-07 host and its fixed-argv executor are
implemented in `diary_lock_console_ui.py` and `diary_merge_console_service.py`;
they are not deployed.

## Review snapshot

`MergeConsole.snapshot()` reads the isolated `diary-scripts` queue while holding
its exclusive queue lock. It resolves the base from **approved `master`**, not
from the queue checkout's potentially stale `master` ref, and displays:

- the full exact queue commit OID and approved base OID;
- queued commit OIDs and subjects;
- the real `approved-master...queue-OID` diff, with external diff, textconv, and
  rename processing disabled;
- the advisory llama report only when its queue OID and base OID match that
  snapshot and its `stale` flag is false.

`render_console()` places the advisory report beside, never instead of, the real
diff. Its fixed HTML template escapes commit, diff, and model text. Missing,
stale, malformed, and model-failed reports are labeled but never disable Merge
or Deny and never participate in authorization. Both forms carry hidden queue
and approved-base OIDs. Those values identify the reviewed pair; they are not
proof of ownership.

## Both queues

The console reviews the `diary-scripts` queue and the diary repository's own
queue. The page names the queue under review, and every snapshot and decision
carries a `repo` field (`scripts` or `diary`) that the executor maps to the
matching path set; the caller still supplies no path and no Git argument, and an
unknown or missing repository is rejected rather than defaulted.

The fresh authorization binds the repository along with the action and both
OIDs, so a grant minted for one queue is refused for the other. The diary path
keeps the existing forward-only pointer guard: a decision publishes through the
same `approve_queue(..., expected_base=...)` path, and a stale queue or base is
rejected before any repository operation. Only the `diary-scripts` queue has an
advisory model report; the diary page labels that section as unavailable rather
than pretending it reviewed the change.

## Decision and audit boundary

CTL-07 must inject an owner-authorization callable. Every Merge or Deny decision
must return an `OwnerGrant` for the requested action, exact queue OID, and exact
approved-base OID before this module reads the queue for a decision. Browser
fields, DSH/Kimi sessions or tokens, an ordinary two-factor cookie, and
agent-supplied OIDs must not be translated into a grant. The existing CTL-08
transaction is unlock-specific; CTL-07 must provide an
action-queue-and-base-bound fresh authorization flow rather than presenting an
unlock consumption as merge authority.

After authorization, the console rechecks both the current queue and approved
`master`. A changed queue produces `queue_changed`; a base-only change produces
`base_changed`; either fails without publication. Merge writes an
`owner_authorized` audit event and calls
`agent_merge_queue.approve_queue(..., expected_base=reviewed_base)` with the
same pair. While holding the queue lock, that path checks the queue and expected
base before fetching, then rechecks the expected base immediately before merge.
The optional base argument preserves existing local callers. Publication uses a
normal non-forced push. A `merge_completed` event follows success; a
`merge_failed` event follows failure. Failure of the pre-merge audit prevents
any repository operation. If completion auditing fails after publication, the
pre-merge event remains the exact queue/base evidence and the UI must report an
unknown outcome rather than claiming a clean denial.

Deny writes one `owner_denied` event and performs no merge, push, queue reset,
integration synchronization, or environment operation. The queue remains
available for inspection. Both decision types carry the queue OID in trusted
audit `event_id` and approved base in `base_oid`; the audit daemon derives writer
identity from its socket peer, not from browser claims.

`DiaryDecisionAudit` writes to the existing metadata-only trusted audit socket
and fails closed. The console does not query agent state or require an attended
agent session, so the owner path remains independent when the agent is locked.
The existing local `agent-merge-approve scripts <OID>` command remains unchanged
and available; it does not opt into base enforcement.

## CTL-07 and CTL-09 integration contract

These are integration steps, not work performed by the LATER-01 branch:

1. CTL-07 adds the rendered section to its independent owner UI and routes only
   explicit same-origin Merge/Deny POSTs through `MergeConsole.decide()`.
   Authentication, CSRF/origin checks, fresh transaction consumption, and HTTP
   status mapping stay in that owner UI layer.
2. Run the owner UI with the account owner's unprivileged identity and only the
   required read access to queue/review state and write access to approved and
   integration repositories plus the audit socket. It needs no agent identity,
   agent token, root shell, or direct firewall authority.
3. CTL-09 may reuse `snapshot()` for read-only dashboard presentation and show
   the resulting audit decisions. It must not create another authorization or
   publication path. Its concurrent branch is intentionally untouched here.
4. Install and run only the approved scripts copy. Never execute code from the
   queue being reviewed. Keep the existing local owner approval command until
   all live gates below pass.

## Isolated tests and remaining live gates

`uv run pytest -q scripts/test_agent_merge_console.py` uses temporary Git
repositories and a temporary bare remote. It covers real approved-master diffs,
report/OID binding and safe rendering, authorization and publication binding to
both OIDs, exact-pair merge while marked locked, queue-only and base-only stale
rejection, agent-session rejection, deny without publication, pre-merge audit
failure, and missing/stale/malformed/failed advisory fallback. Queue tests also
inject a base advance between the initial locked freshness check and the final
pre-merge recheck. No test contacts the real queue.

Before marking LATER-01 done, integrate with the completed CTL-07 UI and
rebased CTL-09 dashboard, then pass controlled owner-approved live tests using
a scratch queue:

- real fresh owner Merge and Deny produce trusted audit records containing both
  exact displayed OIDs;
- changing either the queue or approved base between render and POST rejects
  Merge without an unreviewed merge or remote change;
- Deny leaves queue, approved, integration, and remote OIDs unchanged;
- both actions work during real general-agent lockdown, while stolen DSH/Kimi
  sessions, ordinary cookies, and bare agent-supplied OIDs receive no grant and
  cannot publish;
- missing, stale, malformed, and failed llama reports leave both this console's
  local approval and the existing `agent-merge-approve` path available;
- audit unavailability prevents Merge before publication, and the post-merge
  audit-failure behavior is verified without an automatic unsafe retry;
- queue code is never executed by snapshot, review, Merge, or Deny.

No deployment, host configuration change, real-queue merge/push, or live owner
verification is claimed by this preparation.

## Implemented authority model (CTL-07 host and executor)

The console is split across two identities so that web compromise is not
publication authority:

1. **Web host** (`diary_lock_console_ui.py`, unprivileged `diary-lock-ui`,
   loopback 9093 behind Caddy). It registers only `GET /lock/merge/`,
   `POST /lock/merge/decide`, and `GET /lock/health`. It has no unlock,
   acknowledgement, attendance, or dashboard route, no repository or audit
   access, no controller membership, and no firewall authority. Its only
   privileged peer is the executor socket.
2. **Executor** (`diary_merge_console_service.py`, `kek`, systemd
   socket-activated, peer UID must be `diary-lock-ui`). It has exactly two
   verbs: `snapshot` (read the reviewed queue pair through
   `MergeConsole.snapshot()`) and `decide` (run `MergeConsole.decide()`). There
   is no shell, no caller-supplied path or Git argument, and no verb that skips
   the console.
3. **Fresh grant** (root-owned `diary-fresh-unlock`, CTL-08 pattern). A merge or
   deny transaction is created with `purpose=merge` and bound to the action plus
   the exact queue and approved-base OIDs. The owner must complete a forced
   password + user-verified WebAuthn ceremony (`/lock/merge/begin` →
   Authelia → `/lock/merge/return`). At decision time the host consumes it
   (`/lock/merge/consume`), which is single-use and session-bound, and the
   executor independently asks the root-owned verifier socket whether that exact
   grant was consumed. An unlock transaction cannot be consumed as a merge
   grant, and a merge grant cannot be consumed at the unlock endpoint.
   `begin` requires a live owner two-factor session, so an anonymous caller
   cannot fill the bounded live-transaction table and starve the owner's only
   merge path; it accepts either the JSON API call or the console's plain
   same-origin form. `return` renders a page from the root-owned service that
   names the bound action and both OIDs. That page is the owner's check on what
   the ceremony authorized: the console is untrusted, so a compromised console
   can ask the owner to authenticate a pair it did not display, but it cannot
   hide the pair from the authorization service's own confirmation page.

**What a fully compromised web process can do:** consume a grant the owner
just authorized and submit that exact action and OID pair, and start a ceremony
for any pair it likes — so the owner must compare the action and OIDs on the
authorization service's confirmation page, not only the console. It cannot mint
a grant, change a pair after the ceremony began, merge anything else, push, run
Git, write audit, unlock, or extend attendance. **What the executor can do:** merge or deny exactly the
reviewed pair, and only with a consumed grant; its code path is fixed.

`deny` still requires a grant and writes `owner_denied`, then performs no
repository or environment operation. Replay is bounded twice: grants are
single-use, and the queue/base pair is rechecked under the queue lock (a second
merge of the same pair finds itself already approved). A stale queue or base
produces `queue_changed`/`base_changed` before publication; an unavailable
pre-merge audit prevents the repository operation; a post-publication audit
failure is reported as an unknown outcome, never as a clean denial.

Why not run the web service as `kek`, and why not a general broker: the web
process parses attacker-influenceable queue text and model output. Running it as
`kek`, or giving it an open-ended "run this Git command" channel, would make any
web bug a full publication compromise. The executor's fixed verb set keeps the
blast radius at "the decision the owner just approved".

```bash
uv run pytest -q scripts/test_agent_merge_console.py scripts/test_diary_lock_console.py
```

Live gates still open: install `diary-lock-ui.service`,
`diary-merge-console.socket`/`@.service` and the verifier socket via the CTL-08
installer, then pass the owner-approved live checks in the LATER-01 card.
