# Signal messages — `signal-capture` + `signalshow`

> Index: [scripts/](../README.md)

Part of the **sjekk-flow**. Reads the linked Signal device's traffic in a
structured way, replacing the old `journalctl -u signal-mirror | grep`
sweep (the journal is the daemon's *human* stdout — variable lines per
message, sender/body split across lines, bounded by journald retention; it
lost a whole conversation on 2026-06-07).

**Architecture** (mirrors the Spond sink→JSONL→cursor→`*show` shape):

- **`signal-capture`** — an always-on `systemd --user` service
  (`signal-capture.service`) that connects to the `signal-mirror` daemon's
  JSON-RPC socket (`$XDG_RUNTIME_DIR/signal-cli/socket`) as a client and
  appends one normalised record per *conversation* message (incoming +
  the user's own sent, mirrored via `syncMessage`) to
  `memory/signal/YYYY-MM-DD.jsonl`. Typing/receipt/reaction-only/empty
  envelopes are dropped; attachments are recorded as **metadata only**
  (no blobs). The full envelope is kept under `raw` so nothing is lost.
  - Connecting to the *existing* daemon's socket is the sanctioned IPC —
    it is **not** a second receiver and does not ACK independently, so it
    does not violate the "never run a second `signal-cli receive`" rule.
  - **Push model:** a client only sees messages that arrive while
    connected (signal-cli does not replay history). So the service must run
    continuously (`Restart=always`); messages received while it is down
    survive only in the journal. To backfill a gap, read the journal for
    that window.
  - **Privacy:** `memory/signal/` is git-ignored — conversation content
    stays local beside the repo, like the maildir. Only the cursor
    (`memory/signal-state.json`, a timestamp) is committed.

- **`signalshow`** — the reader. Cursor is `memory/signal-state.json`'s
  `cursor`, **hand-advanced like the mail cursor** (not auto-bumped like
  spond): `signalshow` is the only writer of the state file; `signal-capture`
  never touches it.

**SOP (per sjekk):**

1. `uv run signalshow --since-cursor --headers-only` — one line per new
   message (`ISO  ←/→  peer  sender: text`). Filter with `--from <name>`,
   `--with <peer>` (whole thread incl. your replies), `--group`/`--no-group`.
   (In a sjekk, `sjekk-collect` already produced this listing at
   `memory/sjekk/latest/signal.txt` — read that file.)
2. Triage substantive messages as first-class sjekk input (same weight as
   mail/Spond) — surface anything actionable, file durable facts to the
   right topic file. The threshold is fuzzy; the line is roughly **"would
   the user want this remembered or acted on later?"** Above the line, and
   worth capturing:
   - **Scheduling / logistics** — dates, times, plans, changes, who's
     where when.
   - **Gift signals** — anyone expressing they want / wish for a thing
     (e.g. a partner mentioning something they'd like), or a question that
     implies gift-planning. Worth a durable note even when phrased
     casually.
   - **Explicit "remember to" / "must do" items** — anything framed as a
     todo, deadline, or thing-not-to-forget.

   **Note to Self is a first-class capture channel.** The user's own
   Signal *Note to Self* messages (peer == self) are deliberate captured
   reminders — act on each one, then file/route it; never skip a
   self-note as "noise." (This channel replaced the retired `/notes/`
   web capture page in 2026-10.)

   **Receipt photos arrive here too.** The user photographs paper
   receipts straight into Note to Self, where they show up in the
   `signalshow` triage as image attachments. Handle them like finance
   Route A (see [finance.md](finance.md)): the JSONL record carries attachment metadata only —
   pull the on-disk blob from
   `~/.local/share/signal-cli/attachments/<id>`, view it, and digest it
   into `RECEIPTS.md` (date, vendor, amount, what it was for).

   **Do NOT mention insignificant messages at all** — real-time
   coordination ("kom hit litt", "ja"), greetings, acknowledgements,
   group-chat thanks/reactions and the like are noise: leave them out of
   the sjekk summary *and* the daily note entirely. Read them only to
   confirm there's nothing actionable, then move on. Surface a Signal
   message only when it carries a fact worth keeping or an action to take.
3. `uv run signalshow --since-cursor --bump` (or just `--bump`) to advance
   the cursor to the newest message shown. Commit `memory/signal-state.json`
   under `MEM:` — the JSONL itself is git-ignored.

Tests: `uv run pytest scripts/test_signal_capture.py scripts/test_signalshow.py`
plus doctests (`uv run python -m doctest scripts/signal_capture.py
scripts/signalshow.py`). Service health: `systemctl --user status
signal-capture`. Setup/rebuild of the underlying mirror:
[../../../network/docs/signal-cli-mirror.md](../../../network/docs/signal-cli-mirror.md).
