# AUD-05 production baseline evidence and review report

`diary-production-baseline` turns the existing trusted `diary-audit baseline`
API into a coverage-aware review report. It is a reporting client only: it does
not install instrumentation, exercise production flows, authenticate the owner,
deploy changes, or enable enforcement. A report with complete evidence is not
by itself proof that AUD-05 is done. The production interval, coverage records,
expected-flow classifications, and owner review must refer to real trusted
artifacts.

## Coverage and interval evidence

Supply a JSON document with this shape:

```json
{
  "schema_version": 1,
  "interval": {
    "started_at": "2026-11-01T09:00:00+00:00",
    "ended_at": "2026-11-02T10:00:00+00:00",
    "evidence": [
      {
        "observed_at": "2026-11-01T09:00:00+00:00",
        "reference": "trusted-journal-start-review-1",
        "result": "passed"
      },
      {
        "observed_at": "2026-11-02T10:00:00+00:00",
        "reference": "trusted-journal-end-review-1",
        "result": "passed"
      }
    ]
  },
  "coverage": {
    "normal_destinations": [
      {
        "observed_at": "2026-11-01T12:00:00+00:00",
        "reference": "trusted-flow-review-1",
        "result": "passed"
      }
    ]
  }
}
```

The complete checklist keys are:

- `normal_destinations`, `search`, `fetch`, and `dns`;
- `broker_signal`, `broker_diary_sync`, `broker_agent_merge`, and `broker_sudo`;
- `collector_mail`, `collector_spond`, `collector_vigilo`, and
  `collector_skolemelding`;
- `active_turns`, `restart`, and `boot`.

Omitted checks are reported as uncovered. A covered check needs at least one
passing, timestamped evidence record inside the declared interval. Interval
evidence must include passing records at both the exact start and end
boundaries. The interval must be at least 86,400 seconds, and
the client queries exactly that interval rather than accepting an unrelated
`24h` argument. References are bounded metadata, not message bodies or other
private content. All timestamps require a timezone. Duplicate JSON fields,
unknown checks, non-passing records, and out-of-interval evidence fail closed.

These records make the acceptance evidence explicit and reviewable; the tool
cannot independently establish that the referenced production activity actually
happened.

## Expected-flow classifications

Without an expected-flow file, every network/DNS destination is novel and every
non-routine broker denial is suspicious. Supply classifications explicitly:

```json
{
  "schema_version": 1,
  "expected_destinations": [
    {
      "match": {
        "event": "network.flow",
        "destination_ip": "192.0.2.10",
        "destination_port": 443,
        "protocol": "tcp"
      },
      "reference": "owner-approved-model-destination-1"
    }
  ],
  "routine_expected_failures": [
    {
      "match": {
        "event": "broker.decision",
        "component": "diary-sync",
        "operation": "sync",
        "decision": "would_deny",
        "caller_uid": "1001"
      },
      "reference": "owner-classified-routine-sync-attempt-1"
    }
  ]
}
```

A rule is a non-empty exact-match subset of the metadata returned by
`diary-audit baseline`. Destination rules require `destination_ip` or
`dns_name`. Every rule needs an owner-reviewable reference. No real endpoint or
identity is built into the tool.

The report has four finding sections:

1. **Routine expected failures:** attention decisions matched by an expected
   destination or an explicit routine-failure rule.
2. **Novel destinations:** network/DNS groups not matched by an expected
   destination. A novel group cannot be hidden by a routine-failure rule.
3. **Repeated denials:** non-routine `deny`, `would_deny`, or `error` groups
   whose cumulative occurrence count is greater than one.
4. **Suspicious broker activity:** non-routine broker attention decisions.
   Unknown broker operations always remain suspicious.

Novel/repeated and suspicious/repeated groups can appear in both applicable
sections; the categories preserve independent signals rather than discarding
one through precedence. Expected audit-only network flows do not become
repeated-denial findings merely because normal production use repeats them.
Ordinary observed/allowed groups stay in the underlying baseline without
creating a finding.

## Generate and review

```bash
diary-production-baseline \
  --evidence production-evidence.json \
  --expected expected-flows.json \
  --json

diary-production-baseline \
  --evidence production-evidence.json \
  --expected expected-flows.json \
  --review owner-review.json \
  --json
```

The first output supplies `report_sha256` and `expected_flows_sha256`. The hash
covers the reviewable interval, baseline data, coverage evidence,
classifications, and findings; volatile report-generation timestamps are
excluded so an unchanged report can be regenerated for owner review. Any change
to reviewable data changes the hash and requires a new review.

Owner-review evidence has this shape:

```json
{
  "schema_version": 1,
  "report_sha256": "64-lowercase-hex-characters-from-the-report",
  "expected_flows_sha256": "64-lowercase-hex-characters-from-the-report",
  "reviewed_at": "2026-11-02T11:00:00+00:00",
  "review_reference": "owner-review-record-1",
  "expected_flow_classifications_reviewed": true,
  "acknowledged_fingerprints": [
    "fingerprint-of-each-novel-repeated-or-suspicious-group"
  ]
}
```

The acknowledgement set must exactly equal the current non-routine finding
fingerprints. Review time must be after the interval and no later than report
generation. The expected-flow review flag, both hashes, and all
acknowledgements are required for the owner-review gate. This JSON is evidence
of an external owner decision, not an authentication mechanism; the independent
UI remains CTL-07.

`acceptance.gates` separately reports coverage, minimum duration, untruncated
report completeness, journal-record presence, and owner review.
`acceptance.evidence_complete` is true only when all five pass.
`acceptance.live_verification` always remains `not_performed_by_this_tool`.
AUD-05 can be deleted from the implementation board only after its real AUD-03,
production-duration, coverage, owner-review, and prerequisite-card conditions
are satisfied and deployed verification is recorded through the normal process.
