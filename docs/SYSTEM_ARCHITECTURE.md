# System architecture

A modular monolith ([ADR 0001](adr/0001-modular-monolith.md)): one FastAPI process, one Dramatiq worker
process (many threads), one scheduler loop, all sharing PostgreSQL, Redis and a blob store.

## Components

```mermaid
flowchart LR
  B[Browser] --> FE["Next.js app<br/>pages + BFF routes<br/>/api/session, /api/v1/*"]
  C[API client<br/>API key] --> API
  FE -->|Bearer access token| API["FastAPI API<br/>app.main"]
  API -->|RLS session as rag_app| PG[("PostgreSQL 16<br/>+ pgvector + FTS")]
  API -->|rate limits, OAuth state| R[(Redis)]
  API -->|enqueue| R
  API --> BS[("Blob store<br/>local dir or S3/MinIO")]
  W["Dramatiq worker<br/>app.worker<br/>queues: ingest, index, sync, agent"] -->|consume| R
  W -->|RLS session per org| PG
  W --> BS
  S["Scheduler<br/>app.connectors.scheduler"] -->|due_sync_orgs()| PG
  S -->|enqueue sync| R
  API -->|stream| LLM["LLM provider<br/>Anthropic / OpenAI-compatible"]
  W --> LLM
  W --> EMB["Embedder<br/>fastembed (local) / OpenAI-compatible"]
  API --> EMB
  W -->|SSRF-guarded HTTP| EXT["External sources<br/>websites, S3, Postgres, Google Drive"]
```

| Component | Code | Role |
|---|---|---|
| API | `backend/app/main.py`, `*/router.py` | All `/v1` routes, auth, RLS sessions, SSE streams |
| Worker | `backend/app/worker.py` | Registers every actor: `ingestion.tasks` (queue `ingest`), `retrieval.tasks` (`index`), `connectors.sync` (`sync`), `agent.tasks` (`agent`) |
| Scheduler | `backend/app/connectors/scheduler.py` | Every 60 s: starts due syncs per org; reaps stale runs |
| Frontend | `frontend/app`, `frontend/lib` | UI and a BFF: tokens live in httpOnly cookies, never in browser JS |
| Postgres | `alembic/versions/0001-0008` | All state, vectors (pgvector), keyword index (FTS) |
| Redis | `docker-compose.yml` | Dramatiq broker, rate-limit counters, OAuth `state` (10 min) |
| Blob store | `app/ingestion/storage.py` | Uploaded files and agent reports (`LocalBlobStore` or `S3BlobStore`) |

## Tenancy model

Every request resolves a `Tenant` (`app/core/tenancy.py`) from a JWT or an API key. The session runs
`set_config('app.org_id', ..., true)` and `app.user_id` at the start of every transaction
(`app/core/db.py`). The API and worker connect as `rag_app` (no BYPASSRLS), so Postgres row-level
security filters every query. Details: [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md),
[ADR 0002](adr/0002-postgres-pgvector-rls.md).

## Flows

### Request

```mermaid
sequenceDiagram
  participant FE as Browser / BFF
  participant API
  participant PG as Postgres
  FE->>API: request + Bearer (JWT or rag_ API key) [+ X-Request-ID]
  API->>API: RequestIdMiddleware (id, timing log)
  API->>PG: api_key_lookup(prefix) if API key (SECURITY DEFINER)
  API->>PG: BEGIN; set_config(app.org_id, app.user_id)
  API->>PG: SELECT role FROM memberships (403 if removed)
  API->>API: need(scope) · require_role(...) · rate limit · budget
  API->>PG: route queries (RLS applies)
  API-->>FE: JSON, CSV, file, or SSE; errors as {error:{code,message,request_id}}
```

### Ingest

```mermaid
sequenceDiagram
  participant API
  participant BS as Blob store
  participant Q as Redis
  participant W as Worker (ingest)
  participant PG as Postgres
  API->>API: size limit, MIME from content, sha256
  API->>PG: same sha256 upload in KB? -> 200 existing doc
  API->>BS: put org/kb/sha256
  API->>PG: documents(status=queued)
  API->>Q: ingest_document(doc, org)
  W->>PG: status=processing
  W->>BS: get blob
  W->>W: parse -> sections -> chunks (page/section/sheet aware)
  W->>PG: chunks rows
  W->>W: embed (KB's active index model; budget check; usage row)
  W->>PG: chunk_embeddings; status=ready (or failed with reason)
```

Details: [INGESTION_PIPELINE.md](INGESTION_PIPELINE.md).

### Answer

```mermaid
sequenceDiagram
  participant FE as Client
  participant API
  participant PG as Postgres
  participant L as LLM
  FE->>API: POST /v1/answer
  API->>API: scope, rate limit, budget (402), LLM configured (503)
  API->>PG: conversation + user message (commit)
  API-->>FE: SSE meta
  API->>L: rewrite follow-up (if history)
  API->>PG: hybrid search (vector per index + FTS, relative score fusion)
  API->>API: min-cosine gate; pack context
  alt no hits or below gate
    API-->>FE: token "I couldn't find this in your sources."
  else
    API->>L: stream with [cN] markers
    API-->>FE: citation + token events (unknown markers stripped)
  end
  API->>PG: assistant message + usage rows
  API-->>FE: done (or error)
```

Details: [RAG_ARCHITECTURE.md](RAG_ARCHITECTURE.md).

### Agent

```mermaid
sequenceDiagram
  participant FE as Client
  participant API
  participant W as Worker (agent)
  participant PG as Postgres
  participant L as LLM
  FE->>API: POST /v1/agent/runs (202)
  API->>W: run_agent(run)
  loop until done, failed, or approval needed
    W->>PG: rebuild transcript from agent_steps
    W->>L: model turn (tools fixed in code)
    W->>PG: claim tool_call (started_at IS NULL) then run it
    alt write tool that needs approval
      W->>PG: approvals(pending); run=awaiting_approval
    end
  end
  FE->>API: GET .../events?after=n (SSE: step, step_update, approval, status, end)
  FE->>API: POST /v1/approvals/{id} -> run_agent again
```

Details: [AGENT_ARCHITECTURE.md](AGENT_ARCHITECTURE.md).

### Sync

```mermaid
sequenceDiagram
  participant S as Scheduler / API
  participant PG as Postgres
  participant W as Worker (sync)
  participant X as Source
  S->>PG: due_sync_orgs() then per org: claim idle due sources, insert sync_runs(running)
  S->>W: sync_source(run, org)
  W->>X: discover items (SSRF-checked, pinned IP)
  W->>W: skip unchanged (content hash), fetch changed
  W->>PG: documents per (source_id, external_key) -> ingest pipeline
  W->>PG: delete missing docs only on a full, failure-free run
  W->>PG: run succeeded | partial | failed; source idle | error
```

Details: [CONNECTOR_ARCHITECTURE.md](CONNECTOR_ARCHITECTURE.md).

## Cross-cutting

- **Errors:** one envelope `{"error":{"code","message","request_id"}}` (`app/core/errors.py`); 500s
  never expose internals.
- **Logging:** JSON lines with `request_id`; secrets scrubbed by value and by key pattern
  (`app/core/logging.py`). Request logs carry the path only, never the query string.
- **Observability:** `/healthz` (process up), `/readyz` (DB reachable). No metrics or tracing yet
  (planned).
