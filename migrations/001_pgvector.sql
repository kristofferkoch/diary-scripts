-- Schema for embedded mail archive.
-- Apply with:  psql -d mailvec -f migrations/001_pgvector.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    message_id      TEXT UNIQUE NOT NULL,
    date            TIMESTAMPTZ,
    from_addr       TEXT,
    to_addrs        TEXT,
    subject         TEXT,
    thread_id       TEXT,
    tier            SMALLINT NOT NULL,         -- 1=last1y, 2=thread-of-mine, 3=known-sender
    body_chars      INTEGER,                   -- post-stripping
    embedded_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_date_idx    ON messages (date DESC);
CREATE INDEX IF NOT EXISTS messages_from_idx    ON messages (from_addr);
CREATE INDEX IF NOT EXISTS messages_thread_idx  ON messages (thread_id);
CREATE INDEX IF NOT EXISTS messages_tier_idx    ON messages (tier);

-- bge-m3 = 1024d. Switch to vector(768) for nomic-embed-text.
CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    message_id      BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_idx       SMALLINT NOT NULL,
    text            TEXT NOT NULL,
    embedding       vector(1024) NOT NULL,
    UNIQUE (message_id, chunk_idx)
);

-- HNSW on cosine distance; build after bulk insert for speed.
-- CREATE INDEX chunks_embed_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
