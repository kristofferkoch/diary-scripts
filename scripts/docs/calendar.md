# Calendar — `retire_calendar.py`

> Index: [scripts/](../README.md)

Part of the daily **sjekk-flow** (mail + spond + retire). The user's
daily "sjekk" (typically phrased "les / sjekk mail og spond") includes
retiring expired one-off events from `CALENDAR.md` — without this step,
the calendar slowly fills with past entries and the top of the file stops
being a useful "what's next" view.

### Mechanical step — use the tool

```bash
uv run retire-calendar --dry-run            # preview
uv run retire-calendar                      # apply, today = date.today()
uv run retire-calendar --today 2026-06-01   # simulate a later date
```

Cuts every event line whose end-date is strictly before today's
`currentDate` out of `## One-off events by month` in `CALENDAR.md`,
inserts it into the matching `### <Month> <Year>` subheading in
`CALENDAR-PAST.md` (creating the subheading in chronological order if
missing), and drops any month subheading that ends up empty. Idempotent
— re-running with nothing to do leaves both files byte-identical.
Recurring weekly/monthly/quarterly sections are never touched.

### Manual step — enrichment

In a sjekk the collector (`sjekk-collect`) has already run the tool — the
moved lines are captured in `memory/sjekk/latest/calendar-retire.txt`.
After running the script, look through the lines it moved (printed on
stderr). For any event the daily-note sweep gave new context to — an
action's outcome, who showed up, what was decided — open
`CALENDAR-PAST.md` and **append a short tail** to that moved line
referencing `[[memory/YYYY-MM-DD.md]]`. Past entries become more useful
as snapshots when they point back at the day's notes. This part is a
judgment call and stays manual.

Today's events (with end-date == today) stay in `CALENDAR.md` until the
day is over. Format / parser-contract is identical in both files; see
[../../../CALENDAR-RULES.md](../../../CALENDAR-RULES.md).

### Kindle refresh — don't poke it, let the device pull

**Do not try to refresh the wall Kindle at the end of a sjekk.** The
device sleeps, so the poke always fails — just commit and let it pull.

The wall Kindle renders the [kindle_dashboard](kindle-dashboard.md)
PNG, whose calendar and spond blocks are scraped live from `CALENDAR.md`
and `/var/lib/diary/legacy-state/spond/*.jsonl`. The dashboard *generator* picks up edits on
its next request automatically — but the **device re-fetches only on its
own scheduled wake**.

**As of 2026-05-29 the device suspends to RAM between refreshes** (battery
fix — see [KINDLE.md](../../../KINDLE.md)). While suspended its WiFi is off, so
**`ssh kindle 'initctl restart dashboard'` reliably fails with "No route to
host" — the poke can't reach a sleeping device.** Don't run it. Two
things follow for the sjekk:

- **Just commit and let it poll.** A change to the today..+3 agenda
  appears on the next scheduled wake: **≤15 min** off-peak, **≤5 min**
  during the peak windows (06:00–09:00, 15:00–20:00). For a sjekk that's
  always fine — **no action needed, and no poke to attempt.**
- **Need it on the wall *now*?** That's a **human** job: press the
  Kindle's **power button** (a short press) — `snvs-powerkey` wakes it
  from suspend and the loop re-fetches + re-renders within seconds. That's
  the only reliable on-demand refresh, and it's the user's call — not an
  agent step. Deploying device changes from server has the same problem —
  see the "deploy on next wake" catcher pattern used on 2026-05-29.

**Only bother at all when the change is actually visible on the wall.** The agenda
block (`kindle_dashboard/data.py` → `calendar_block`, `days_ahead=3`)
renders **today + the next 3 days** — so refresh only when an
added/retired/edited CALENDAR.md line (or a Spond RSVP change) falls
within `today..today+3`. A change to an event further out is invisible on
the agenda and **does not warrant a poke**. (The month-grid busy-dots
cover the whole current month, but they're low-signal — don't refresh
just for a dot.) Skip the poke entirely for mail-only triage, finance,
or any pass that touched nothing the dashboard renders.

Lesson 2026-05-29: poked the Kindle mid-sjekk *before* adding the next
day's training to CALENDAR.md, so the wall display stayed stale until
re-poked. Same day, 2nd pass: poked after adding only far-future lines
(cruise 21.07, sommeravslutning 09.06) — unnecessary, nothing in the
today..+3 window changed.

**Pushing to the device is now largely obsolete — the device pulls.**
Earlier plans here were to have a git/file hook poke the Kindle on
`CALENDAR.md` / `/var/lib/diary/legacy-state/spond/*.jsonl` change (the watcher's
`_poke_kindle()` pattern). Suspend kills that: you can't push to a
sleeping device. The device's own ≤15 min / ≤5 min poll is the mechanism
now, and the power button covers the rare "show it this instant" case.
The only push still worth wiring would be a wake-capable channel (BLE,
WoWLAN) — not worth it for a wall calendar. Leave the LLM out of it:
commit the change and let the device poll.

### Script behavior reference

- Operates only inside `## One-off events by month`. Recurring weekly /
  monthly / quarterly sections are skipped.
- Decision is on **end-date** for date spans (so `2026-06-29 – 2026-07-03`
  retires under July when 2026-07-04+ arrives).
- Drops `### Month Year` subheadings that end up empty in `CALENDAR.md`.
- New month sections in `CALENDAR-PAST.md` are inserted chronologically.
- Idempotent: when nothing is expired, both files are left byte-identical
  (no spurious whitespace churn).
- The `[[memory/YYYY-MM-DD.md]]`-tail enrichments on moved lines are a
  manual judgment call; the script intentionally does not touch the
  line body.

Tests: `uv run pytest scripts/test_retire_calendar.py` (run from inside `diary-scripts/`).
