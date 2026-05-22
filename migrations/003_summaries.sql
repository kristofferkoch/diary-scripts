-- Cached LLM summaries of messages, keyed by (message, model) so we can
-- A/B different models or regenerate without losing prior outputs. v1 reads
-- the latest row for the configured model.
--
-- Apply with:  psql -d mailvec -f migrations/003_summaries.sql

CREATE TABLE IF NOT EXISTS summaries (
    id            BIGSERIAL PRIMARY KEY,
    message_id    BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    model         TEXT   NOT NULL,
    short         TEXT   NOT NULL,
    long          TEXT,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT summaries_msg_model_key UNIQUE (message_id, model)
);

CREATE INDEX IF NOT EXISTS summaries_message_idx ON summaries(message_id);
