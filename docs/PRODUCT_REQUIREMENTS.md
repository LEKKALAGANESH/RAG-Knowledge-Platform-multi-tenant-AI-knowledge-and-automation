# Product requirements

Users, stories and acceptance criteria, with what the code does today.

## Users

- **Individual:** a personal knowledge assistant over their own files and sites.
- **Org member or admin:** shared knowledge bases (KBs), roles, connectors, audit.
- **Developer:** REST API + scoped API keys to embed search and answers in their product.
- **Platform operator:** runs it as multi-tenant SaaS or inside a customer's cloud.

## Problem

Business knowledge is spread across files, drives, sites and databases. Single-folder chatbots
hallucinate, leak across tenants, and cannot act. Users need cited answers across all sources, plus
authorized, approval-gated actions.

## User stories and status

| # | Story | Status |
|---|---|---|
| 1 | Register, get a personal org, invite members with roles | implemented+tested (members are added by email of an existing account; no email invites) |
| 2 | Create a KB and upload PDF, DOCX, TXT/MD, CSV/XLSX, HTML (also JSON, JSONL) | implemented+tested |
| 3 | See per-document status, warnings and failures | implemented+tested |
| 4 | Streamed answer with clickable citations | implemented, not fully tested (scripted model only; live LLM blocked by credentials) |
| 5 | Unanswerable question → explicit "not found" | implemented, not fully tested (similarity gate + prompt rule; live LLM blocked) |
| 6 | Connect a website (sitemap/crawl, robots.txt) | implemented+tested |
| 7 | Connect S3 / Postgres (read-only) / Google Drive; manual + scheduled sync; deletes propagate | S3, Postgres: implemented+tested. Google Drive: implemented, not fully tested (live blocked by credentials) |
| 8 | Agent task (e.g. invoice totals) → plan, tool log, cited XLSX | implemented, not fully tested (scripted model only) |
| 9 | Consequential agent actions pause for approval | implemented+tested |
| 10 | Admin sees audit log, token and cost usage; budgets enforced | implemented+tested |
| 11 | Developer creates a scoped API key and calls `/v1/search`, `/v1/answer` | implemented+tested (no Python SDK yet: planned) |
| 12 | Org chooses LLM and embedding provider | partial: chosen per server by env vars, not per org |

## Acceptance criteria

| AC | Criterion (short) | Status | Evidence |
|---|---|---|---|
| AC1 | Org B gets 404 for org A resources; every tenant table has RLS | implemented+tested | `tests/test_tenancy.py` enumerates tables and asserts RLS enabled + forced and no cross-org reads; per-route 404 tests in each module |
| AC2 | Each format yields ≥1 chunk with page/sheet/section; corrupt file ends `failed` with a reason | implemented+tested | `tests/test_ingestion.py` (`test_ac2_*`) |
| AC3 | Same file in same KB is not re-embedded | implemented+tested | upload returns 200 with the existing document |
| AC4 | Seed eval Recall@5 ≥ 0.80, MRR ≥ 0.6, hybrid beats vector-only | implemented+tested | hybrid R@5 0.974, MRR 0.942 vs vector 0.953 / 0.938 (`evals/run_eval.py`, 47 items) |
| AC5 | Every citation maps to retrieved context; unknown markers stripped; abstention ≥ 90% | partial | Citation mapping tested (`tests/test_rag.py`). Answer-level abstention rate needs a live LLM: **blocked by credentials** |
| AC6 | First token < 3 s p50 | blocked by credentials | Server overhead to first token measured at p50 42 ms with a scripted model; real model latency unmeasured |
| AC7 | Deleted document/source file becomes unretrievable within one sync | implemented+tested | `tests/test_connectors.py` (deletion only on full, failure-free runs) |
| AC8 | Embedding model change creates a new index version; vectors never mixed | implemented+tested | `tests/test_retrieval.py` AC8 test |
| AC9 | Website connector refuses private/loopback/link-local IPs and honors robots.txt | implemented+tested | `tests/test_connectors.py` SSRF matrix |
| AC10 | No write-class tool without a recorded approval; injected text cannot trigger tools | implemented+tested | `tests/test_agent.py` (scripted "rogue" model) |
| AC11 | Credentials only Fernet-encrypted; no secrets in logs | implemented+tested | `tests/test_security.py`, `test_connectors*.py`, `test_auth_keys.py` |
| AC12 | Every LLM/embedding call writes a usage row | implemented+tested | `tests/test_rag.py`, `test_retrieval.py`, `test_agent.py` |

## Non-functional requirements

| Requirement | Status |
|---|---|
| Multilingual (Unicode, multilingual embeddings, `simple` FTS config) | implemented+tested (cross-language eval items) |
| p95 search < 500 ms at 1M chunks/org | planned: no load test yet. Known cliff: one partial HNSW per KB ([ADR 0002](adr/0002-postgres-pgvector-rls.md)) |
| Every async boundary has an error state; readable user errors | implemented (error envelope, SSE `error` event, document/run `error` fields) |

## Out of scope for now

OCR, audio/video, archives, the remaining connectors, Meta APIs, SSO/SAML/SCIM, CMEK, BYOC storage
routing, other vector stores, billing, Kubernetes. See [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md).
