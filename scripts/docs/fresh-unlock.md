# Fresh single-use unlock authorization

`diary-fresh-unlock` is the CTL-08 authorization boundary. It does not contain
review UI, report acknowledgement, attendance expiry, or a controller client.
CTL-07 consumes a fresh transaction only after those independent checks, then
makes its separate controller request. CTL-01 remains the only lock-state
authority.

The daemon is stdlib-only and installed as a root-owned file. It reads fixed
root-owned `/etc/diary/lock-auth.toml`; there is no environment or working-tree
configuration override. Transactions live in a service-owned SQLite database,
which stores only SHA-256 digests of the 256-bit browser and Authelia session
secrets. Upgrading from the original cookie-only schema invalidates all legacy
transactions.

## Trusted freshness proof

Stock Authelia forward-auth establishes only a current `two_factor` level, not
when either factor completed. The pinned Authelia build therefore includes the
reviewed `session-proof.patch`, which exposes the session's existing password,
WebAuthn, user-verification, and first/second-factor timestamp fields through
`StateGET`. The daemon calls that API directly over loopback with the exact
session cookie. Client-supplied `Remote-*` headers are never trusted.

Both factor timestamps must be strictly later than transaction creation, and
password, WebAuthn, and WebAuthn user verification must all be present. Return
stores a SHA-256 digest of that exact session cookie. Consume must present the
same session and prove both timestamps again. An omitted preexisting cookie, a
different session left valid by logout, or a session substituted after return
therefore cannot satisfy the transaction.

## Protocol

1. `POST /lock/unlock/begin` requires the configured HTTPS `Origin` and
   `X-Diary-Unlock-Begin: yes`. A global creation rate limit runs before any
   Authelia call. The daemon forwards only the presented Authelia session
   cookie to logout, requires an HTTP 200 `status=OK` JSON response, and, when
   a cookie was presented, independently verifies that exact cookie no longer
   passes forward-auth. Any failure creates no transaction.
2. Successful invalidation creates a pending transaction, a strict Secure
   HttpOnly browser cookie, and a redirect to the fixed Authelia portal with a
   fixed return URL. Cookie-less begin is safe because every old session still
   fails the trusted timestamp proof.
3. `GET /lock/unlock/return` requires the transaction secret and trusted fresh
   session proof, then atomically changes the matching unexpired transaction
   from `pending` to `fresh` and records its session binding.
4. After CTL-07 completes its review and explicit confirmation,
   `POST /lock/unlock/consume` requires the configured HTTPS `Origin`,
   `X-Diary-Unlock-Confirm: yes`, the same browser transaction and Authelia
   session, and fresh trusted proof. An atomic `fresh` to `consumed` transition
   returns 204. Only then may CTL-07 call the controller. Replays, pending
   transactions, stale cookies, DSH/Kimi cookies or tokens, wrong-owner
   sessions, expired transactions, and cookie-only requests fail.

The transaction cookie has `Path=/lock/unlock`, `SameSite=Strict`, and a maximum
five-minute lifetime. No secret appears in a URL, response body, or database.
All endpoints use `Cache-Control: no-store`. The local `/lock/health` endpoint
is read-only and is not exposed through Caddy.

Creation is globally limited to 8 transactions per 60 seconds, with at most 32
live transactions and 16 concurrent HTTP requests. Expired rows are pruned on
each creation. These limits do not use source-IP headers or any other
client-controlled identity.

Run tests with `uv run pytest scripts/test_diary_fresh_unlock.py`. The diary
`authelia/test-local` integration test additionally exercises the patched real
Authelia APIs, two isolated Chromium clients, actual user-verified WebAuthn
ceremonies, preexisting-session substitution, and replay rejection.
