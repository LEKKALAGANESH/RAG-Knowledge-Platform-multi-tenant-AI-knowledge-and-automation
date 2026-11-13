# Database schema

PostgreSQL 16 with the `vector` extension. Source: `backend/alembic/versions/0001`–`0012`. Head is
`0012`.

| Rev | Change |
|---|---|
| 0001–0008 | Core tables, embeddings, conversations/usage, sources, `due_sync_orgs`, agent, API keys + audit log, sessions |
| 0009 | `sources.last_error`, `sources.needs_reconnect` (source lifecycle) |
| 0010 | `users` RLS + column grants, `org_invites`, `auth_user_by_email` / `my_invites` / `respond_invite` |
| 0011 | Ownership moves to `rag_owner` / `rag_definer` (ADR 0009); `messages` RLS follows its conversation |
| 0012 | `usage_events.kind` adds `agent`; `audit_log.actor_email`; `due_sync_orgs` floors `stale_after` at 1 h; `prune_sessions()` |

## Roles

| Role | Created by | Used for |
|---|---|---|
| `rag` (superuser in compose) | `POSTGRES_USER` in `docker-compose.yml` | Bootstrap only: roles, database ownership, `CREATE EXTENSION vector`. No app or migration URL uses it |
| `rag_owner` (LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB) | `backend/docker/initdb/02-owner-role.sh` | Migrations (`MIGRATION_DATABASE_URL`); owns the database, every table, index and sequence, and `embedding_hnsw` |
| `rag_definer` (NOLOGIN, BYPASSRLS) | `backend/docker/initdb/02-owner-role.sh` | Owns the SECURITY DEFINER functions with `row_security = off`; grants only on the tables their bodies touch |
| `rag_app` (LOGIN, NOSUPERUSER, NOBYPASSRLS) | `backend/docker/initdb/01-app-role.sh` (0001 also creates it NOLOGIN if missing) | API, worker, scheduler (`DATABASE_URL`) |

`rag_app` gets `SELECT, INSERT, UPDATE, DELETE` on tables by default privileges, then narrower grants
where noted below. Migration `0011` moved ownership from `rag` to `rag_owner` / `rag_definer`
([ADR 0009](adr/0009-non-superuser-owner-roles.md)).

## Tenant context

Every policy reads two transaction-local settings, set by `app/core/db.py` at the start of every
transaction:

```sql
NULLIF(current_setting('app.org_id', true), '')::uuid    -- ORG
NULLIF(current_setting('app.user_id', true), '')::uuid   -- USER
```

All tenant tables have `ENABLE` **and** `FORCE ROW LEVEL SECURITY`. Composite foreign keys
`(org_id, parent_id)` stop a row from pointing at another org's parent (RLS does not apply to FK
checks).

## Tables

### orgs (0001)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | text | |
| settings | jsonb `{}` | `monthly_budget_usd` (string decimal or null), `budget_action` (`block`/`warn`), `agent_approve_reversible_writes` (bool) |
| created_at | timestamptz | |

RLS `tenant_isolation`: USING `id = ORG OR id IN (SELECT org_id FROM memberships)`; WITH CHECK `id = ORG`.

### users (0001)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| email | text UNIQUE | stored lower-case |
| pw_hash | text | argon2 (pwdlib) |
| created_at | timestamptz | |

RLS (0010, FORCE): `user_visibility` FOR SELECT `id = USER OR id IN (SELECT user_id FROM memberships)`
(own row + members of the current org, since memberships RLS limits the subquery);
`user_self_insert` WITH CHECK `id = USER`. `rag_app` has `SELECT (id, email, created_at)` and
`INSERT` only: no `pw_hash` read, no UPDATE/DELETE (cleanup runs as the owner role). Login reads the hash via `auth_user_by_email`.

### memberships (0001)
`org_id` → orgs, `user_id` → users, `role` CHECK `owner|admin|editor|viewer`, `created_at`.
PK `(org_id, user_id)`. RLS: USING `org_id = ORG OR user_id = USER`; WITH CHECK `org_id = ORG`.

### api_keys (0001, 0007)
`id`, `org_id`, `user_id` (creator), `name`, `prefix` (UNIQUE, 12 hex), `hash` (HMAC-SHA256 hex),
`scopes text[]`, `created_at`, `revoked_at`, `expires_at` (0007), `last_used_at` (0007).
RLS: `org_id = ORG`.

### knowledge_bases (0001, 0002)
`id`, `org_id`, `name`, `settings jsonb`, `embedding_index_id` (FK `(org_id, embedding_index_id)` →
embedding_indexes, 0002), `created_at`. UNIQUE `(org_id, id)`. RLS: `org_id = ORG`.

### documents (0001, 0004)
| Column | Type | Notes |
|---|---|---|
| id, org_id, kb_id | uuid | FK `(org_id, kb_id)` → knowledge_bases ON DELETE CASCADE |
| source_id | uuid null | FK `(org_id, kb_id, source_id)` → sources (0004) CASCADE; null = upload |
| name, mime, sha256, blob_key | text | |
| version | int, default 1 | |
| status | text | `queued|processing|ready|failed|deleted` |
| error | text | readable failure or retry note |
| warnings | jsonb `[]` | e.g. `page 2: no text, OCR not enabled` |
| meta | jsonb `{}` | `size`; connector metadata (urls, owners, record/table) |
| external_key, content_hash | text (0004) | connector item key and change marker |
| created_at | timestamptz | |

Constraints: UNIQUE `(org_id, kb_id, id)`, UNIQUE `(source_id, external_key)`, partial UNIQUE index
`documents_upload_sha_key (kb_id, sha256, version) WHERE source_id IS NULL` (0004 replaced the
0001 table-wide unique). RLS: `org_id = ORG`.

### chunks (0001, 0002, 0003)
`id`, `org_id`, `kb_id`, `document_id` (FK `(org_id, kb_id, document_id)` → documents CASCADE),
`parent_id` (self FK, unused so far), `ord`, `text`, `loc jsonb` (`page`, `section`, `table`,
`sheet`, `range`, `url`, `record`), `tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text))`,
`created_at`. GIN index on `tsv`; UNIQUE `(document_id, ord)` (0003); UNIQUE `(org_id, id)` (0002).
RLS: `org_id = ORG`.

### embedding_indexes (0002)
`id`, `org_id`, `kb_id` (FK → knowledge_bases CASCADE), `provider`, `model`, `dim` (1–2000),
`version` (UNIQUE per KB), `status` `building|active|retired`, `created_at`. RLS: `org_id = ORG`.

### chunk_embeddings (0002)
`chunk_id`, `index_id`, `org_id`, `embedding vector` (untyped; each index has its own dim). PK
`(index_id, chunk_id)`. FKs to chunks and embedding_indexes (CASCADE). HNSW indexes are created per
embedding index at runtime by `embedding_hnsw()`:
`chunk_embeddings_hnsw_<index id> ON chunk_embeddings USING hnsw ((embedding::vector(dim)) vector_cosine_ops) WHERE index_id = '<id>'`.
RLS: `org_id = ORG`. Empty when `VECTOR_STORE=qdrant`: vectors then live in the Qdrant collection
`rag_<index id hex>` (ADR 0010); `embedding_indexes` rows are unchanged.

### conversations (0003)
`id`, `org_id`, `user_id`, `kb_ids uuid[]`, `title`, `created_at`. RLS: `org_id = ORG AND user_id = USER`
(private to the author).

### messages (0003)
`id`, `org_id`, `conversation_id` (FK → conversations CASCADE), `role` `user|assistant`, `content`,
`citations jsonb` (`n, chunk_id, document_id, document_name, kb_id, loc, excerpt`), `usage jsonb`
(provider usage + `abstained`, optional `interrupted`), `created_at`. RLS: `org_id = ORG`.

### usage_events (0003)
`id`, `org_id`, `user_id` (null for worker jobs; SET NULL on user delete), `kind` `chat|embed|rewrite|agent`,
`provider`, `model`, `in_tok`, `out_tok`, `cost_usd numeric(12,6)` (null = unknown price),
`request_id`, `created_at`. Index `(org_id, created_at DESC)`. RLS: `org_id = ORG`. Agent model turns
are recorded as `agent` (0012; `chat` before).

### sources (0004)
`id`, `org_id`, `kb_id`, `type`, `name`, `config jsonb`, `secret_enc bytea` (Fernet), `status`
`idle|syncing|error|disabled`, `cursor jsonb`, `last_sync_at`, `schedule_minutes` (null or ≥ 15),
`created_at`. RLS: `org_id = ORG`.
`last_error text` (0009, null): readable error of the last failed/partial run, no secrets; cleared by a clean run.
`needs_reconnect boolean NOT NULL DEFAULT false` (0009): the last run failed on a revoked/expired OAuth grant;
cleared by a reconnect or a run that works.

### sync_runs (0004)
`id`, `org_id`, `source_id` (CASCADE), `status` `running|succeeded|failed|partial`, `started_at`,
`finished_at`, `stats jsonb` (`discovered, added, updated, unchanged, deleted, failed`), `error`.
Partial UNIQUE `sync_runs_one_running (source_id) WHERE status = 'running'`. RLS: `org_id = ORG`.

### agent_runs (0006)
`id`, `org_id`, `user_id`, `goal`, `kb_ids uuid[]`, `status`
`planning|running|awaiting_approval|succeeded|failed|cancelled`, `max_steps` (1–25), `result jsonb`,
`error`, `created_at`, `finished_at`. RLS: `org_id = ORG AND user_id = USER`.

### agent_steps (0006)
`id`, `org_id`, `run_id`, `n` (≥ 1, UNIQUE per run), `kind` `plan|tool_call|tool_result|message`,
`tool`, `input jsonb`, `output jsonb`, `risk` `read|reversible_write|irreversible_write`,
`started_at`, `finished_at`, `error`, `created_at`. Partial UNIQUE
`(run_id, kind, input->>'tool_use_id') WHERE kind IN ('tool_call','tool_result')`.
RLS: `org_id = ORG AND EXISTS (SELECT 1 FROM agent_runs r WHERE r.id = run_id)` (follows the run's policy).

### approvals (0006)
`id`, `org_id`, `run_id`, `user_id` (run owner), `step_id`, `tool`, `input jsonb`, `status`
`pending|approved|rejected`, `decided_by`, `decided_at`, `idempotency_key` UNIQUE
(`<run>:<tool_use_id>`), `created_at`. RLS: `org_id = ORG` (org-wide so an admin can decide; the API
also checks owner-or-admin).

### reports (0006)
`id`, `org_id`, `run_id`, `name`, `mime`, `blob_key`, `size`, `created_at`. RLS: same as agent_steps.

### audit_log (0007)
`id`, `org_id`, `actor_user_id`, `actor_api_key_id`, `action`, `target_type`, `target_id`,
`request_id`, `ip_hash` (HMAC of `ip:<addr>`), `meta jsonb`, `created_at`. RLS: `org_id = ORG`.
`actor_email text` (0012, null): the actor's email copied at write time (a subquery in the INSERT), so
the trail stays readable after the member leaves; rows written before 0012 and keyless system rows are
null. It is the only place an email is stored in the log ([SECURITY](SECURITY.md)).
**Append-only for `rag_app`:** `UPDATE, DELETE, TRUNCATE` revoked; only `SELECT, INSERT` granted.

### sessions (0008)
`id` (= refresh token `jti`), `org_id`, `user_id`, `family_id`, `parent_id`, `replaced_by`,
`created_at`, `expires_at`, `revoked_at`, `ip_hash`, `user_agent_hash`.
RLS `user_isolation`: `user_id = USER` (per user, not per org). `rag_app`: `SELECT, INSERT, UPDATE`
only (no DELETE). Rows expired or revoked more than `SESSION_RETENTION_DAYS` (default 30) ago are
deleted by the scheduler tick through `prune_sessions()` (0012).

### org_invites (0010)
`id`, `org_id`, `email` (lower-case), `role` CHECK, `invited_by` → users (SET NULL), `created_at`,
`expires_at`, `accepted_at`, `declined_at`, `revoked_at`. Pending = all three NULL and
`expires_at > now()` (partial indexes on `org_id` and `email`). RLS `tenant_isolation`: `org_id = ORG`.
`rag_app`: `SELECT, INSERT, UPDATE` (no DELETE). The invitee reaches it only via the functions below.

## SECURITY DEFINER functions

Each is owned by `rag_definer` (the `row_security = off` ones) or `rag_owner` (`embedding_hnsw`),
pins `search_path`, has `EXECUTE` revoked from `PUBLIC` and granted to `rag_app` only ([ADR 0005](adr/0005-security-definer-functions.md)).

| Function | Migration | Why it exists | Scope limit |
|---|---|---|---|
| `embedding_hnsw(p_index uuid, p_create boolean) RETURNS void` (plpgsql) | 0002 | `rag_app` does not own `chunk_embeddings`, so it cannot CREATE/DROP INDEX | Only for an index row of the caller's `app.org_id`; dim comes from that row. `search_path = public, pg_temp` |
| `due_sync_orgs(stale_after interval) RETURNS SETOF uuid` (sql, STABLE) | 0005 | The scheduler must find which orgs need work; FORCE RLS shows one org at a time | Returns org ids only; `stale_after` is floored at 1 hour (0012), so a caller cannot list every org with a running sync. `row_security = off`, `search_path = pg_catalog, public` |
| `api_key_lookup(key_prefix text) RETURNS TABLE(id, org_id, user_id, hash, scopes, expires_at, revoked_at, last_used_at)` (sql, STABLE) | 0007 | An API key arrives before its org is known | One row by unique prefix; the caller compares the HMAC in constant time |
| `revoke_member_sessions(member uuid) RETURNS integer` (sql, VOLATILE) | 0008 | Removing a member must end their sessions, which per-user RLS hides from the admin | Only rows of the caller's `app.org_id` |
| `prune_sessions(keep interval) RETURNS integer` (sql, VOLATILE) | 0012 | Housekeeping: per-user RLS hides other users' sessions and `rag_app` has no DELETE | Deletes only rows expired or revoked more than `GREATEST(keep, 1 day)` ago; returns the count. `rag_definer` has `DELETE` on `sessions` for it |
| `auth_user_by_email(lookup text) RETURNS TABLE(id, pw_hash)` (sql, STABLE) | 0010 | Login runs before any user context; users RLS + column grants hide the row and the hash | One row by exact email; caller verifies argon2 |
| `my_invites() RETURNS TABLE(id, org_id, org_name, role, created_at, expires_at)` (sql, STABLE) | 0010 | The invitee is not in the inviting org | Pending invites whose email = the `app.user_id` user's email |
| `respond_invite(invite uuid, accept boolean) RETURNS TABLE(org_id, role)` (sql, VOLATILE) | 0010 | Accept/decline + create the membership in an org the caller is not in yet | Only a pending, unexpired invite to the caller's own email; no row otherwise |

## Migration notes

- Migrations run in one transaction each (`transaction_per_migration=True`, `alembic/env.py`).
- `0004` downgrade deletes all connector-owned documents.
- `0012` downgrade drops `actor_email` and `prune_sessions`, and restores the old kind CHECK as
  `NOT VALID` (kept `agent` rows stay; FORCE RLS hides them from a data fix run as `rag_owner`).
- The table owner is not a superuser (`0011`, ADR 0009). A new `row_security = off` definer function
  needs `ALTER FUNCTION ... OWNER TO rag_definer` plus grants for the tables it touches; for any role
  without BYPASSRLS, `row_security = off` makes an RLS-covered query fail loudly instead of
  silently returning nothing (that is why it is set). `alembic/env.py` sets
  `check_function_bodies = off`, because `rag_owner` cannot plan those bodies at CREATE.
