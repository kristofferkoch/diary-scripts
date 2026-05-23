-- Agenda dismissals. Tier-2 extracts deadlines / events / valid_until
-- dates per summary; the mail-reader's agenda strip aggregates them.
-- When the user has handled an item (paid the bill, RSVP'd, etc.) they
-- shouldn't see it again on the strip until next occurrence — which,
-- for a one-off deadline, is never.
--
-- Keyed on (thread_id, kind, occurs_at) — exactly the dedup key the
-- agenda query already uses, so a single dismissal silences the entire
-- thread for that (kind, date), not just the message that triggered it.
--
-- Apply with:  psql -d mailvec -f migrations/008_agenda_dismissed.sql

CREATE TABLE IF NOT EXISTS agenda_dismissed (
    thread_id     TEXT        NOT NULL,
    kind          TEXT        NOT NULL CHECK (kind IN (
        'deadline', 'event', 'valid_until', 'mentioned'
    )),
    occurs_at     DATE        NOT NULL,
    dismissed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, kind, occurs_at)
);

-- The agenda query already filters by `occurs_at >= current_date`; the
-- index covers the join condition (thread_id, kind, occurs_at).
