-- Summary generation state machine. See mail_reader/DESIGN.md §11.
--
-- Adds a status enum to `summaries` so multiple concurrent requests for
-- the same uncached message can dedup via atomic INSERT ON CONFLICT — the
-- claim writes a `pending` row, others see it and just render a placeholder
-- instead of double-firing Ollama.
--
-- `streaming` is reserved for a future SSE token-streaming variant; the
-- state machine accommodates it without a further schema change.
--
-- Apply with:  psql -d mailvec -f migrations/004_summary_status.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'summary_status') THEN
        CREATE TYPE summary_status AS ENUM ('pending', 'streaming', 'done', 'failed');
    END IF;
END $$;

ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS status summary_status NOT NULL DEFAULT 'done',
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS error TEXT;

-- Existing summaries with `status='done'` keep working; the default keeps
-- backfilled rows valid.

-- Partial index speeds up reclaim sweeps and "is anything in flight" probes.
CREATE INDEX IF NOT EXISTS summaries_status_inflight_idx
    ON summaries(updated_at)
    WHERE status IN ('pending', 'streaming');

-- Trigger to keep updated_at fresh on every UPDATE — but only when the
-- caller didn't already set it explicitly (so reclaim sweeps + tests can
-- backdate or pin a value without the trigger clobbering it).
CREATE OR REPLACE FUNCTION summaries_touch_updated_at() RETURNS trigger AS $$
BEGIN
    IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at = now();
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS summaries_touch_updated_at ON summaries;
CREATE TRIGGER summaries_touch_updated_at
    BEFORE UPDATE ON summaries
    FOR EACH ROW EXECUTE FUNCTION summaries_touch_updated_at();
