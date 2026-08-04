-- Widen chunks.embedding to the served model's full native width.
-- Apply with:  psql -d mailvec -f migrations/015_full_width_vectors.sql
--
-- Qwen3-Embedding on llama-server is natively 4096-dim; until 2026-08-03
-- vectors were truncated MRL-style to 1024 dims client-side (EMBED_DIMS).
-- The 1024-d vectors cannot be widened retroactively, so chunks/attachments
-- are rebuilt from notmuch with `embed-mail --reembed` after this migration
-- (resumable — re-run if interrupted).
--
-- themes.embedding stays vector(1024): the themes pipeline was retired
-- 2026-08-02 and nothing queries those vectors any more.
--
-- Stop mail-sync.timer before applying; restart it after the reembed.

DROP INDEX IF EXISTS chunks_embed_hnsw;

TRUNCATE chunks, attachments;

ALTER TABLE chunks
    ALTER COLUMN embedding TYPE vector(4096);

-- Rebuild after `embed-mail --reembed` has completed (build after bulk
-- insert for speed). pgvector's HNSW caps indexes at 2000 dims (vector) /
-- 4000 dims (halfvec), so the index uses an MRL-truncated prefix (4000 of
-- 4096 dims, sanctioned for Qwen3-Embedding) cast to halfvec; search_mail
-- matches the index expression on both sides:
-- CREATE INDEX chunks_embed_hnsw ON chunks USING hnsw ((subvector(embedding, 1, 4000)::halfvec(4000)) halfvec_cosine_ops);
