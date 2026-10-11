-- VLM image descriptions: embed-images walks image attachments that text
-- extraction left at text_chars=0, describes them with a vision-language
-- model, and stores the description as ordinary chunks with attachment_id
-- set (same retrieval path as extracted attachment text).
-- Apply with:  psql -d mailvec -f migrations/017_vlm.sql
--
-- vlm_model is the state marker:
--   NULL              not attempted yet (pending)
--   <model id>        described by this model (e.g. 'qwen3.8-flash-next')
--   'skipped:tiny'    below the size floor (icons / tracking pixels)
--   'skipped:no-blob' no local blob can ever exist (vigilo news attachments
--                     download over the API at show time) — terminal mark,
--                     no attempts burned
--   'error'           gave up after vlm_attempts reached 3
-- vlm_attempts counts failures, so a permanently broken blob or endpoint
-- eventually leaves the pending set instead of clogging every run.

ALTER TABLE attachments ADD COLUMN IF NOT EXISTS vlm_model TEXT;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS vlm_attempts SMALLINT NOT NULL DEFAULT 0;
