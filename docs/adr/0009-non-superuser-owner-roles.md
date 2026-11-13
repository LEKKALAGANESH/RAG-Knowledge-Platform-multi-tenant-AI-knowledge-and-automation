# ADR 0009: Non-superuser table owner and a BYPASSRLS-only definer role

**Status:** accepted (FX-6, migration `0011`). Amends ADR 0005.

## Context

Until `0011`, the compose superuser `rag` ran migrations and owned every table and every SECURITY
DEFINER function (finding S14). Their bodies are safe today, but any future bug in a definer function
would run as a superuser, and a superuser can run `COPY ... PROGRAM` (code execution on the DB host).

The obvious fix ("make the owner a normal role") does not work alone. Every table uses `FORCE ROW
LEVEL SECURITY`, so RLS applies to the table owner too. The cross-tenant definer functions
(`api_key_lookup`, `due_sync_orgs`, `revoke_member_sessions`, `auth_user_by_email`, `my_invites`,
`respond_invite`) use `SET row_security = off`. For a role without BYPASSRLS, that setting makes any
RLS-covered query fail with an error. A plain owner would break all of them.

## Decision

There are three roles, created by `backend/docker/initdb/02-owner-role.sh`:

| Role | Attributes | Owns / does |
|---|---|---|
| `rag` (POSTGRES_USER) | superuser | Bootstrap only: roles, database ownership, `CREATE EXTENSION vector`. No app or migration URL uses it |
| `rag_owner` | LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE; owns the `rag` database (so it gets CREATE on `public`) | Runs migrations (`MIGRATION_DATABASE_URL`). Owns all tables, indexes, sequences, and the non-bypassing definer `embedding_hnsw` (CREATE INDEX needs table ownership) |
| `rag_definer` | NOLOGIN, NOSUPERUSER, BYPASSRLS; USAGE+CREATE on `public`; SELECT/UPDATE/INSERT on only the tables the bypassing bodies touch | Owns every SECURITY DEFINER function with `row_security = off` |
| `rag_app` | unchanged (LOGIN, NOSUPERUSER, NOBYPASSRLS) | API and workers |

`0011` assigns ownership by rule, not by name. A definer function with `row_security=off` goes to
`rag_definer`. Every other function, and every relation in `public`, goes to `rag_owner`. Extension
members are skipped. The same migration resets `rag_app`'s default privileges for tables created by
`rag_owner`, and makes `messages` follow its conversation (finding N6).

`rag_owner` is a member of `rag_definer`, so migrations can hand new definer functions to it. This
also lets `rag_owner` `SET ROLE rag_definer`. That gives it nothing new: as the table owner it can
already turn RLS off on its own tables.

## Consequences

- No app, worker or migration connection is a superuser. A definer bug is limited to
  `rag_definer`'s grants: no `COPY PROGRAM`, no DDL on tables, no access to other databases.
- **Future migrations** that add a cross-tenant definer function must (1) grant `rag_definer`
  exactly the privileges the body needs, then (2) `ALTER FUNCTION ... OWNER TO rag_definer`. A
  bypassing function left with `rag_owner` fails loudly (`row_security` error); it does not leak.
- **Data migrations** run under FORCE RLS as `rag_owner`. Without `app.org_id` set, an
  `UPDATE`/`DELETE` sees zero rows. Set the tenant GUCs, or temporarily `NO FORCE` the table inside the
  migration.
- Maintenance writes that used to rely on the superuser owner (for example deleting a `users` row,
  which has no DELETE policy) must use the superuser bootstrap role. `evals/run_eval.py` already uses
  its own superuser URL.
- Existing volumes: run `02-owner-role.sh` once, then `alembic upgrade head` **as the superuser**
  (0011 moves ownership off it), then point `MIGRATION_DATABASE_URL` at `rag_owner`. See DEPLOYMENT.
- Tests: `MIGRATION_DATABASE_URL` is `rag_owner`. `TEST_ADMIN_DATABASE_URL` (the superuser) creates
  the test DB and the extension only.

## Alternatives considered

- **`rag_owner` with BYPASSRLS.** One role, but then every migration and every definer function
  bypasses RLS. Rejected.
- **Drop `FORCE` so the owner bypasses RLS naturally.** This weakens the defence-in-depth that
  `FORCE` gives across 20+ tables, and every owner session would silently skip RLS. Rejected.
- **Definer functions without `row_security = off`.** RLS would apply with the caller's GUCs. That
  defeats their purpose (cross-tenant lookups) and fails silently by returning nothing. Rejected.
