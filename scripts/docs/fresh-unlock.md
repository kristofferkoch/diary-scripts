# Passkey sessions and fresh single-use authorization

`diary-fresh-unlock` is the owner authentication and CTL-08 authorization
boundary. It contains no review UI, report acknowledgement, attendance expiry,
controller client, or executor authority. CTL-07 consumes a fresh transaction
only after its independent checks; CTL-01 remains the lock-state authority.

The daemon reads fixed root-owned `/etc/diary/lock-auth.toml`; production has no
environment or working-tree configuration override. A root-owned systemd
credential supplies the initial one-time enrollment code. The WebAuthn runtime
comes from the hash-pinned diary `owner-auth/requirements.txt`, not the agent
workspace.

## Passkeys and sessions

Enrollment requires discoverable credentials and user verification. A one-time
code can register exactly one credential; a successful registration consumes
its digest and creates an owner session. The owner can issue another code with
the installed recovery command without revoking existing passkeys.

Authentication uses a random 256-bit challenge held in a bounded, expiring
server-side ceremony. The browser receives a separate random ceremony cookie;
only its SHA-256 digest is stored. Completion atomically removes the challenge
before verifying the assertion, verifies the RP ID, HTTPS origin, signature,
user presence, user verification, and credential sign count, then creates a
ten-minute session. Session tokens and transaction secrets are stored only as
SHA-256 digests. Credential records contain public keys and sign counts, never
private keys.

`GET /lock/auth/forward-auth` is the loopback Caddy and owner-UI boundary. It
returns owner headers only for a live session. Anonymous browser requests get a
fixed local login redirect; client-supplied `Remote-*` headers are never trusted.

## Fresh action protocol

1. `POST /lock/merge/begin` requires a live owner session and same-origin action
   fields. The service stores the repository, merge/deny action, full queue OID,
   and full approved-base OID. Unlock has its own purpose and cannot carry merge
   bindings. The old session is invalidated after the reservation succeeds.
2. The redirect to `/lock/auth/` shows the trusted stored action before the
   passkey tap. The options request binds the challenge to the pending
   transaction. A successful assertion creates a session whose authentication
   timestamp and transaction ID match that ceremony.
3. `/lock/merge/return` or `/lock/unlock/return` requires the transaction secret
   and a session created after transaction creation by that exact transaction's
   ceremony. It atomically changes `pending` to `fresh` and records the session
   digest. Merge return displays the same details again.
4. Consume requires the same transaction and session, rechecks freshness and
   transaction binding, and atomically changes `fresh` to `consumed`. Merge also
   requires the repository to match. The executor independently asks the Unix
   verifier whether that exact grant was consumed before any repository change.

Replays, pending transactions, stale or ordinary login sessions, sessions for a
different transaction, DSH/Kimi cookies or tokens, expired transactions, wrong
owners, cross-purpose grants, and altered repository/action/OID fields fail.

The transaction and ceremony cookies have `Path=/lock`, `SameSite=Strict`, and a
maximum five-minute lifetime. No secret appears in a URL, response body, or
database. All endpoints use `Cache-Control: no-store`. `/lock/health` is local
and read-only. Creation is globally limited to 8 per 60 seconds, with at most 32
live transactions, 32 ceremonies, and 16 concurrent HTTP requests.

Run tests with:

```bash
uv run pytest scripts/test_diary_fresh_unlock.py
```

The suite's software authenticator performs real ES256 WebAuthn registration and
assertion signatures. The diary `owner-auth/test-local` integration test adds
real Caddy and two Chromium processes with virtual authenticators for browser
enrollment, session substitution, and replay rejection.
