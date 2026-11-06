# agent-merge — reviewed publication for diary and `diary-scripts`

> Index: [scripts/](../README.md)

Only additions, modifications, and deletions of regular, non-executable files
under the private diary's `memory/` directory and in its explicit data-topic
allowlist publish unattended. The allowlist contains `BILLS.md`, `CALENDAR.md`,
`CALENDAR-PAST.md`, `CONTEXT.md`, `DELIVERIES.md`, `FINANCE.md`, `MEMORY.md`,
`NORCAP.md`, `RECEIPTS.md`, `SCHOOL.md`, and root-level `FDV_*.md` files. Every
other diary change and every `diary-scripts` change requires owner review. This
covers instructions, executables, installers, units, service configuration,
submodule pointers, symlinks, and mode changes without relying on a
sensitive-path denylist.

```
agent source clone + worktree
        │ agent UID: clean precheck + full HEAD OID + bundle
        ▼
regular bundle + expected OID ──bundle fetch + OID verification──▶ isolated queue
                                                                      │
reviewed queue OID + owner UID ───────────────────────────────────────┤
                                                                      ▼
                                                               approved master ──▶ GitHub
                                                                      │
                                                   after approval ────┴──▶ integration + queue

unattended diary bundle ──OID verification + classification──▶ approved diary
```

The agent source, integration, queue, and approved checkouts are four separate
repositories with separate object databases. Source clones and all their Git
metadata are agent-owned. Integration mirrors are writable only by the account
owner; approved and queue `.git` directories are also owner-only. There are no
shared refs, objects, logs, worktree registrations, config, or alternates between
these repositories.

The agent client writes a deterministic bundle under
`~/diary-worktrees/_bundles/<repo>/<sanitized-branch>-<full-OID>.bundle` and sends
the repository, branch, and full expected OID over the socket. The service
independently derives that fixed path, requires a regular file, fetches the named
branch from the bundle, and requires `FETCH_HEAD` to equal the expected OID before
classification or merge. The client removes the bundle afterward. Bundle transport
does not invoke `upload-pack`, read source config, or follow source alternates.

Queueing never pushes and never modifies approved `master`, integration, source
refs, or source worktrees. Approval publishes exactly the reviewed queue commit,
then synchronizes only the approved repository into the queue and integration
checkouts. Unattended diary publication classifies the exact verified object
against approved `master`. A source branch or source `master` changed after bundle
creation cannot alter the commit being published.

The kek service never runs Git in or writes through an agent source clone or
worktree. Source discovery, status/filter evaluation, expected-OID resolution,
bundle creation, worktree removal, and branch deletion all run as the agent UID in
the root-owned request client. The client refuses every source operation for any
other UID. Source code, hooks, aliases, filters, fsmonitor commands, external diff,
installers, and submodule initialization are never executed by the kek service.

## Agent operation

The installer creates standalone, non-hardlinked source clones at
`~/diary-worktrees/_sources/diary` and
`~/diary-worktrees/_sources/diary-scripts`. Create every worktree from the
corresponding source, not from an integration mirror:

```bash
(umask 002; git -C ~/diary-worktrees/_sources/diary worktree add ~/diary-worktrees/<slug> -b dev/<slug>)
(umask 002; git -C ~/diary-worktrees/_sources/diary-scripts worktree add ~/diary-worktrees/<slug>-scripts -b dev/<slug>)
```

Fetch and rebase in the source clone, then recreate or update a worktree as
needed. The diary worktree's submodule stays uninitialized; use the sibling
scripts worktree for code and tests. Stage a reviewed gitlink without
initializing it with `git -C <wt> update-index --cacheinfo
"160000,<sha>,diary-scripts"`.

Request publication from the diary workspace:

```bash
agent-merge-request diary dev/<slug>
agent-merge-request scripts dev/<slug>
```

The source branch must be clean and checked out under the agent worktree root.
The request command handles bundle creation and removal automatically. After
successful queueing or publication the agent client removes its worktree and
deletes its branch through the source clone; queue success never depends on kek
cleaning source. No routine owner-side Git cleanup or sudo is needed. Review and
approval remain separate operations. A diary request that needs review prints the
affected paths and the exact approval command.

### Wait for the exact decision

Add `--wait` to a request to print the resulting queue OID on its own line and
wait in the same turn. An unattended diary publication prints its exact source
OID instead and completes immediately. The timeout applies only to waiting:

```bash
agent-merge-request scripts dev/<slug> --wait --timeout 600
agent-merge-wait scripts <full-queue-OID> --timeout 600
```

The default timeout is 20 minutes. The client polls every two seconds and starts
no daemon or replacement publication request. Each socket exchange has one
absolute deadline covering connect, send, and every receive, recalculated before
each blocking operation; responses are capped at 1 MiB. Its exit statuses are:

- `0`: the exact requested OID has type `commit` and is an ancestor of that
  repository's approved `master` (including when approval created a later merge
  commit);
- `10`: `absent`, meaning the current queue head is not that OID and approved
  `master` does not contain it — denied, superseded, rewritten, or rebased away;
- `124`: the bounded wait expired while the exact OID remained queued;
- `2`: malformed or non-commit input, a socket error, an invalid/failed service
  response, or a Git operational failure.

Waiting uses the existing peer-credential-checked socket's read-only `status`
verb. The service returns only `queued`, `absent`, or `approved` for one repo and
full exact commit OID. Annotated tags and other non-commit objects are rejected,
never peeled to commits. A genuinely missing object is absent; an unreadable,
corrupt, or otherwise failing repository fails the exchange rather than becoming
`absent` or remaining queued until timeout. Only the owner or designated agent
UID may observe status; approval still requires the owner UID. Status takes no
queue lock, creates no queue or state file, exposes no diff or branch, and cannot
approve, merge, push, or replay a denied request. It remains available in locked
mode as a read-only operation.

## Human review and approval

The queue checkouts are `diary-automatic` and `diary-scripts-automatic`, beside
the diary workspace. As the account owner, review the relevant queue (scripts
shown here):

```bash
git -C /path/to/diary-scripts-automatic log --oneline master..queue/automatic
git -C /path/to/diary-scripts-automatic diff master...queue/automatic
git -C /path/to/diary-scripts-automatic rev-parse HEAD
agent-merge-review show <full-OID-from-rev-parse>
agent-merge-approve scripts <full-OID-from-rev-parse>
```

For diary, use `/path/to/diary-automatic` and `agent-merge-approve diary <OID>`.
The legacy `agent-merge-approve <OID>` form still means scripts.

### Diary `diary-scripts` pointer is forward-only

Approving a diary branch never moves the recorded submodule pointer backwards:

- a branch that left `diary-scripts` alone but was forked before a bump is
  merged with `--no-ff`, so master's current pointer wins — a fast-forward
  would copy the branch's stale entry and silently drop the approved scripts
  change;
- an explicit pointer change must name a commit that exists in the approved
  `diary-scripts` repository and is a descendant of the current pointer.
  Anything older is refused with "pointer would move backwards"; an unknown
  commit with "not an approved commit — approve the scripts change first".

The check runs during approval, on the reviewed OID and before the merge into
approved master (2026-11-02: a branch recorded its fork-point pointer and
reverted the sjekk-cursor change in `master` even though the approved scripts
repository still had it). It applies to the unattended memory path as well, so
a routine memory publication cannot roll the pointer back either.

### Integration checkout after a pointer bump

The scripts integration checkout is the diary workspace's `diary-scripts`
submodule, so a submodule update leaves it detached at the gitlink the diary
records. Approval accepts that detached HEAD only when the checkout is clean
and `HEAD` equals `git -C <superproject> rev-parse HEAD:diary-scripts`; a
different commit is refused and the error names both OIDs. A normal integration
checkout (the diary workspace itself, where `.git` is a directory) must still be
on `master`, so the diary-side guard is unchanged. The hand workaround of moving
the submodule's `master` branch before approving is no longer needed.

For scripts, an allowlist pre-scan rejects simple unapproved identities before
queue acceptance. The request returns sanitized categories and paths to the
producing agent, whose worktree remains available for fix-and-retry; matched
values are not echoed. Queue operations and review-event writes are serialized
by the queue lock, and trigger replacement is atomic.

After acceptance, queue-triggered `agent-merge-review` adds an advisory local-model
report without modifying the diff. It never approves or
executes queued code. The report supplements the diff and tests; it does not
replace them. See [`agent-merge-review.md`](agent-merge-review.md). The prepared
LATER-01 owner UI extension renders that report beside the real diff and reuses
the exact-OID approval below; its CTL-07/09 boundary and undeployed live gates
are in [`agent-merge-console.md`](agent-merge-console.md).

The human supplies the reviewed OID explicitly; the client never substitutes a
newer queue HEAD. `agent-merge-approve` sends that OID with the request, so a
branch queued after review cannot slip into approval. The socket service uses
`SO_PEERCRED` to require a caller with the account owner's UID for approval;
status separately admits only that owner or the designated agent UID. There is
no token or Signal prompt. Queue and approval operations are serialized with an
exclusive lock; read-only status deliberately does not take that lock. The console additionally supplies `expected_base` with the reviewed
approved-master OID. `approve_queue()` checks it before fetching and rechecks it
under the same lock immediately before merge, preventing base-only drift from
creating an unreviewed merge. The argument is optional, so existing local CLI
and unattended publication behavior is unchanged.

The socket front-end also emits AUD-04 locked-mode decisions with kernel peer
identity: diary/scripts requests are `would_deny`; approval is `allow` only when
the peer UID equals the service owner UID, and non-owner approval is `would_deny`
with `reason=owner_required`. Status is `allow` with `reason=read_only`. This
observation does not alter queueing or approval and does not use the advisory
model reviewer as an authority.

Setting `DIARY_BROKER_ENFORCE_LOCKDOWN=1` in the root-owned service environment
opts into CTL-05 enforcement. While `LOCKING`/`LOCKED`, diary/scripts queue and
publication requests and unknown operations are denied before dispatch; exact-OID
owner approval and read-only status remain available. Controller uncertainty or a failed audit write
also denies. The service has `diary-lock-control` only to confirm controller
state. The shipped unit leaves the variable unset, preserving audit-only behavior,
and denied requests are never queued for replay.

An empty queue is a no-op. A changed queue OID, dirty approved/integration
checkout, dirty queue, unexpected queue HEAD, wrong caller UID, or merge conflict
fails closed. Approval uses a normal non-forced `git push origin master`; a failed
push can be retried with the same reviewed OID.

## One-time migration from shared worktrees

Before installing this flow, finish or move every worktree registered in either
integration repository. The installer refuses deployment if any legacy
integration worktree remains; it does not prune or destroy active work. Migrate
unmerged commits to branches in the new source clones after the installer creates
them, or remove cleanly published legacy worktrees as the owner before rerunning.

The installer creates missing source clones as `agent` with `git clone
--no-hardlinks`, validates standalone `.git` directories, nested
refs/objects/worktrees symlinks, alternates, and hardlinked objects, and leaves
existing valid sources in place. Recursive permission work on agent trees runs as
`agent`; root recursively changes only trusted integration/approved paths. The
installer removes prior group/other write permissions from integration Git
metadata. Its smoke tests add, commit, remove, and delete branches as `agent` in
both sources and verify integration metadata is not agent-writable. Any smoke or
boundary failure aborts nonzero and prevents socket re-enablement. Normal branch
publication and cleanup need no sudo; only the reviewed installer and service
deployment require owner action.

## Execution boundary

The service invokes a root-owned copy of the queue module from the standalone
approved scripts repository. Automatic trees are for review only and are never
execution sources. Privileged sync services, token units, timers, and installers
use approved repositories or root-owned installed copies, never integration or
agent source checkouts. Scripts approval runs `uv sync --frozen` in approved so
the environment matches the reviewed lockfile before GitHub publication. Secret
lookups are fixed `pass` argv, not shell commands read from unattended diary
configuration.

## Refresh the installed queue module after a change

The approval client is **not** the `agent-merge-queue` in any virtualenv. Both
`agent-merge-approve` and the socket service exec the root-owned copy
`/usr/local/libexec/agent-merge-queue`, installed from
`$APPROVED/scripts/agent_merge_queue.py` by `dsh/agent-uid/install`. Editing
`agent_merge_queue.py` and approving it therefore changes *nothing* about the
next approval until that copy is refreshed.

Refresh it with the installer, or — when the rest of the bootstrapped boundary
is unchanged — with the exact line the installer runs (root-owned, reviewed
source, same mode):

```bash
sudo-ask 'install -o root -g root -m 0755 \
  /home/kek/diary-scripts-approved/scripts/agent_merge_queue.py \
  /usr/local/libexec/agent-merge-queue'
```

Verify the behaviour actually landed before relying on it:

```bash
grep -c MERGE_IDENTITY /usr/local/libexec/agent-merge-queue   # >0 when the identity pin is in
python3 /usr/local/libexec/agent-merge-queue --help           # module still imports
```

Symptom when it is forgotten (2026-11-02): the approved venv was synced on every
approval while the agent-facing integration venv never was, and every
non-fast-forward queue merge failed with `queue merge conflict` — the installed
copy still had the pre-fix `sync_venv()` and the merge path that inherited the
account owner's git identity. Reading `git -C ~/diary-scripts-approved log` is
not evidence that approvals run that code; only the installed copy is.

## Pinned first-run bootstrap

The first installer run uses the owner-reviewed integration checkout because the
approved diary does not exist yet. The owner supplies full reviewed master OIDs
for both approved repositories:

```bash
sudo-ask 'env DIARY_APPROVED_OID=<full-reviewed-diary-OID> DIARY_SCRIPTS_APPROVED_OID=<full-reviewed-scripts-OID> /path/to/diary/dsh/agent-uid/install'
```

Later runs use `/path/to/diary-approved/dsh/agent-uid/install` and fresh reviewed
OIDs. The installer quiesces the legacy merge socket, clones or fetches both
repositories without checkout, and requires local `master` plus any fetched
master to equal the corresponding owner OID before checkout, `uv sync`, file
installation, or service restart. A mismatch leaves the legacy socket stopped;
success installs the reviewed boundary and re-enables the socket. Steady-state
queue approval remains bound to the exact reviewed queue OID as described above.
