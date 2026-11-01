# ACT-01 lockdown acceptance harness

`diary-lockdown-acceptance` validates and orchestrates the complete integrated
acceptance catalog. It does not implement lockdown, an adapter, deployment, or
any concurrent dependency. The shipped catalog is a statement of required
checks, not evidence that any check has run or passed.

## Offline use

From the scripts repository:

```bash
uv run diary-lockdown-acceptance validate
uv run diary-lockdown-acceptance plan
uv run diary-lockdown-acceptance verify --catalog /path/to/catalog.toml /path/to/evidence-run
uv run pytest scripts/test_lockdown_acceptance.py
```

`validate` checks the exact suite identity and ordered 47-case set, unique
operations, semantic expectation/OID-predicate completeness, the single reboot
boundary, and mandatory source-design/ACT-01 coverage. `plan` only prints
operations. `verify` only reads and authenticates an existing run, including
its cross-case OID binding and post-boot ID against the running kernel. None of
these commands constructs an adapter or changes host enforcement.

Live execution additionally requires both `--execute` and the exact
acknowledgement exposed as `LIVE_ACKNOWLEDGEMENT` in
`scripts/lockdown_acceptance.py`. The diary ACT-01 runbook is the operator
entry point. Live run/resume accepts no catalog or adapter path override: it
uses only `/usr/local/libexec/diary-lockdown-acceptance.toml` and
`/usr/local/libexec/diary-lockdown-acceptance-adapter`. Each file and every
ancestor must be root-owned, non-symlinked, and not group/other writable. The
harness opens each final file with `O_NOFOLLOW`, validates it by descriptor,
reads the catalog through that descriptor, and executes the adapter through
`/proc/self/fd` with the descriptor inherited. Replacing a pathname after the
check therefore cannot select a different inode.

## Adapter boundary

The concurrent CTL/PIPE branches must supply one root-owned, non-group/other
writable executable adapter, or a root-owned facade over their component
adapters. This change deliberately does not create that adapter or select live
endpoints. It must use fixed approved configuration and fixed argv, never a
shell command or endpoint supplied in the request.

The harness executes the adapter once per case with no arguments, a minimal
`PATH` and `LC_ALL=C`, one JSON object on standard input, and the catalog's
timeout. Standard output must be exactly one JSON object. Standard error and
the exit status are retained as process evidence. A nonzero exit cannot be a
pass.

Request fields are exact:

```json
{
  "protocol": 2,
  "suite_id": "act01-lockdown-acceptance",
  "run_id": "operator-selected-run-id",
  "case_id": "network.ipv4_tcp",
  "operation": "network.locked_ipv4_tcp",
  "phase": "execute"
}
```

`phase` is `verify` only for the post-reboot invocation and `cleanup` only for
`suite.relock_cleanup`. The adapter must reject unknown fields, suite/run
mismatches, unexpected phases, replayed case attempts, and operations outside
its implemented catalog. `run_id` is the correlation key across components;
it is not authorization.

Response fields are exact except for the harness-added `_process` evidence:

```json
{
  "protocol": 2,
  "status": "pass",
  "summary": "short non-private result summary",
  "evidence": {
    "probe_id": "run-scoped-opaque-id",
    "denied": true,
    "locked_verified": true,
    "audit_refs": ["trusted-journal-cursor-or-emission-id"]
  }
}
```

Statuses are:

- `pass`: every `required_evidence` key is present and nonempty, and every
  `[expect]` value matches exactly with the declared boolean/integer type.
  Required boolean evidence without an expectation is rejected. Thus a response
  cannot pass with `denied = false`, `all_rejected = false`, or
  `passkey_user_verified = false`. `blocked_replay_count` and
  `suppressed_replay_count` must both be numeric zero after
  `replay.no_replay`, `owner.first_cycle`, and cleanup.
- `fail`: an assertion failed. The harness stops and attempts cleanup/relock.
- `blocked`: a dependency, adapter operation, or safe precondition is absent.
  This is not a pass and must never be converted to one.
- `reboot_required`: valid only for the first `boot.locked` invocation. The
  adapter has verified locked state and staged its own trusted run state; the
  owner must reboot locally. The harness exits 3 without cleanup or unlock.

`approved_commit_ids`, `deployed_commit_ids`, and `activated_commit_ids` are
objects containing exactly `diary` and `scripts`, each a lowercase 40-character
Git OID. `[oid_equal]` requires equality within preflight/activation, and the
harness also binds all three across cases at execution time and again during
verification. The bound object is persisted as `commit_oids` in `run.json`.
Preflight-to-activation drift stops the run immediately.

A pass with missing evidence, malformed JSON, extra response fields, a timeout,
or an invalid boot transition is a failed case. Do not return credentials,
message bodies, URLs, cookies, private queries, or raw packet payloads in
`evidence`; use opaque probe IDs, trusted journal references, digests,
counters, classifications, and boolean assertions.

### Expected component operations

The catalog is the complete operation list. Its dependency labels describe
which concurrent outcome supplies each capability:

- `preflight.*`: owner-approved exact deployed identities, dark state, safe
  maintenance window, recovery, CTL-09 read-only dashboard health, adapter
  capabilities, and evidence storage.
- `activation.integrated`: CTL-02/03/04/05/07 and PIPE-05 jointly enable the
  exact integrated result, initially locked.
- `network.*`: CTL-03/04 execute agent-identity probes and return trusted flow,
  DNS, and rule/set evidence. They cover new and existing flows, both address
  families and transport protocols, every named alternate-resolution path,
  loopback, LAN, tailnet, and model search/fetch.
- `pipeline.*`: PIPE-01 through PIPE-05 prove fixed-model allowance isolation
  and one real event from independent mail collection through local schema
  validation, safe rendering, and Signal delivery while agent egress is denied.
- `broker.*`: CTL-05, AUD-04, and PIPE-05 use run-scoped non-destructive canary
  operations. Rejection must precede execution and include trusted caller and
  decision evidence. Approved trees, collectors, messages, and host state must
  remain unchanged. Safe sync reads are positive controls.
- `immutability.*`: trusted components compare before/after digests and health
  after agent-identity write attempts. Test fixtures must not weaken ownership
  or leave agent-writable trusted files behind.
- `failure.*`: CTL/AUD/PIPE adapters inject one controlled failure at a time,
  observe containment during the failure, and restore health before continuing.
  Audit failure has separate agent-action and pipeline-notification cases.
- `auth.*` and `turn.*`: CTL-02/07/08 drive real owner/authentication and turn
  fixtures. Fresh authorization is not an ordinary two-factor cookie. Active
  agent work must not extend attendance or resume after cancellation.
- `notification.*`: PIPE-03/04/05 submit adversarial local fixtures and provide
  sanitized render digests, local-review references, and delivery/suppression
  counts. No rejected or suppressed fixture may be sent later.
- `replay.no_replay`: correlates every earlier blocked operation ID with a
  post-unlock observation window and trusted execution/audit records.
- `boot.locked`: records the pre-reboot boot ID and locked proof, suspends, then
  returns the same pre ID and a distinct post-reboot ID, boot audit, agent
  denial, and pipeline containment. The post ID must equal the running kernel
  at resume and final verification, and both records must agree on the pre ID.
  The adapter does not reboot the host itself.
- `owner.first_cycle`: the owner, not the adapter autonomously, reviews the live
  report, acknowledges findings, performs fresh user-verified passkey unlock,
  checks ordinary behavior and no replay, and records an explicit decision.

An unimplemented operation must return `blocked`; a placeholder `pass` is a
protocol violation and invalidates the run.

## Evidence layout

A new run creates a mode-0700 directory below the operator-selected evidence
root. Existing evidence-root, run, and cases directories must be root-owned
non-symlink directories with exact mode 0700 during live run, resume, and
verification; loose permissions are rejected rather than repaired. Existing
run IDs cannot be reused. The directory contains:

- `catalog.toml`: byte-for-byte catalog used for the run.
- `run.json`: suite/run identity, catalog SHA-256, bound `commit_oids`, current
  status, next case, record indexes and per-file SHA-256 digests, cleanup
  result, and harness errors. Status is `running`, `reboot_required`, `failed`,
  or `passed`.
- `cases/NNN-<case>-attemptN.json`: exact public request and adapter response.
  The boot case has a pre-reboot and post-reboot record.
- `cleanup.json`: terminal cleanup/relock response and its indexed digest.

The harness writes files atomically with mode 0600, fsyncs each file and its
containing directory around rename, and rejects symlink or non-regular
evidence files. Resume and verification require the unchanged catalog, bound
OIDs, and post-boot running-kernel match; perform final verification on the
acceptance boot. Evidence digests detect accidental modification; they are not
a signature or a substitute for trusted journal provenance. Treat the whole
directory as sensitive, retain it under the owner-approved audit policy, and
never commit it to either repository.

## Cleanup, interruption, and return codes

Except for the deliberate reboot suspension, every terminal path attempts
`suite.relock_cleanup`, including a failed case, malformed adapter response,
timeout, interruption, and internal harness failure. Cleanup must:

1. Remove only fixtures and temporary probe state belonging to `run_id`.
2. Leave blocked operations permanently blocked; never replay or queue them.
3. If activation occurred, force and independently verify `LOCKED`, agent
   network/broker denial, pipeline containment, and healthy audit writing.
4. If activation never occurred, preserve the verified dark state rather than
   enabling enforcement as a side effect of a failed preflight.
5. Restore injected controller/audit/policy failures and cancel test turns.
6. Return `cleanup_complete`, `locked_verified`, and zero
   `blocked_replay_count`/`suppressed_replay_count`. Before activation,
   `locked_verified` means the expected dark/locked recovery
   state was verified without turning on general-agent enforcement.

A failed cleanup makes the entire run failed and requires local owner recovery;
it must not be hidden by earlier passing cases. A final pass also requires a
passing cleanup and successful `verify` of every case.

Return codes: 0 = complete pass/valid offline operation, 1 = failed acceptance
or cleanup, 2 = catalog/protocol/usage/verification error, 3 = reboot required,
130 = interrupted with cleanup attempted.
