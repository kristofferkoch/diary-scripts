-- Non-mail sources: Signal / Spond / Vigilo JSONL records share the
-- messages/chunks/attachments store with mail (embedded by embed-messages).
-- Apply with:  psql -d mailvec -f migrations/016_sources.sql
--
-- `source` tags where a row came from ('mail' for everything embedded
-- before this migration). Query-side consumers that only make sense for
-- mail (mail_reader.related's tankekart) filter on it.
--
-- `content_hash` is embed-messages' change-detection key: mail is immutable
-- so those rows stay NULL; the mutable sources (Spond RSVP/reschedule,
-- Vigilo message_update) re-emit records, and a hash mismatch triggers
-- delete + re-insert of the whole message row.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'mail';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS messages_source_idx ON messages (source);
