# Safe pipeline notification rendering

`scripts.pipeline_notifications` implements PIPE-04 as a deterministic library.
It has no transport, notification daemon, model endpoint, or deployment wiring.
PIPE-03 passes validated results in; a future PIPE-05 boundary can transport the
returned notification records.

## PIPE-03 input

`NotificationRenderer.submit()` accepts exactly the five-field result mapping:

- `event_id`: an immutable `evt_<64 lowercase hex>` ID;
- `urgency`: a lowercase machine token; exactly `low` is held for digest;
- `summary`: model-written text;
- `deadline` and `review_reference`: text or null.

Missing or additional fields, invalid IDs, and invalid field types raise
`TriageResultError`. In particular, the model cannot add recipient, envelope,
URL, or link fields. PIPE-03 remains responsible for model invocation and for
checking that the returned event ID is the one submitted to the model.

## Fixed output and sanitization

Every returned `Notification` has the fixed recipient `note-to-self`, fixed
envelope `diary-pipeline-v1`, a deterministic notification ID, and a body built
only by the module's immediate or digest template. There is no caller option for
recipient or envelope. `Notification.as_dict()` provides the versioned record
for the future transport boundary.

Summary, deadline, and review-reference text is normalized to one plain-text
line. The sanitizer removes terminal escape/control sequences, bidi and
zero-width formatting, private-use/surrogate code points, HTML and Markdown
formatting, and URLs (including common obfuscated forms). Compatibility
normalization prevents full-width or mathematical styled text from masquerading
as template formatting. Summary/deadline/reference output is capped at
500/80/120 characters respectively. Fixed templates supply all line breaks and
labels.

## Durable decisions and frequency limits

The renderer stores its trusted local state in the caller-supplied SQLite path.
The service integration must place this in a pipeline-owned state directory;
the path is never supplied by the model. The defaults allow five rendered
notifications in a sliding 3600-second window. Immediate and digest
notifications share that aggregate budget.

- The first non-low result is rendered as `notified` while budget remains.
- A non-low result over budget becomes terminally `rate_limited`.
- A low result becomes `pending_digest` without consuming budget.
- `render_digest()` explicitly batches up to 20 pending low-priority events in
  deterministic order. A digest consumes one aggregate slot. If no budget is
  available, no notification is returned and the events stay pending.
- Rate-limited events never enter a later digest automatically.

SQLite `BEGIN IMMEDIATE` transactions serialize concurrent decisions. Event and
notification rows commit before a notification is returned. Reprocessing an
event returns `duplicate` with no notification, including after process restart
or when the model returns different prose for the same ID. This is intentionally
at-most-once rendering: a crash after commit but before transport must not cause
notification replay.

## Local review

`get_review(event_id)` and `list_review()` retain every event's original strict
result, sanitized fields, terminal/pending status, and notification association.
Thus rate-limited and duplicate-suppressed events remain locally available.
Original model text is review data only; callers must never copy it directly to
a notification. Digested events transition once to `digested`, and an empty or
already-drained digest returns no notification.

## Integration and live gates

Before activation, PIPE-03 must call `submit()` only after its strict response
and event-ID validation, and PIPE-05 must accept only the fixed output record
from the trusted pipeline identity. The integration must choose and protect the
persistent state path and schedule explicit digest rendering. Controlled live
tests must then prove restart persistence, aggregate suppression, local review,
and the absence of replay through the real trusted path. No live notification or
deployment test is part of this isolated implementation.
