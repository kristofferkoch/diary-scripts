# diary-pipeline-triage — fixed-model event triage

> Index: [scripts/](../README.md)

`diary-pipeline-triage` is PIPE-03's deterministic local-model stage. It reads
PIPE-01's immutable `EventSpool`, writes private per-event processing state, and
never sends a notification. PIPE-02 supplies the separate `diary-pipeline`
identity, approved-code execution, filesystem policy, state directory, and
permanent egress containment; this tool does not create or modify any of them.

## Integration boundaries

The production command has no path, model, endpoint, proxy, or fallback options:

```bash
diary-pipeline-triage
```

It reads `/var/lib/diary/events` through `EventSpool` and exits after one pass.
PIPE-02's service/integration owns invocation and restart policy. This change
adds no second service, timer, or path trigger. A new transient failure or a
deferred event exits nonzero; completed and terminal review records remain
unchanged on later invocations. A crash after the model call can repeat that
local call only after its persistent backoff, and cannot repeat a notification
because this stage has no delivery capability.

The model and its one numeric endpoint come only from
`/etc/diary/pipeline.toml`, separate from collector-sensitive `trusted.toml` and
ordinary `hosts.toml`:

```toml
[pipeline]
model = "local-model-name"
model_ip = "192.0.2.10"
model_port = 8080
```

The active file is root-owned, group `diary-pipeline`, mode `0640`, single-link,
and not group/other writable. The table has exactly `model`, `model_ip`, and
`model_port`. The address must be numeric IPv4; hostnames, URLs, IPv6,
unspecified/multicast addresses, unknown keys, alternate endpoints, hard links,
symlinks, unsafe ownership/modes, and oversized/malformed files are rejected.
There is no ordinary-config or environment fallback and no reason for the
pipeline to read any collector setting. Direct HTTP does not use proxies or
follow redirects. There is no cloud, alternate-endpoint, or alternate-model
fallback, and prompt caching and thinking are disabled.

PIPE-02 must enforce the same single IP/port at packet level. Application checks
are not a substitute for that containment. This stage does not inherit the
advisory queue reviewer's identity, queue access, report access, or filesystem
permissions. It reuses only the no-tool, strict-response, no-proxy/no-redirect,
and no-fallback code patterns.

## Minimum model input

The request contains only `event_id`, `source`, `kind`, and recursively
allowlisted triage content such as headings, message text, sender, event times,
and deadlines. The spool's `native_id`, `revision`, collector identifiers,
recipients, attachment paths/URLs, and unrecognized payload fields are not sent.
The content itself is not redaction: allowed message text can still contain
private data and goes only to the fixed local model. Empty allowlisted content
and input over 512 KiB stay in local review without a model call.

The model has no tools and receives a strict JSON-schema request. A result must
contain exactly `event_id`, `urgency`, `summary`, `deadline`, and
`review_reference`:

- `event_id` and `review_reference` must both equal the submitted immutable ID.
- `urgency` is `low`, `normal`, or `high`.
- `summary` is nonempty and at most 4000 characters.
- `deadline` is null, a valid `YYYY-MM-DD`, or an RFC 3339 timestamp with a
  timezone.

Missing/extra fields, duplicate JSON keys, malformed JSON, multiple choices,
`tool_calls`, legacy `function_call`, wrong IDs, invalid field types/values, and
oversized responses are rejected. They produce terminal local `review` state and
no notification or second-model attempt.

## Durable state, backoff, and exhaustion

PIPE-02 provisions `/var/lib/diary/triage-state`; PIPE-03 consumes that fixed
directory and no other production state path. Each
`/var/lib/diary/triage-state/<event-id>.json` is a private `0600` file written
atomically under a per-store lock. Its exact state schema records the input hash,
prompt version, attempts, update time, next-eligible time, bounded error, and
optional result. Status is unambiguous:

- `pending`: discovered but not yet attempted;
- `processing`: attempt durably started, result not committed;
- `retry`: transient failure or interrupted processing with a persistent
  `next_eligible_at` backoff;
- `completed`: exact validated result committed locally;
- `review`: deterministic rejection or exhausted attempts, terminal without
  owner action.

There are at most five model attempts per event. Transient failures use
persistent exponential backoff of 60, 120, 240, and 480 seconds after attempts
one through four. Before that timestamp, later invocations record no new attempt
and report the event as deferred. After the fifth failure, the event becomes
terminal local `review` with no next-eligible time and no notification. Redirects,
HTTP 400, and other deterministic HTTP 4xx responses except 408/429 go directly
to local review rather than consuming the retry budget.

On restart, `processing` becomes `retry` with the same attempt count and a new
persistent backoff, or terminal `review` if the ceiling was already reached.
Completed and review events are not recomputed. Malformed, symlinked,
non-private, or input-inconsistent state fails closed rather than being replaced.

## PIPE-04 consumer

PIPE-04 must call `TriageStore.completed_results()` and consume only those exact
result dictionaries. It must not interpret `processing`, `retry`, or `review` as
notification candidates. The handoff preserves the validated field values without
rendering them. Safe rendering, URL/control stripping, length policy, digests,
and rate limits remain PIPE-04 responsibilities; validated model text is not yet
safe to display or send.

## Verification and live gates

The isolated test suite uses loopback HTTP fixtures and fabricated events only.
It covers the fixed config/state paths, config ownership/schema, minimum-input
filtering, strict/adversarial responses including legacy function calls,
redirect/HTTP-400 terminal review, persistent backoff and exhaustion across
invocations, crash recovery, state tampering, and the exact PIPE-04 value
handoff. No test sends an external notification.

This implementation does not satisfy PIPE-03's live card by itself. After
PIPE-01/02 integration and owner-approved deployment, the live gates remain:
valid real events complete locally; malformed/wrong-ID/extra-field responses stay
in review; the pipeline can reach only the configured model IP/port while DNS and
all alternates fail; `/etc/diary/pipeline.toml` is the only model configuration
and collector configuration remains unreadable; the provisioned state directory
is writable only by the pipeline; persistent deferral/exhaustion survives real
service restarts; and no notification occurs before PIPE-04/05. Owner deployment
and those live checks are deliberately not part of this change.
