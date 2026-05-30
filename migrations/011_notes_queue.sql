-- Notes queue — a capture inbox for "things I don't want to forget".
-- The user types free-text notes into the /notes/ page on server;
-- each lands here as a pending row. A check round (the daily "sjekk")
-- digests them: scripts/notes.py lists pending notes, the agent acts on
-- them (files a calendar entry, updates a topic file, etc.), then marks
-- them done — at which point they drop off the page but stay in the
-- table as a record of what was captured.
--
-- Apply with:  psql -d mailvec -f migrations/011_notes_queue.sql

CREATE TABLE IF NOT EXISTS notes_queue (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    body        TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The page lists pending notes newest-first; the index serves that scan.
CREATE INDEX IF NOT EXISTS notes_queue_pending_idx
    ON notes_queue (created_at DESC)
    WHERE status = 'pending';
