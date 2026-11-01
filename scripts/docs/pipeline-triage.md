# diary-pipeline-triage — fixed-model event triage

> Index: [scripts/](../README.md)

`diary-pipeline-triage` is PIPE-03's deterministic local-model stage. It reads
PIPE-01's immutable `EventSpool`, writes private per-event processing state, and
never sends a notification. PIPE-02 supplies the separate `diary-pipeline`
identity, approved-code execution, filesystem policy, and permanent egress
containment; this tool does not create or modify any of them.

## Integration boundaries

The production command has no path, model, endpoint, proxy, or fallback options:

```bash
diary-pipeline-triage
```

It reads `/var/lib/diary/events` through `EventSpool` and exits after one pass.
PIPE-02's future service/timer owns invocation and restart policy. A transport
failure exits nonzero after recording `retry`; completed and terminal review
records remain unchanged on later invocations. A crash after the model call can
repeat that local call, but cannot repeat a notification because this stage has
no delivery capability.

The model and its one endpoint come only from root-owned
`/etc/diary/trusted.toml`:

```toml
[pipeline]
model = "local-model-name"
endpoint = "http://192.0.2.10:8080"
```

The endpoint must be HTTP with an explicit port and a literal loopback, private,
or link-local IP address. Hostnames, URLs with paths/query/userinfo, public IPs,
and HTTPS are rejected. There is no ordinary-config or environment override.
HTTP proxies and redirects are disabled. There is no cloud, alternate-endpoint,
or alternate-model fallback, and prompt caching and thinking are disabled.

PIPE-02 must enforce the same single IP/port at packet level. Application checks
are not a substitute for that containment. PIPE-02 must also provision a
root-owned, pipeline-only view at the fixed trusted-config path inside the
service's mount namespace (or an equivalent reviewed mechanism). Do not give the
pipeline group/ACL read access to the collectors' full `trusted.toml`: the
contained service needs `[pipeline]`, not collector settings.

This stage does not inherit the advisory queue reviewer's identity, queue access,
report access, or filesystem permissions. It reuses only the no-tool,
strict-response, no-proxy/no-redirect, and no-fallback code patterns.

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
tool calls, wrong IDs, invalid field types/values, and oversized responses are
rejected. They produce terminal local `review` state and no notification or
second-model attempt.

## Durable state and PIPE-04 consumer

`/var/lib/diary/triage-state/<event-id>.json` is a private `0600` file written
atomically under a per-store lock. Its exact state schema records the input hash,
prompt version, attempts, timestamp, bounded error, and optional result. Status
is unambiguous:

- `pending`: discovered but not yet attempted;
- `processing`: attempt durably started, result not committed;
- `retry`: transport failure or interrupted processing, eligible next run;
- `completed`: exact validated result committed locally;
- `review`: deterministic input/result rejection, terminal without owner action.

On restart, `processing` first becomes `retry` with an interruption reason.
Completed and review events are not recomputed. Malformed, symlinked,
non-private, or input-inconsistent state fails closed rather than being replaced.

PIPE-04 must call `TriageStore.completed_results()` and consume only those exact
result dictionaries. It must not interpret `processing`, `retry`, or `review` as
notification candidates. Safe rendering, URL/control stripping, length policy,
digests, and rate limits remain PIPE-04 responsibilities; validated model text
is not yet safe to display or send.

## Verification and live gates

The isolated test suite uses loopback HTTP fixtures and fabricated events only.
It covers minimum-input filtering, strict/adversarial responses, redirect and
fallback denial, transport retry, crash recovery, terminal-state persistence,
state tampering, and the completed-result consumer. No test sends an external
notification.

This implementation does not satisfy PIPE-03's live card by itself. After
PIPE-01/02 integration and owner-approved deployment, the live gates remain:
valid real events complete locally; malformed/wrong-ID/extra-field responses stay
in review; the pipeline can reach only the configured model IP/port while DNS and
all alternates fail; its config view contains `[pipeline]` without collector
settings; restart preserves every state; and no notification occurs before
PIPE-04/05. Owner deployment and those live checks are deliberately not part of
this change.
