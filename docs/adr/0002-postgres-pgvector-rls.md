# ADR 0002: PostgreSQL + pgvector + row-level security for tenancy

Status: Accepted (waves 1-2)

## Context

Tenant isolation is the top requirement (AC1). The platform stores relational data, vectors and a
keyword index, and should stay Supabase-compatible. App-only filtering (`WHERE org_id = ?` in every
query) fails open: one forgotten filter leaks data.

## Decision

- One PostgreSQL 16 database with `pgvector`. Keyword search uses a generated `tsvector` column with
  the `simple` config (language-neutral).
- Every tenant table has `org_id NOT NULL`, RLS `ENABLE` + `FORCE`, and a policy on
  `current_setting('app.org_id')` (per user where privacy needs it: conversations, agent runs,
  sessions). The session sets `app.org_id` / `app.user_id` with `set_config(..., true)` at the start
  of every transaction.
- The app connects as `rag_app` (no superuser, no BYPASSRLS); migrations use the owner role.
- Composite `(org_id, id)` foreign keys, because FK checks ignore RLS.
- Vectors: one untyped `vector` column; each embedding index has its own dimension and a partial HNSW
  index on `embedding::vector(dim) WHERE index_id = ...`, created at runtime.

## Consequences

- A missed filter in app code returns nothing instead of another tenant's rows;
  `tests/test_tenancy.py` enumerates every table.
- Cross-org needs (API-key lookup, scheduler, index DDL, session revoke) require SECURITY DEFINER
  functions ([ADR 0005](0005-security-definer-functions.md)).
- One partial HNSW per KB: planning time and insert predicate checks grow with KB count, and
  CREATE/DROP INDEX is not concurrent. Upgrade path: LIST-partition `chunk_embeddings` by index.
- p95 search at 1M chunks per org is unmeasured.
- The `users` table has no RLS (users span orgs); see the threat model.

## Alternatives considered

- **App-level filtering only:** rejected (fails open).
- **Schema or database per tenant:** strong isolation but heavy migrations and connection counts.
- **Separate vector DB (Qdrant, Pinecone):** later, behind an adapter; one DB is simpler and
  transactional for the MVP.
- **Elasticsearch for keyword search:** another system to run; Postgres FTS was enough on the eval.
