-- Priority column for the summary worker queue.
--
-- Until now the queue was a pure FIFO over `requested_at`: newest enqueue
-- wins, and a user-bump (`SET requested_at = now()`) lets a viewed mail
-- jump ahead. That's fine for the tier-1 backlog (cheap model, catches
-- up quickly) but leaves tier-2 stuck processing newsletters before
-- coordination mail from Astrid / contractors / barnehage when a burst
-- of noise arrives.
--
-- This adds a `priority` column written by a deterministic heuristic at
-- enqueue time (see `mail_reader/priority.py`). Worker `ORDER BY` becomes
-- `priority DESC, requested_at DESC, id ASC` so high-priority always
-- beats medium, regardless of arrival order. User-bump promotes to
-- priority 3 in addition to setting requested_at — opening a mail still
-- wins over any algorithmic guess.
--
-- Priority ladder:
--   3 = known-important sender (family, active contractors, barnehage)
--   2 = unknown / default
--   1 = mild deprioritization (reserved; not used by the floor yet)
--   0 = noise (noreply, newsletters, mass-send infra)
--
-- All existing rows get the default (2). Idempotent migration.
--
-- Apply with:  psql -d mailvec -f migrations/010_priority.sql

ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 2;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'summaries_priority_range'
    ) THEN
        ALTER TABLE summaries
            ADD CONSTRAINT summaries_priority_range
            CHECK (priority BETWEEN 0 AND 3);
    END IF;
END $$;

-- Replace the queue index: priority is now the primary sort key. The
-- partial-index predicate (status = 'pending') is unchanged, so workers
-- keep scanning only the live queue.
DROP INDEX IF EXISTS summaries_queue_pending;
CREATE INDEX summaries_queue_pending
    ON summaries (quality_tier, model, priority DESC, requested_at DESC, id ASC)
    WHERE status = 'pending';
