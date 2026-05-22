-- Prompt versioning for cached summaries.
--
-- The same model can be re-prompted in better ways over time; we want to
-- keep old rows around (so the user sees *something* immediately) while
-- generating a fresh one at the new prompt version. The reader prefers
-- the current version; if only older exists, it returns the old row with
-- is_stale=true so the caller can schedule a background regen.
--
-- Apply with:  psql -d mailvec -f migrations/005_summary_prompt_version.sql

ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS prompt_version TEXT NOT NULL DEFAULT 'p1';

-- Move uniqueness from (message_id, model) to (message_id, model, prompt_version)
-- so the same mail can have one row per prompt revision.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'summaries_msg_model_key'
    ) THEN
        ALTER TABLE summaries DROP CONSTRAINT summaries_msg_model_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'summaries_msg_model_version_key'
    ) THEN
        ALTER TABLE summaries
            ADD CONSTRAINT summaries_msg_model_version_key
            UNIQUE (message_id, model, prompt_version);
    END IF;
END $$;
