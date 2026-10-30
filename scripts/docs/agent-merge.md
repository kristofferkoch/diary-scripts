# agent-merge — reviewed publication for `diary-scripts`

> Index: [scripts/](../README.md)

The private diary keeps its unattended memory/sjekk merge flow. The public
`diary-scripts` repository has a stronger boundary because its approved tree is
executed by privileged sync services.

```
agent branch ──fetch──▶ isolated queue/automatic ──reviewed OID + Signal token──▶ approved master ──▶ GitHub
```

The integration, queue, and approved checkouts are separate repositories with
separate object databases. Approved and queue `.git` directories are accessible
only to the account owner. Queueing fetches the agent commit into the isolated
queue; it never pushes and never modifies approved `master`. Approval publishes
exactly the reviewed queue commit, then synchronizes the queue and integration
checkouts to approved `master`.

## Agent operation

From the diary workspace:

```bash
agent-merge-request scripts dev/<slug>
```

The source branch must be clean and checked out under the agent worktree root.
After a successful queue merge its source worktree is removed. Review and
approval are deliberately separate operations.

## Human review and approval

The queue checkout is `diary-scripts-automatic`, beside the diary workspace.
As the account owner:

```bash
git -C /path/to/diary-scripts-automatic log --oneline master..queue/automatic
git -C /path/to/diary-scripts-automatic diff master...queue/automatic
git -C /path/to/diary-scripts-automatic rev-parse HEAD
agent-merge-approve <full-OID-from-rev-parse>
```

The human supplies the reviewed OID explicitly; the client never substitutes a
newer queue HEAD. `agent-merge-approve` sends that OID with the request, so a
branch queued after review cannot slip into approval. It prompts for the
persistent token without putting the token in process arguments. The service
serializes queue/approval operations and compares the token in constant time
with `/var/lib/agent-merge/token`, a `0600` file owned by the account owner. The
agent cannot read that file or incoming Signal messages.

An empty queue is a no-op. A changed queue OID, dirty approved/integration
checkout, dirty queue, unexpected queue HEAD, or merge conflict fails closed.
Approval uses a normal non-forced `git push origin master`; a failed push can be
retried with the same reviewed OID.

## Token delivery

`agent-merge-token-notify.service` starts with `agent-merge.socket` and sends the
token to Signal note-to-self through the write-only `signal-notify` proxy. The
token is persistent across restarts. To resend it:

```bash
systemctl start agent-merge-token-notify.service
```

To rotate and deliver a replacement as the account owner:

```bash
agent-merge-token-notify --rotate
```

Rotation invalidates the old token immediately. Do not send the token through
the agent, include it in a branch, or put it on a command line.

## Execution boundary

The service invokes a root-owned copy of the queue module from the standalone
approved repository. The automatic tree is for review only and is never an
execution source. Privileged sync services use the approved repository, and
approval runs `uv sync --frozen` there so the environment matches the reviewed
lockfile before GitHub publication. Secret lookups are fixed `pass` argv, not
shell commands read from the unattended diary configuration.
