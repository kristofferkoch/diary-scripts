# agent-merge-review — advisory local-model queue review

> Index: [scripts/](../README.md)

`agent-merge-review` is the first, non-approving tier of automated review for
the `diary-scripts` publication queue. It reads the isolated
`queue/automatic` checkout, sends a redacted diff to the fixed local
OpenAI-compatible chat endpoint in `hosts.vlm`, and writes an advisory report.
It never executes queued code, approves a commit, merges, or pushes. Owner
review and exact-OID approval remain mandatory.

## Owner operation

After a trusted scripts queue update, the merge service atomically writes the
exact queue OID to `/var/lib/agent-merge/review-request`. A systemd path unit
starts the reviewer on that event; there is no polling timer. Review the model
report alongside, never instead of, the real diff:

```bash
agent-merge-review show
agent-merge-review show <full-queue-OID>
git -C /path/to/diary-scripts-automatic diff master...queue/automatic
agent-merge-approve scripts <full-queue-OID>
```

With no OID, `show` uses the current queue HEAD. Reports live in owner-only
`/var/lib/agent-merge/reviews/<queue-OID>.json`. If the queue changes during a
review, the old report is marked stale and the same activation reviews the new
HEAD, continuing until it is stable or a bounded retry limit is reached. A
stale report is recomputed automatically. A completed current report is not
recomputed unless the owner explicitly runs:

```bash
agent-merge-review run --force
```

An empty queue is a no-op. Model failure, malformed output, or an oversized
diff produces no model assessment and never blocks the existing human approval
path. Retrying an already-queued request emits the review event again, so a
missing trigger write can be retried without changing the queue.

## Allowlist pre-scan

`/etc/diary/review.toml` is a root-owned regular file with no environment or
agent-publishable override:

```toml
[review]
model = "local-model-name"
allowed_identities = ["Fake Person", ".example"]
max_diff_chars = 500000
```

The allowlist must contain only known fake identities or public service
domains. It is not a private deny-list. Built-in entries cover placeholders
such as `server`, `gpu-host`, documentation domains, loopback/unspecified
addresses, and documentation IP ranges. An entry beginning with a dot allows
that domain suffix.

Before the model call, the reviewer scans added diff lines, filenames, and
commit messages for simple email, URL, hostname/address assignment, and IPv4
patterns. Non-allowlisted matches are replaced with fixed redaction markers;
the matched value is not copied into the report. Context lines are also
redacted before transmission, without creating new-change findings.

This is deliberately best-effort protection against simple accidental PII. It
does not recognize every person's name, physical address, private identifier,
or encoded value. Redaction is not a guarantee that only allowlisted PII
reaches the model. The local model receives the remaining diff and can flag
contextual PII, security, and correctness concerns.

## Model and report boundary

The reviewer uses no tools and no cloud fallback. HTTP proxies and redirects
are disabled so the request cannot be redirected to another endpoint. Prompt
caching is disabled. The prompt treats the diff and commit messages as
untrusted data, and only a strict JSON response with `summary` and bounded
`pii`, `security`, or `correctness` findings is accepted.

Reports bind the input hash, model name, prompt version, base OID, and exact
queue OID. A fixed renderer strips terminal controls and misleading Unicode
from displayed model text. Reports always state that owner approval is still
required; the reviewer has no approval capability.

## Deployment

The diary-side `dsh/agent-uid/install` installs the root-owned launcher,
reviewer configuration if absent, service, and path unit from the approved
trees. It disables the old timer and starts one non-blocking review for any
change that was already queued during upgrade. The service runs as the
unprivileged `diary-review` identity, with read-only ACLs on the isolated queue
and no general access to owner-private files. Reports are group-accessible to
the owner but not the agent. Its writable path is limited to
`/var/lib/agent-merge`. Home is read-only, with explicit paths for the queue,
approved scripts, `hosts.toml`, and `review.toml`. The
installer resolves `hosts.vlm` and generates an egress drop-in allowing only
loopback and those model IP addresses. Rerun the reviewed installer after a
model endpoint or address change. The service does not run tests or any other
queued code.
