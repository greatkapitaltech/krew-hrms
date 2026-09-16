-- Provision the krew-hrms database (krew-dev-db) and roles on its dedicated
-- RDS instance (krew-dev-database, PostgreSQL 16).
--
-- Run as the RDS master user (krew_admin). The instance is private, so this
-- has to run from inside vpc-014af4749a26d6a9f — see docs/deploy.md.
--
--   psql "host=krew-dev-database.<...>.ap-south-1.rds.amazonaws.com \
--         user=krew_admin dbname=postgres sslmode=require" \
--        -v owner_pw="'...'" -v app_pw="'...'" \
--        -f scripts/provision-rds-dev.sql
--
-- Passwords are passed in as psql variables so they never live in git.
-- Put the real values in Secrets Manager and reference them from the ECS task.

\set ON_ERROR_STOP on

-- Two roles, because row-level security depends on the app NOT owning its
-- tables. A table owner bypasses RLS unless the table is FORCE'd, and a
-- superuser bypasses it unconditionally. See docs/redis-and-rls.md.
--
--   krew_owner  owns the schema, runs migrations. Never serves traffic.
--   krew_app    runtime role. Owns nothing, so policies actually apply.

-- psql does not substitute :variables inside dollar-quoted bodies, so role
-- creation is driven through \gexec instead of a DO block. :'name' interpolates
-- the value as a correctly quoted SQL literal.
SELECT format('CREATE ROLE krew_owner LOGIN PASSWORD %L', :'owner_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'krew_owner')
\gexec

SELECT format('CREATE ROLE krew_app LOGIN PASSWORD %L', :'app_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'krew_app')
\gexec

-- Neither role may sidestep policy. SUPERUSER and BYPASSRLS can only be changed
-- by a real superuser, which the RDS master user is not — but roles are created
-- without either attribute, so the correct move is to VERIFY rather than set.
-- This aborts the script if a role could bypass the policies we are about to add.
ALTER ROLE krew_owner NOCREATEDB;
ALTER ROLE krew_app   NOCREATEDB NOCREATEROLE;

DO $verify$
DECLARE bad text;
BEGIN
    SELECT string_agg(rolname, ', ') INTO bad
    FROM pg_roles
    WHERE rolname IN ('krew_owner', 'krew_app')
      AND (rolsuper OR rolbypassrls);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'Role(s) % can bypass row-level security. Fix before proceeding.', bad;
    END IF;
    RAISE NOTICE 'Verified: krew_owner and krew_app cannot bypass row-level security.';
END
$verify$;

-- On RDS the master user is not a true superuser, so it must be a member of
-- krew_owner to create a database owned by it.
GRANT krew_owner TO krew_admin;

-- CREATE DATABASE cannot run inside a transaction block or a DO block, so this
-- is guarded by \gexec instead.
-- The name is hyphenated to match the local development database, so it must be
-- quoted everywhere it appears in SQL. Django quotes identifiers itself, so only
-- hand-written SQL like this needs the care.
SELECT format('CREATE DATABASE %I OWNER krew_owner ENCODING ''UTF8''', 'krew-dev-db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'krew-dev-db')
\gexec

\connect "krew-dev-db"

-- Lock down the public schema: only the owner creates objects.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO krew_owner;
GRANT USAGE ON SCHEMA public TO krew_app;

-- krew_app reads and writes rows but never changes structure.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO krew_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO krew_app;

-- The same grants for tables migrations create later, so a new model does not
-- silently become unreadable by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE krew_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO krew_app;
ALTER DEFAULT PRIVILEGES FOR ROLE krew_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO krew_app;

\echo ''
\echo 'Provisioned. Verify with:'
\echo '  SELECT rolname, rolsuper, rolbypassrls FROM pg_roles'
\echo "   WHERE rolname IN ('krew_owner','krew_app');"
\echo ''
\echo 'Both MUST show f / f, otherwise row-level security will silently do nothing.'
