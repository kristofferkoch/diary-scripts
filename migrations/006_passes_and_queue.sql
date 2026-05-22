-- Multi-pass summaries + priority queue. See mail_reader/DESIGN.md.
--
-- Two changes in one migration since they're coupled:
--
--   (1) Allow multiple `done` rows per (message_id, model, prompt_version)
--       triple. The strict UNIQUE is replaced by a partial unique index
--       that ONLY enforces "at most one in-flight row" — terminal rows
--       (done/failed) accumulate as history for quality assessments.
--
--   (2) Add `quality_tier` (which pass produced this row) and
--       `requested_at` (priority for the worker queue). Bumping a row's
--       priority = `UPDATE … SET requested_at = now()`. Workers pull in
--       requested_at DESC order, so currently-viewed items rise to the
--       top while abandoned ones drift down.
--
-- Apply with:  psql -d mailvec -f migrations/006_passes_and_queue.sql

-- Drop the strict unique — multiple terminal rows are now allowed.
ALTER TABLE summaries
    DROP CONSTRAINT IF EXISTS summaries_msg_model_version_key;

-- Per-row quality tier. Existing rows came from qwen3.6:35b-a3b (tier 2).
ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS quality_tier SMALLINT;
UPDATE summaries SET quality_tier = 2 WHERE quality_tier IS NULL;
ALTER TABLE summaries ALTER COLUMN quality_tier SET NOT NULL;
ALTER TABLE summaries ALTER COLUMN quality_tier SET DEFAULT 1;

-- Worker-queue priority. Higher requested_at = earlier in line.
ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Replacement lock: at most one in-flight row per (mid, model, version).
CREATE UNIQUE INDEX IF NOT EXISTS summaries_inflight_lock
    ON summaries (message_id, model, prompt_version)
    WHERE status IN ('pending', 'streaming');

-- Worker queue: pull newest requested_at first within each tier+model.
CREATE INDEX IF NOT EXISTS summaries_queue_pending
    ON summaries (quality_tier, model, requested_at DESC, id ASC)
    WHERE status = 'pending';

-- Reader: best done by tier desc then recency, within a (mid, version).
CREATE INDEX IF NOT EXISTS summaries_best_done
    ON summaries (message_id, prompt_version, quality_tier DESC, generated_at DESC)
    WHERE status = 'done';
