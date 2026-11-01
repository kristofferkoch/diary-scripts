# agent-merge — reviewed publication for diary and `diary-scripts`

> Index: [scripts/](../README.md)

Only additions, modifications, and deletions of regular, non-executable files
under the private diary's `memory/` directory publish unattended. Every other
diary change and every `diary-scripts` change requires owner review. This covers
instructions, executables, installers, units, service configuration, submodule
pointers, symlinks, and mode changes without relying on a sensitive-path
denylist.

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

regular-file memory bundle ──OID verification + classification──▶ approved diary
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
checkouts. Unattended memory publication classifies the exact verified object
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

For scripts, an allowlist pre-scan rejects simple unapproved identities before
queue acceptance. The request returns sanitized categories and paths to the
producing agent, whose worktree remains available for fix-and-retry; matched
values are not echoed. Queue operations and review-event writes are serialized
by the queue lock, and trigger replacement is atomic.

After acceptance, queue-triggered `agent-merge-review` adds an advisory local-model
report without modifying the diff. It never approves or
executes queued code. The report supplements the diff and tests; it does not
replace them. See [`agent-merge-review.md`](agent-merge-review.md).

The human supplies the reviewed OID explicitly; the client never substitutes a
newer queue HEAD. `agent-merge-approve` sends that OID with the request, so a
branch queued after review cannot slip into approval. The socket service uses
`SO_PEERCRED` to require a caller with the account owner's UID; there is no token
or Signal prompt. Queue and approval operations are serialized with an exclusive
lock.

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
