# LATER-01 owner merge/deny console extension

> Index: [scripts/](../README.md)

`agent_merge_console` is the prepared, UI-neutral extension for the independent
CTL-07 owner page. It is not a second agent-merge broker and does not replace
`agent-merge-approve`. It has not been deployed or connected to the live UI.

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
or Deny and never participate in authorization. The hidden OID in a rendered
form is only the reviewed-object identifier; it is not proof of ownership.

## Decision and audit boundary

CTL-07 must inject an owner-authorization callable. Every Merge or Deny decision
must return an `OwnerGrant` for the requested action and exact OID before this
module reads the queue for a decision. Browser fields, DSH/Kimi sessions or
tokens, an ordinary two-factor cookie, and an agent-supplied OID must not be
translated into a grant. The existing CTL-08 transaction is unlock-specific;
CTL-07 must provide an action-and-OID-bound fresh authorization flow rather
than presenting an unlock consumption as merge authority.

After authorization, the console rechecks the current queue. A changed or
already-published OID fails with no publication. Merge writes an
`owner_authorized` audit event and then calls the unchanged
`agent_merge_queue.approve_queue()` with the same OID. That existing path takes
the queue lock, checks the exact OID again, merges approved `master`, and uses a
normal non-forced push. A `merge_completed` event follows success; a
`merge_failed` event follows failure. Failure of the pre-merge audit prevents
any repository operation. If completion auditing fails after publication, the
pre-merge event remains the exact-OID evidence and the UI must report an
unknown outcome rather than claiming a clean denial.

Deny writes one `owner_denied` event and performs no merge, push, queue reset,
integration synchronization, or environment operation. The queue remains
available for inspection. Both decision types carry the queue OID in trusted
audit `event_id`; the audit daemon derives writer identity from its socket peer,
not from browser claims.

`DiaryDecisionAudit` writes to the existing metadata-only trusted audit socket
and fails closed. The console does not query agent state or require an attended
agent session, so the owner path remains independent when the agent is locked.
The existing local `agent-merge-approve scripts <OID>` command remains unchanged
and available.

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
report/OID binding and safe rendering, exact-OID merge while marked locked,
stale-OID rejection, agent-session rejection, deny without publication,
pre-merge audit failure, and missing/stale/malformed/failed advisory fallback.
It never contacts the real queue.

Before marking LATER-01 done, integrate with the completed CTL-07 UI and
rebased CTL-09 dashboard, then pass controlled owner-approved live tests using
a scratch queue:

- real fresh owner Merge and Deny produce trusted audit records containing the
  exact displayed OID;
- changing the queue between render and POST rejects Merge without changing
  approved `master` or its remote;
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
