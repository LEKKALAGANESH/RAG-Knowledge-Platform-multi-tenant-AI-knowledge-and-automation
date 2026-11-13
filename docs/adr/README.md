# Architecture decision records

Format: Context → Decision → Consequences → Alternatives considered. Status is Accepted unless noted.

| ADR | Decision |
|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith API + Dramatiq workers |
| [0002](0002-postgres-pgvector-rls.md) | PostgreSQL + pgvector + row-level security for tenancy |
| [0003](0003-relative-score-fusion.md) | Relative score fusion instead of RRF for hybrid search |
| [0004](0004-per-thread-worker-engine.md) | One database engine per worker thread |
| [0005](0005-security-definer-functions.md) | Narrow SECURITY DEFINER functions for cross-org needs |
| [0006](0006-hmac-api-keys.md) | API keys stored as peppered HMAC, not argon2 |
| [0007](0007-private-endpoint-gate.md) | Private connector endpoints need a server gate |
| [0008](0008-refresh-session-grace-window.md) | Refresh sessions with a reuse grace window |
| [0009](0009-non-superuser-owner-roles.md) | Non-superuser table owner and a BYPASSRLS-only definer role |
| [0010](0010-qdrant-vector-store.md) | Optional Qdrant vector store behind the same store contract |
