# agent-merge — reviewed publication for diary and `diary-scripts`

> Index: [scripts/](../README.md)

Only additions, modifications, and deletions of regular, non-executable files
under the private diary's `memory/` directory publish unattended. Every other
diary change and every `diary-scripts` change requires owner review. This covers
instructions, executables, installers, units, service configuration, submodule
pointers, symlinks, and mode changes without relying on a sensitive-path
denylist.

```
agent branch ──fetch──▶ isolated queue/automatic ──reviewed OID + owner UID──▶ approved master ──▶ GitHub
```

The integration, queue, and approved checkouts are separate repositories with
separate object databases. Approved and queue `.git` directories are accessible
only to the account owner. Queueing fetches the agent commit into the isolated
queue; it never pushes and never modifies approved `master`. Approval publishes
exactly the reviewed queue commit, then synchronizes the queue and integration
checkouts to approved `master`.

Unattended memory publication also fetches a pinned commit into the standalone
approved diary first. It classifies that exact object and never treats the
agent-writable integration `master` ref as the approval base. A branch changed
after the fetch cannot alter the commit being published.

## Agent operation

From the diary workspace:

```bash
agent-merge-request diary dev/<slug>
agent-merge-request scripts dev/<slug>
```

The source branch must be clean and checked out under the agent worktree root.
After successful queueing or publication its source worktree is removed. Review
and approval are deliberately separate operations. A diary request that needs
review prints the affected paths and the exact approval command.

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

## Execution boundary

The service invokes a root-owned copy of the queue module from the standalone
approved scripts repository. Automatic trees are for review only and are never
execution sources. Privileged sync services, token units, timers, and installers
use approved repositories or root-owned installed copies, never the integration
checkouts. Scripts approval runs `uv sync --frozen` there so the environment
matches the reviewed lockfile before GitHub publication. Secret lookups are fixed
`pass` argv, not shell commands read from the unattended diary configuration.
