-- Structured extraction for tier-2 summaries. See mail_reader/DESIGN.md
-- and mail_reader/IDEAS.md (2026-05-22 — extract more per qwen call).
--
-- Tier-1 (qwen2.5:3b) keeps producing a free-text `short` only. Tier-2
-- (qwen3.6:35b-a3b) is upgraded to return JSON that we shred into:
--
--   summaries.action_required      — does this need a response/payment/RSVP
--   summary_temporal               — deadlines / events / valid_until dates
--   themes  + summary_themes       — many-to-many; themes are deduped via
--                                    nearest-neighbour on bge-m3 vectors
--   entities + summary_entities    — typed kinds (person/org/place/money/
--                                    identifier/contact/url) with JSONB
--                                    `meta` for kind-specific shape
--
-- Prompt bump → p3. Existing rows become `done_stale` and regenerate
-- through the queue we already have.
--
-- Apply with:  psql -d mailvec -f migrations/007_structured_extraction.sql

-- (1) Flag for "user owes a response/action". Pairs with summary_temporal
--     rows (kind='deadline') when there's a specific date.
ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS action_required BOOLEAN NOT NULL DEFAULT FALSE;

-- (2) Dates pulled out of the body. One row per date the LLM extracts.
--     `kind` lets the UI render urgency contextually:
--       deadline    — user must act before occurs_at
--       event       — something happens on occurs_at (RSVP context)
--       valid_until — promo / offer / link expires
--       mentioned   — generic date reference, no action implied
CREATE TABLE IF NOT EXISTS summary_temporal (
    summary_id BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    kind       TEXT   NOT NULL CHECK (kind IN ('deadline', 'event', 'valid_until', 'mentioned')),
    occurs_at  DATE   NOT NULL,
    note       TEXT,
    PRIMARY KEY (summary_id, kind, occurs_at)
);

-- Agenda query: "what deadlines do I have in the next N days?"
CREATE INDEX IF NOT EXISTS summary_temporal_upcoming
    ON summary_temporal (occurs_at, kind);

-- (3) Themes — normalized many-to-many. Dedup via cosine on bge-m3.
CREATE TABLE IF NOT EXISTS themes (
    id         BIGSERIAL PRIMARY KEY,
    text       TEXT NOT NULL UNIQUE,
    embedding  vector(1024) NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS themes_hnsw
    ON themes USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS summary_themes (
    summary_id BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    theme_id   BIGINT NOT NULL REFERENCES themes(id),
    PRIMARY KEY (summary_id, theme_id)
);

-- Reverse lookup: "all summaries for a given theme" without scanning.
CREATE INDEX IF NOT EXISTS summary_themes_by_theme
    ON summary_themes (theme_id);

-- (4) Entities — single typed table, JSONB for kind-specific fields.
CREATE TABLE IF NOT EXISTS entities (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN (
        'person', 'org', 'place', 'money', 'identifier', 'contact', 'url'
    )),
    value      TEXT NOT NULL,           -- raw as extracted
    normalized TEXT NOT NULL,           -- canonical for dedup
    meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, normalized)
);

-- Future: pg_trgm-backed fuzzy lookup on `value` for "Hafslund" matching
-- "Hafslund AS". Skipped here because pg_trgm isn't installed on this
-- cluster; dedup goes through `normalized` instead. To enable later:
--   CREATE EXTENSION pg_trgm;
--   CREATE INDEX entities_value_trgm ON entities USING gin (value gin_trgm_ops);

CREATE TABLE IF NOT EXISTS summary_entities (
    summary_id BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    entity_id  BIGINT NOT NULL REFERENCES entities(id),
    PRIMARY KEY (summary_id, entity_id)
);

CREATE INDEX IF NOT EXISTS summary_entities_by_entity
    ON summary_entities (entity_id);
