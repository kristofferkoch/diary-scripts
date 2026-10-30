# Spond — `spond-sync` / `spondshow`

> Index: [scripts/](../README.md)

Evaluation phase, no timer yet. `uv run spond-sync --once` pulls
chats + events + posts via the Olen/Spond library and appends raw JSONL
to `memory/spond/YYYY-MM-DD.jsonl`. State cursor lives at
`memory/spond-state.json` (auto-bumped by the tool — unlike the mail
cursor). Use `uv run spondshow --since-cursor [--headers-only]` to
triage new items; `--kind event --future` filters to upcoming events.
Run **manually** for now; we'll add a systemd timer once we trust it.

Upstream library does not expose full per-chat message history nor Spond
Pay; the pipeline captures "chat X has new activity" signals + event /
post bodies, while payment receipts come in over mail.

### Auth

- `SPOND_USERNAME` is exported from user's shell environment — call
  `spond-sync` / `spondshow` bare under `uv run`, **no inline
  `SPOND_USERNAME=…` prefix** (a guessed override silently routes to the
  wrong account).
- Password: fixed argv `pass show spond/user`; no configurable shell command.
- 2FA must be **off** on the Spond account — Olen/Spond doesn't implement
  the OTP-verify flow
  ([issue #205](https://github.com/Olen/Spond/issues/205)).
- Set `$SPOND_RSVP_MEMBER_ID` to surface accept/decline/unanswered
  markers in event headers; Robin' member-id in Eksempel-IL G-lag is
  `0123456789ABCDEF0123456789ABCDEF`.

### Processing new Spond items (SOP)

Unlike the mail cursor, `memory/spond-state.json` is auto-bumped —
running `spond-sync --once` updates `last_successful_run` and the
per-chat / per-event / per-post seen-sets on every successful run. The
cursor reflects "the last time `spond-sync` ran cleanly", not "the
last time we wrote a digest".

1. **Fetch new activity** — `uv run spond-sync --once`.
   Prints a one-line `new records: total=N, by_kind={...}` summary on
   stderr; appends new records to `memory/spond/YYYY-MM-DD.jsonl`.
   Idempotent — re-running with no new activity is a no-op.
   (In a sjekk, `sjekk-collect` already ran this in Phase A, and its
   Phase B produced `memory/sjekk/latest/spond.txt` — step 2's output —
   and `spond-events.txt` — step 2b's output.)
2. **Triage new records** — `uv run spondshow --since-cursor
   --headers-only` for a one-line summary per record; drop
   `--headers-only` to see full JSON bodies. Filter with `--kind
   chat|event|post` or pin to `--chat <id>` / `--event <id>`.
   Events are tracked by a **content key** (`event_activity_key`), so an
   RSVP change, reschedule, or cancellation on an already-seen event
   re-emits and shows up here — set `$SPOND_RSVP_MEMBER_ID` (exported in
   user's shell) so the key folds in the tracked member's own response.
   If it's unset, `spond-sync` prints a `!!` warning and only
   reschedule/cancellation are caught, not RSVP. (Before 2026-05-29
   events used a flat id-only seen-set that swallowed RSVP changes
   entirely — see `test_spond_sync.py` for the regression.)
2b. **Belt-and-suspenders: list upcoming events each pass** — `uv run
   spondshow --kind event --future --headers-only` and
   reconcile every `[?]`/`[✓]`/`[✗]` RSVP marker against `CALENDAR.md`. A
   marker that disagrees with the calendar (or a calendar line still
   saying "RSVP: svar?" for an event user has since answered) is the
   signal to update. Catches anything the cursor logic misses.
   **`[event AVLYST]`** (with the organiser's `reason=`) is a cancelled
   event: it keeps its original start time, so mark the `CALENDAR.md` line
   avlyst (or drop it) rather than reading it as a live commitment. These
   were invisible before 2026-10-11 — `--future` rendered a cancelled
   event exactly like an active one, so a cancelled training stayed in the
   calendar through two sjekker after it was called off.
3. **Distil into the daily note** — for *actionable* items
   (kampendringer, oppmøtefrister, betalingskrav, foreldredugnad), add a
   `## Spond` section to today's `memory/YYYY-MM-DD.md`. Posts from the
   klubb-feed are deliberately low-signal — capture in JSONL, only
   surface to the daily note if there's a real follow-up.
4. **Commit** — daily-note updates under `MEM:`; pipeline changes under
   `TOOLS:`. The state file (`memory/spond-state.json`) and the JSONL
   files (`memory/spond/*.jsonl`) are generated — commit them alongside,
   but don't hand-edit.
