-- A read-only role for human access from a GUI client (DBeaver, psql, etc.).
--
-- Deliberately separate from the two application roles:
--   krew_owner  owns the schema and can drop it — never hand this to a GUI
--   krew_app    the application's own identity; sharing it makes audit logs lie
--   krew_reader this role: SELECT only, nothing else
--
-- Read-only because a GUI client is exactly where an accidental UPDATE without
-- a WHERE clause happens. Widening it later is one GRANT; explaining a silent
-- data change is not.
--
-- Run as the RDS master user against krew-dev-db:
--   psql "host=<endpoint> user=krew_admin dbname=krew-dev-db sslmode=require" \
--        -v reader_pw="<password>" -f scripts/provision-rds-reader.sql

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE krew_reader LOGIN PASSWORD %L', :'reader_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'krew_reader')
\gexec

ALTER ROLE krew_reader NOCREATEDB NOCREATEROLE;

-- Must not sidestep row-level security either: once RLS lands, a human browsing
-- in DBeaver should see exactly what the policies allow, not more.
DO $verify$
DECLARE bad boolean;
BEGIN
    SELECT rolsuper OR rolbypassrls INTO bad FROM pg_roles WHERE rolname = 'krew_reader';
    IF bad THEN
        RAISE EXCEPTION 'krew_reader can bypass row-level security. Fix before use.';
    END IF;
    RAISE NOTICE 'Verified: krew_reader cannot bypass row-level security.';
END
$verify$;

GRANT CONNECT ON DATABASE "krew-dev-db" TO krew_reader;
GRANT USAGE ON SCHEMA public TO krew_reader;

GRANT SELECT ON ALL TABLES    IN SCHEMA public TO krew_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO krew_reader;

-- Tables created by future migrations must be readable too, otherwise every new
-- model silently becomes invisible in DBeaver until someone re-runs a GRANT.
ALTER DEFAULT PRIVILEGES FOR ROLE krew_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO krew_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE krew_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO krew_reader;

\echo ''
\echo 'krew_reader provisioned (read-only).'
\echo 'To allow writes later:'
\echo '  GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO krew_reader;'
