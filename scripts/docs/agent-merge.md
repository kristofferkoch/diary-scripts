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
        │ pinned branch fetch
        ▼
isolated queue/automatic ──reviewed OID + owner UID──▶ approved master ──▶ GitHub
        │                                                │
        └────────approved master after approval──────────┴──▶ integration mirror

regular-file memory branch ──pinned fetch + classification──▶ approved diary
```

The agent source, integration, queue, and approved checkouts are four separate
repositories with separate object databases. Source clones and all their Git
metadata are agent-owned. Integration mirrors are writable only by the account
owner; approved and queue `.git` directories are also owner-only. There are no
shared refs, objects, logs, worktree registrations, or config between these
repositories.

Queueing fetches the named branch from the agent source into the isolated queue;
it never pushes and never modifies approved `master` or integration. Approval
publishes exactly the reviewed queue commit, then synchronizes only the approved
repository into the queue and integration checkouts. Unattended memory
publication fetches a pinned source commit into the standalone approved diary
first and classifies that exact object against approved `master`. A source branch
or source `master` changed after the fetch cannot alter the commit being
published.

The service treats source repositories and worktrees as untrusted. Its Git
environment disables fsmonitor, hooks, external diff, and
`uploadpack.packObjectsHook`, including in the local `upload-pack` child used for
fetches. Source code, hooks, aliases, installers, and submodule initialization
are never executed by the merge service.

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
After successful queueing or publication the service removes its worktree and
deletes its branch through the source clone. No routine owner-side Git cleanup
or sudo is needed. Review and approval remain separate operations. A diary
request that needs review prints the affected paths and the exact approval
command.

## Human review and approval

The queue checkouts are `diary-automatic` and `diary-scripts-automatic`, beside
the diary workspace. As the account owner, review the relevant queue (scripts
shown here):

```bash
git -C /path/to/diary-scripts-automatic log --oneline master..queue/automatic
git -C /path/to/diary-scripts-automatic diff master...queue/automatic
git -C /path/to/diary-scripts-automatic rev-parse HEAD
agent-merge-approve scripts <full-OID-from-rev-parse>
```

For diary, use `/path/to/diary-automatic` and `agent-merge-approve diary <OID>`.
The legacy `agent-merge-approve <OID>` form still means scripts.

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
--no-hardlinks`, validates standalone `.git` directories, leaves existing valid
sources in place, and removes prior group/other write permissions from
integration Git metadata. Its smoke tests add, commit, remove, and delete
branches as `agent` in both sources and verify integration metadata is not
agent-writable. Normal branch publication and cleanup need no sudo; only the
reviewed installer and service deployment require owner action.

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
