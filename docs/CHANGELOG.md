# Changelog

By build wave. No releases have been tagged.

## Wave 7: OAuth connectors and hardening

- **A7a** Google Drive connector: OAuth code + PKCE, encrypted tokens, refresh/revoke, incremental
  changes, Docs/Sheets/Slides export. Behind `CONNECTOR_GDRIVE_ENABLED`. Contract-tested only; live
  blocked by credentials.
- **B6d** OAuth connect UI (start → provider → callback notices) and a BFF CSRF fix (`Origin` /
  `Sec-Fetch-Site` checks, JSON-only session POSTs).
- **A5.1** Agent SSE adds `approval` and `step_update` events and replays them on reconnect; agent
  citations carry `document_id`, `kb_id`, `excerpt`; test-only `tests.test_agent serve` helper.
- **B6e** Agent UI uses the new events (no re-reads) and links citations to the KB.
- **H1** `RATE_LIMIT_NAMESPACE` setting (tests get their own); budget blocks fail ingestion once
  without retries; CSV formula-cell neutralization; member list readable by every role.
- In progress: **A7b** OneDrive/SharePoint and Dropbox (flags off), **B7a** this documentation.

## Wave 6: Enterprise

- **A6a** Scoped API keys (HMAC-peppered, prefix lookup via SECURITY DEFINER), append-only audit log,
  Redis rate limits (auth fails closed). Migration 0007.
- **A6b** Refresh sessions with rotation, 30 s reuse grace and theft detection; logout and
  logout-everywhere; member role change and removal; per-org monthly budgets (402 before any model
  call); usage API and CSV. Migration 0008.
- **B6a** Integrations UI: connector cards, schema-driven forms, write-only secrets, sync history.
- **B6b** Agent UI (runs, live steps, approvals, reports) and global 429 countdown / 503 handling.
- **B6c** Admin UI: team, API keys (shown once), audit log, usage dashboard, budget. BFF proxy path
  guard (security fix).

## Wave 5: Agent and data connectors

- **T1** Per-runner test database (`RAG_TEST_DB`, must match `rag_test*`).
- **B4.2** Scheduler (`python -m app.connectors.scheduler`) with SECURITY DEFINER `due_sync_orgs()`.
  Migration 0005.
- **A5** Agent: one orchestrator, fixed tool registry with risk classes, approvals, exactly-once tool
  calls, Decimal calculator, invoice extraction, cited XLSX reports. Migration 0006.
- **B5** S3-compatible and PostgreSQL (read-only) connectors.
- **B5.1** Private-endpoint server gate, `test()` before saving a source, structured test result with a
  Postgres schema browser, `?full=true` sync.

## Wave 4: Quality and first connector

- **A4** Retrieval eval framework and 47-item seed set (Recall@K, MRR, nDCG, leak, abstention sweep).
- **B4** Connector framework, sync engine, Website connector (SSRF guard, robots.txt, sitemaps).
  Migration 0004.
- **B4.1** Upload dedupe ignores crawled/deleted copies; stale-run reaper; per-org scheduler tick.

## Wave 3: RAG and chat

- **A3** LLM providers (Anthropic, OpenAI-compatible), SSE `/v1/answer`, citation validation,
  abstention, follow-up rewrite, usage rows. Migration 0003.
- **A3.1** Embedding usage rows, partial answers saved on disconnect, provider keys scrubbed from logs,
  conversation cursor, worker import cycle fixed.
- **A3.2** Keyword search ORs content words; relative score fusion replaces RRF (hybrid R@5 0.974 /
  MRR 0.942); min-cosine gate 0.32 for MiniLM.
- **A3.3** Citations carry `kb_id`; stored `abstained` flag; 401 mid-answer redirects to login;
  embedder reports provider token counts.
- **B3b** Chat (streaming, citation chips and panel, history) and search pages.

## Wave 2: Document intelligence

- **A2** Ingestion: content-based type detection, 9 formats, location-aware chunking, sha256 dedupe,
  local and S3 blob stores, Dramatiq task with readable failures.
- **A2.1** Retry status visible to users; `GET /v1/documents/{id}/chunks`.
- **A2.2 / A2.3** Worker hang fixed (per-thread engine); two DDL deadlocks fixed; KB delete drops its
  HNSW indexes.
- **B2** Embeddings (fastembed, OpenAI-compatible), per-KB index versions, per-index partial HNSW,
  hybrid search, reindex. Migration 0002.
- **B3a / B3a.1** Documents UI (upload, status polling, warnings, reprocess, delete) and chunk preview.

## Wave 1: Foundation

- **A1** FastAPI app, auth (argon2, JWT), orgs and roles, KB CRUD, RLS on every tenant table,
  `rag_app` role, Docker Compose. Migration 0001.
- **B1 / B1.1** Next.js shell, login/register via a BFF with httpOnly cookies, KB pages, typed API
  client aligned to the real API; open-redirect fixes.
