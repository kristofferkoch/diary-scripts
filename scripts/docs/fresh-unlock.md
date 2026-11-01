# Fresh single-use unlock authorization

`diary-fresh-unlock` is the CTL-08 authorization boundary. It does not contain
review UI, report acknowledgement, attendance expiry, or a controller client.
CTL-07 consumes a fresh transaction only after those independent checks, then
makes its separate controller request. CTL-01 remains the only lock-state
authority.

The daemon is stdlib-only and installed as a root-owned file. It reads fixed
root-owned `/etc/diary/lock-auth.toml`; there is no environment or working-tree
configuration override. Transactions live in a service-owned SQLite database,
which stores only a SHA-256 digest of each 256-bit browser secret.

## Protocol

1. `POST /lock/unlock/begin` requires the configured HTTPS `Origin` and
   `X-Diary-Unlock-Begin: yes`. The daemon forwards only the Authelia session
   cookie to Authelia's logout API. A non-200 logout response fails closed and
   creates no transaction. A successful logout is followed by a new pending
   transaction, a strict Secure HttpOnly browser cookie, and a redirect to the
   fixed Authelia portal with a fixed return URL.
2. Authelia requires the complete owner password plus user-verified WebAuthn
   flow. Its existing session was invalidated, so an old `two_factor` cookie
   cannot satisfy this step.
3. `GET /lock/unlock/return` independently calls Authelia forward-auth using
   only the current Authelia cookie. It ignores client-supplied `Remote-*`
   headers and requires the configured owner and `lock-owner` group. It then
   atomically changes the matching unexpired transaction from `pending` to
   `fresh`.
4. After CTL-07 completes its review and explicit confirmation,
   `POST /lock/unlock/consume` requires the configured HTTPS `Origin`,
   `X-Diary-Unlock-Confirm: yes`, another owner two-factor forward-auth result,
   and the same browser transaction cookie. An atomic `fresh` to `consumed`
   transition returns 204. Only then may CTL-07 call the controller. Replays,
   pending transactions, stale cookies, DSH/Kimi cookies or tokens, wrong-owner
   sessions, expired transactions, and cookie-only requests fail.

The transaction cookie has `Path=/lock/unlock`, `SameSite=Strict`, and a maximum
five-minute lifetime. No secret appears in a URL, response body, or database.
All endpoints use `Cache-Control: no-store`. The local `/lock/health` endpoint
is read-only and is not exposed through Caddy.

Run tests with `uv run pytest scripts/test_diary_fresh_unlock.py`. The diary
`authelia/test-local` integration test additionally exercises the real pinned
Authelia logout and forward-auth APIs and the HTTP transaction state machine.
