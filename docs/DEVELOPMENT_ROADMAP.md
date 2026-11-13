# Development roadmap

Planned and in-progress work. Nothing here is built unless it says so.

## In progress

| Task | Scope | Status |
|---|---|---|
| A7b OneDrive / Dropbox | Connectors reusing `oauth.py`; Drive incremental runs pick up subfolders created between full runs | partial: code and tests being written |
| B7a Docs set | This documentation | in progress |

## Next

| Task | Scope |
|---|---|
| A7c OAuth source lifecycle | In-place reconnect `POST /v1/sources/{id}/oauth/reconnect`; first sync starts after the callback; source `error` text + `needs_reconnect` flag (migration 0009) |
| B7b Whole-repo security audit | Adversarial audit; findings fixed or filed |
| T2 Serve-helper login 500 | First login after `tests.test_agent serve` starts fails: the Redis asyncio pool is bound to the seeding event loop. Check whether the production rate-limit client has the same per-loop issue |

## Blocked by credentials

| Item | Needs |
|---|---|
| Live LLM answers, AC6 latency, answer-level eval, live agent run, `pytest -m live` | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or a local Ollama |
| Google Drive live verification | Google Cloud OAuth client (Drive API enabled, consent screen, test users) |
| OneDrive / Dropbox live verification | Entra app registration; Dropbox app |
| Meta (Facebook/Instagram/WhatsApp) connectors | Meta app review |

## Backlog (planned)

Retrieval and RAG:
- Cross-encoder reranker over the top ~20 (table and ambiguous misses)
- Multi-part query decomposition
- Grow the eval seed set past 47 items; answer-level numbers once an LLM key exists

Ingestion:
- OCR (tesseract), PPTX, audio/video (faster-whisper), archives with a bomb guard

Connectors:
- MongoDB, Supabase, MySQL; generic REST/OpenAPI; Notion, Confluence, Slack, GitHub; Meta APIs
- Qdrant (and other vector store) adapters

Security and data:
- Dedicated non-superuser table-owner role for migrations and SECURITY DEFINER functions
- Clamp `due_sync_orgs(stale_after)` to a server-side minimum
- Audit rows carry an actor email snapshot
- Prune old `sessions` rows (owner-role job)
- Usage kind `agent` (needs a migration)
- SSO/SAML/SCIM, CMEK, BYOC storage routing

Platform:
- CI pipeline (lint, types, tests, secret scan, dependency audit)
- Dev start script that exports throwaway env without writing `.env`
- Python SDK and TypeScript examples
- Load tests (p95 search < 500 ms at 1M chunks/org); LIST-partition `chunk_embeddings` by index if
  per-KB HNSW planning cost grows
- Kubernetes manifests
- Playwright end-to-end suite

Done from the old backlog (task H1): members list readable by all roles, budget blocks no longer retried,
CSV formula neutralization.
