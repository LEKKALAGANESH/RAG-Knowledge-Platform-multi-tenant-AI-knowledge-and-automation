#!/bin/sh
# S14 / ADR 0009: tables are owned by rag_owner (no superuser, no BYPASSRLS), and the definer
# functions that must read across RLS by rag_definer (NOLOGIN, BYPASSRLS, nothing else).
# The superuser (POSTGRES_USER) only bootstraps: roles, database ownership, extensions.
# Idempotent. On a volume created before this file existed, run it once by hand:
#   docker compose exec -e OWNER_DB_PASSWORD=... db sh /docker-entrypoint-initdb.d/02-owner-role.sh
set -eu
psql -v ON_ERROR_STOP=1 -v owner_pw="$OWNER_DB_PASSWORD" --username "$POSTGRES_USER" --dbname postgres <<'SQL'
SELECT 'CREATE ROLE rag_owner' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_owner')
\gexec
ALTER ROLE rag_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'owner_pw';
SELECT 'CREATE ROLE rag_definer' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_definer')
\gexec
ALTER ROLE rag_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
-- Lets migrations (as rag_owner) hand new definer functions to rag_definer.
GRANT rag_definer TO rag_owner;
SELECT format('ALTER DATABASE %I OWNER TO rag_owner', datname) FROM pg_database
WHERE datname IN ('rag', 'rag_test')
\gexec
\connect rag
CREATE EXTENSION IF NOT EXISTS vector;
SQL
