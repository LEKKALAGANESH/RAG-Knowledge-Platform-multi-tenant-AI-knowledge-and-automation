# ADR 0005: Narrow SECURITY DEFINER functions for cross-org needs

Status: Accepted (migrations 0002, 0005, 0007, 0008)

## Context

With FORCE RLS and a non-bypassing app role, some legitimate operations cannot run: building an HNSW
index on a table the app role does not own, finding which orgs need a scheduled sync, looking up an
API key before its org is known, and revoking a removed member's sessions (hidden by per-user RLS).

## Decision

Owner-run SECURITY DEFINER functions, one per need, each answering only that question:
`embedding_hnsw(index, create)`, `due_sync_orgs(stale_after)`, `api_key_lookup(prefix)`,
`revoke_member_sessions(member)`. Rules for every one: fixed `search_path`, `REVOKE ALL FROM PUBLIC`,
`GRANT EXECUTE TO rag_app` only, smallest possible output (org ids only; one key row by unique
prefix), and scoped to the caller's `app.org_id` where it writes or does DDL. Functions that must read
across orgs set `row_security = off`, so a non-bypassing owner fails loudly instead of silently
returning nothing.

## Consequences

- RLS stays forced for all normal queries; the cross-org surface is four functions with tests (the
  ACL holds only `rag_app` and the owner).
- The owner is a superuser in compose. Planned: a dedicated non-superuser owner role.
- `due_sync_orgs` takes a caller-chosen interval; a caller could learn which orgs have running syncs
  (org ids only). Planned: clamp it server-side.

## Alternatives considered

- **Scheduler or key lookup connecting as the owner role:** bypasses RLS for whole code paths.
- **Iterating users and impersonating each to read memberships:** a hack, and slow.
- **Granting BYPASSRLS to the app role:** defeats the tenancy model.
