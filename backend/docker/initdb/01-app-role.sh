#!/bin/sh
# Runs once on first volume init. The API connects as rag_app (no superuser, no BYPASSRLS)
# so row-level security actually applies; migrations run as the owner role.
set -eu
psql -v ON_ERROR_STOP=1 -v app_pw="$APP_DB_PASSWORD" --username "$POSTGRES_USER" --dbname postgres <<'SQL'
CREATE ROLE rag_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_pw';
CREATE DATABASE rag_test OWNER rag;
SQL
