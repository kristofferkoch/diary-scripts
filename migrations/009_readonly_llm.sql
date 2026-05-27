-- Read-only Postgres role for LLM-driven SQL.
--
-- Apply (one-time):
--   pass generate pg/llm_pr_composer 32 --no-symbols
--   sudo -u postgres psql -d mailvec \
--     -v pw="$(pass show pg/llm_pr_composer | head -1)" \
--     -f migrations/009_readonly_llm.sql
--
-- Requires Postgres superuser because it CREATE ROLE / CREATE USER. The
-- `sudo -u postgres` switches the Unix user; the $(pass …) substitution
-- still happens in user's shell (so gpg-agent resolves the secret) before
-- sudo hands off. The :'pw' substitution below is psql-side; pass the
-- value via -v pw=...
--
-- Re-running rotates the password (CREATE is guarded by DO blocks; ALTER
-- USER … WITH PASSWORD always runs).
--
-- Rationale:
--   Any consumer that feeds LLM-generated SQL into Postgres (per-mail PR
--   composer, future search landing page, ad-hoc Q&A over the mail
--   archive) must use a role that physically cannot do DDL/DML and is
--   bounded by statement_timeout. We share one SELECT-only base role
--   (mailvec_ro) across all consumers, then give each consumer its own
--   LOGIN user that inherits from it — per-user logins so we can
--   audit / revoke individually without breaking the others.
--
-- ALTER DEFAULT PRIVILEGES keeps future tables auto-readable without
-- needing to re-grant after every schema migration.

-- 1. Shared read-only base role
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mailvec_ro') THEN
        CREATE ROLE mailvec_ro NOLOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE mailvec TO mailvec_ro;
GRANT USAGE ON SCHEMA public TO mailvec_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mailvec_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO mailvec_ro;

ALTER DEFAULT PRIVILEGES FOR ROLE user IN SCHEMA public
    GRANT SELECT ON TABLES TO mailvec_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE user IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO mailvec_ro;

-- 2. Per-consumer LOGIN user (first: per-mail PR composer)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'llm_pr_composer') THEN
        CREATE USER llm_pr_composer;
    END IF;
END
$$;

ALTER USER llm_pr_composer WITH PASSWORD :'pw';
GRANT mailvec_ro TO llm_pr_composer;
ALTER ROLE llm_pr_composer SET statement_timeout = '15s';
ALTER ROLE llm_pr_composer SET idle_in_transaction_session_timeout = '30s';

-- 3. Verify (run manually after migration):
--   PG_DSN="host=localhost dbname=mailvec user=llm_pr_composer \
--     password=$(pass show pg/llm_pr_composer | head -1)"
--   psql "$PG_DSN" -c "SELECT count(*) FROM messages;"            -- returns N
--   psql "$PG_DSN" -c "DROP TABLE messages;"                       -- ERROR
--   psql "$PG_DSN" -c "INSERT INTO messages (message_id) VALUES ('x');"  -- ERROR
--   psql "$PG_DSN" -c "SELECT pg_sleep(20);"                       -- statement_timeout
