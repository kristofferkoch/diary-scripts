-- Note attachments — images captured alongside (or instead of) a note's text.
-- Backs the photo-upload feature on the /notes/ page: from a phone the user
-- can take/upload a picture (Firefox Android), which is processed by Pillow
-- (EXIF-oriented, downscaled, re-encoded JPEG — see mail_reader/note_images.py)
-- and stored here as two BYTEA blobs: a web-size image and a small thumbnail.
--
-- Bytes-in-Postgres (not files on disk) keeps the single-source-of-truth shape
-- the rest of the webapp already uses (notes_queue, shopping_items, summaries
-- all live in mailvec); a personal note's photo is a few hundred KB after
-- downscaling, well within what BYTEA/TOAST handles comfortably.
--
-- The webapp side is mail_reader/notes.py (CRUD) + mail_reader/note_images.py
-- (processing) + routes in mail_reader/server.py. ON DELETE CASCADE means
-- deleting a note (web or `scripts/notes.py rm`) reaps its images too.
--
-- Apply with:  psql -d mailvec -f migrations/013_note_attachments.sql

-- The `description*` columns are filled in lazily, NOT at upload time: a
-- vision model (e.g. qwen2.5vl on gpu-host — see the "image attachment
-- summaries" parking-lot item in IDEAS.md) interprets the picture in a
-- background pass and writes back a text description. They are versioned the
-- same way `summaries` is: `description_model` records WHICH model produced
-- the current text and `described_at` WHEN, so a model swap can re-describe
-- stale rows and the sjekk can read a note's photo without opening the image.
CREATE TABLE IF NOT EXISTS note_attachments (
    id                 BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note_id            BIGINT      NOT NULL REFERENCES notes_queue(id) ON DELETE CASCADE,
    mime_type          TEXT        NOT NULL,  -- always 'image/jpeg' after processing
    image_bytes        BYTEA       NOT NULL,  -- web-size (downscaled) JPEG
    thumb_bytes        BYTEA       NOT NULL,  -- small thumbnail JPEG for the list
    width              INT,                   -- web-size pixel dimensions (for layout)
    height             INT,
    description        TEXT,                  -- VLM interpretation of the image (lazy, nullable)
    description_model  TEXT,                  -- model id that produced `description`
    described_at       TIMESTAMPTZ,           -- when `description` was generated
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The note list resolves each note's attachment by note_id; this serves that
-- lookup and the ON DELETE CASCADE reap.
CREATE INDEX IF NOT EXISTS note_attachments_note_idx
    ON note_attachments (note_id);
