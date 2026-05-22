-- Attachments: track every MIME part with filename / Content-Disposition: attachment
-- on a message. Body parts are NOT in here (they live as message_id-only chunks).
-- For text-extractable types (PDF, DOCX, ODT, text/*, ICS) we store the extracted
-- text as one or more rows in `chunks` with attachment_id set.
-- For images / unsupported types we still insert the metadata row (text_chars=0)
-- so a later image / VLM pass can find them with `WHERE text_chars = 0`.
--
-- Apply with:  psql -d mailvec -f migrations/002_attachments.sql

CREATE TABLE IF NOT EXISTS attachments (
    id              BIGSERIAL PRIMARY KEY,
    message_id      BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename        TEXT,
    mime_type       TEXT NOT NULL,
    size_bytes      INTEGER,
    text_chars      INTEGER NOT NULL DEFAULT 0,   -- post-extraction; 0 = not extracted
    extracted_at    TIMESTAMPTZ DEFAULT now()
);
-- No UNIQUE: idempotency is enforced at the messages level (messages.message_id
-- is UNIQUE), so we only insert attachments when processing a fresh message.
-- A single mail can have duplicate filenames (e.g. multiple inline image001.png).

CREATE INDEX IF NOT EXISTS attachments_message_idx ON attachments (message_id);
CREATE INDEX IF NOT EXISTS attachments_mime_idx    ON attachments (mime_type);

-- Chunks can now come from either the message body (attachment_id NULL) or a
-- specific attachment. Uniqueness needs NULLS NOT DISTINCT (PG15+) so the
-- (msg, NULL, idx) body slots collide as expected.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS attachment_id BIGINT
    REFERENCES attachments(id) ON DELETE CASCADE;

ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_message_id_chunk_idx_key;
ALTER TABLE chunks ADD CONSTRAINT chunks_msg_att_idx_key
    UNIQUE NULLS NOT DISTINCT (message_id, attachment_id, chunk_idx);

CREATE INDEX IF NOT EXISTS chunks_attachment_idx ON chunks (attachment_id);
