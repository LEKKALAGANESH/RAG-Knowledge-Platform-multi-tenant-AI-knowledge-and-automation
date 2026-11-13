# RAG Knowledge Platform

[![CI](https://github.com/LEKKALAGANESH/RAG-Knowledge-Platform-multi-tenant-AI-knowledge-and-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/LEKKALAGANESH/RAG-Knowledge-Platform-multi-tenant-AI-knowledge-and-automation/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Node 22](https://img.shields.io/badge/node-22-green)

A multi-tenant AI knowledge platform. Organizations connect files, websites, cloud drives, databases
and REST APIs, ask questions, and get streamed answers with verifiable citations. An approval-gated
agent runs multi-step analysis and produces Excel reports. Built for teams that need tenant
isolation, an audit trail and cost controls around LLM use.

> **Status:** development build, not yet production-proven. No live LLM run and no live OAuth
> provider run yet (both need credentials), no load tests. See [Status](#status).

## Contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Usage](#usage)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Status](#status)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Multi-tenant by design:** organizations, workspaces per org, roles (owner, admin, editor,
  viewer), Postgres row-level security on every tenant table, pending invites.
- **Ingestion:** PDF, DOCX, XLSX, CSV, TXT, MD, HTML, JSON/JSONL, images and scanned PDFs (OCR via
  Tesseract), ZIP/TAR archives, SRT/VTT subtitles, audio and video (local Whisper transcription).
  Every format has size, page, ratio and time limits; nothing is dropped silently.
- **Retrieval:** multilingual local embeddings, hybrid search (vector + keyword, relative score
  fusion), a similarity gate that abstains instead of guessing, optional reranker, pgvector by
  default or Qdrant.
- **Answers:** streamed over SSE with citations checked against the retrieved chunks, conversation
  memory, prompt-injection guard (retrieved text is data, never instructions).
- **Agent:** one orchestrator with typed tools (search, calculate, extract, Excel report), risk
  classes, human approval for writes, step limits, token budget.
- **Connectors:** website crawler (robots.txt, sitemap, SSRF guard), S3-compatible storage,
  PostgreSQL (read-only), Google Drive, OneDrive/SharePoint, Dropbox, generic REST with OpenAPI
  import. Incremental and scheduled syncs; deletions propagate.
- **Enterprise controls:** scoped API keys, audit log with CSV export, rate limits, per-org monthly
  budgets, usage reporting, session rotation with reuse detection, security headers, CSRF guard.
- **Developer access:** REST API (`/openapi.json`), Python SDK, TypeScript examples.

## Tech stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Dramatiq + Redis, uv
- **Data:** PostgreSQL 16 + pgvector (RLS, FTS), MinIO or any S3 store, optional Qdrant
- **AI:** fastembed (embeddings, reranker), faster-whisper, Tesseract, Anthropic or any
  OpenAI-compatible LLM (OpenAI, Ollama, vLLM, LM Studio, Azure)
- **Frontend:** Next.js 16 (App Router), React 19, TypeScript strict, Tailwind 4, Radix/shadcn, zod
- **Ops:** Docker, Docker Compose, Kubernetes manifests (kustomize), GitHub Actions CI

## Getting started

### Prerequisites

- Docker (Postgres, Redis, MinIO)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for the backend)
- Node.js 22 and npm
- Optional: Tesseract (OCR) and a local Whisper model (transcription)

### 1. Configure

Copy the template in [docs/.env.example.md](docs/.env.example.md) to `.env` at the repo root and
fill in the secrets. Every variable is described in
[docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md). The ones you must set:

| Variable | Purpose |
|---|---|
| `JWT_SECRET` | Signs access and refresh tokens (32+ chars) |
| `FERNET_KEYS` | Encrypts stored connector credentials |
| `API_KEY_PEPPER` | Keys API-key and IP hashes (keep separate from `JWT_SECRET`) |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `OWNER_DB_PASSWORD` | Bootstrap, app and owner DB roles |
| `REDIS_PASSWORD` | Redis auth (compose requires it) |
| `ANTHROPIC_API_KEY` or `LLM_PROVIDER=openai_compat` + `LLM_BASE_URL` | Model for answers and the agent |

For a throwaway local session without a `.env` file, print fresh dev values as shell exports:

```powershell
cd backend
uv run python scripts/dev_env.py --shell powershell | Invoke-Expression
```

```bash
cd backend
eval "$(uv run python scripts/dev_env.py --shell bash)"
```

### 2. Start infrastructure

```bash
docker compose up -d db redis minio
```

The first start creates the database roles: `rag_app` (API and workers), `rag_owner` (owns tables,
runs migrations), `rag_definer` (owns the few RLS-bypassing functions) and the `rag_test` database.
See [ADR 0009](docs/adr/0009-non-superuser-owner-roles.md).

### 3. Run the backend

```bash
cd backend
uv sync
uv run alembic upgrade head                 # as MIGRATION_DATABASE_URL (rag_owner)
uv run uvicorn app.main:app --reload        # API on http://localhost:8000
uv run dramatiq app.worker                  # worker (own terminal)
uv run python -m app.connectors.scheduler   # scheduled syncs (own terminal, optional)
```

### 4. Run the frontend

```bash
cd frontend
npm ci
npm run dev                                 # http://localhost:3000
```

### Run everything in containers

```bash
docker compose --profile app up -d --build  # migrate, api, worker, scheduler, web
```

Kubernetes manifests are in `deploy/k8s` (base + example overlay). See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Usage

### Upload and search with curl

```bash
TOKEN=$(curl -s localhost:8000/v1/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"correct horse battery"}' | jq -r .access_token)
KB=$(curl -s localhost:8000/v1/kbs -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name":"Policies"}' | jq -r .id)
curl -s localhost:8000/v1/kbs/$KB/documents -H "authorization: Bearer $TOKEN" -F file=@handbook.pdf
curl -s localhost:8000/v1/search -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"query":"refund window","k":5}'
```

### Ask a question (server-sent events)

```bash
curl -N localhost:8000/v1/answer -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"question":"How long do refunds take?"}'
```

### Python SDK

```bash
pip install -e sdk/python
```

```python
from rag_platform import Client

client = Client("http://localhost:8000", api_key="rag_...")
result = client.answer.ask("How long do refunds take?")
print(result["text"], result["citations"])
```

More examples, including agent runs with approvals and TypeScript: [docs/SDK.md](docs/SDK.md).
API reference: [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) and `GET /openapi.json`.

## Project structure

```text
backend/
  app/
    main.py, worker.py        FastAPI app; Dramatiq actor registry
    core/                     config, RLS sessions, security, rate limits, crypto, logging
    auth/ orgs/ audit/ usage/ accounts, sessions, API keys, members, invites, audit, budgets
    kb/ ingestion/            knowledge bases, documents, parsers (OCR, media, archives), chunking
    retrieval/ rag/ llm/      embeddings, pgvector/Qdrant, search, reranker, answers, LLM providers
    connectors/               website, s3, postgres, gdrive, onedrive, dropbox, rest, sync, scheduler
    agent/                    agent loop, tools, approvals, reports
  alembic/versions/           migrations 0001-0012
  evals/                      retrieval evaluation (seed set, corpus, metrics)
  scripts/dev_env.py          throwaway dev environment exports
  tests/                      pytest suite (real Postgres)
  docker/initdb/              first-start role setup
  Dockerfile
frontend/                     Next.js app, BFF routes, components, vitest tests, Dockerfile
sdk/python/                   Python SDK (rag_platform)
examples/typescript/          fetch + SSE examples
deploy/k8s/                   kustomize base + example overlay
docs/                         documentation set + ADRs
.github/workflows/ci.yml      backend, frontend, security and eval jobs
docker-compose.yml            Postgres+pgvector, Redis, MinIO; profiles app, qdrant
```

## Testing

```bash
docker compose up -d db redis minio
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy app
RAG_TEST_DB=rag_test_a uv run pytest -q
cd ../frontend && npm run lint && npm run typecheck && npm test
cd ../sdk/python && uv run pytest -q
```

- `RAG_TEST_DB` picks the test database (must match `rag_test*`); it is created and migrated if
  missing.
- Set `S3_ACCESS_KEY` and `S3_SECRET_KEY` (MinIO credentials) or the S3 tests are skipped.
- Tests that need a live LLM, Tesseract or a Whisper model are skipped when those are absent.
- Retrieval evaluation: `RAG_TEST_DB=rag_test_a uv run python -m evals.run_eval --set seed`.

CI runs all of this on every push and pull request. Details: [docs/TESTING.md](docs/TESTING.md).

## Status

Status words: **implemented+tested** · **implemented, not fully tested** · **partial** ·
**planned** · **blocked by credentials**.

| Area | Status | Note |
|---|---|---|
| Auth, sessions, orgs, roles, invites, RLS tenant isolation | implemented+tested | Role matrix test covers every route |
| API keys, audit log, rate limits, budgets, usage | implemented+tested | |
| Ingestion: documents, spreadsheets, archives, subtitles | implemented+tested | |
| OCR and audio/video transcription | implemented, not fully tested | Real OCR and real speech only run where Tesseract / a Whisper model is installed |
| Hybrid search, citation-checked answers, abstention | implemented+tested | Seed eval: hybrid Recall@5 0.974, MRR 0.942 |
| Answers and agent with a real LLM | blocked by credentials | Tested with a scripted model only |
| Reranker | implemented, not fully tested | Off by default: too slow on CPU |
| Qdrant vector store | implemented+tested | Optional; same eval scores as pgvector |
| Website, S3, PostgreSQL, REST connectors, scheduler | implemented+tested | Live-tested where possible |
| Google Drive, OneDrive, Dropbox | blocked by credentials | Contract-tested; flags off until live OAuth apps exist |
| Web app (chat, search, documents, integrations, agent, admin) | implemented+tested | |
| Python SDK, TypeScript examples | implemented+tested | |
| Docker images, compose app profile, K8s manifests | implemented, not fully tested | Compose smoke-tested; K8s schema-validated, not applied to a cluster |
| Load tests, SSO/SAML/SCIM, BYOC storage routing | planned | [Roadmap](docs/DEVELOPMENT_ROADMAP.md) |

## Documentation

| Doc | Covers |
|---|---|
| [Product requirements](docs/PRODUCT_REQUIREMENTS.md) | Users, stories, acceptance criteria |
| [System architecture](docs/SYSTEM_ARCHITECTURE.md) | Components and main flows |
| [Database schema](docs/DATABASE_SCHEMA.md) | Tables, RLS policies, definer functions |
| [API](docs/API_DOCUMENTATION.md) · [SDK](docs/SDK.md) | Routes, scopes, SSE events, client usage |
| [Connectors](docs/CONNECTOR_ARCHITECTURE.md) · [Ingestion](docs/INGESTION_PIPELINE.md) · [RAG](docs/RAG_ARCHITECTURE.md) · [Agent](docs/AGENT_ARCHITECTURE.md) | Subsystem design |
| [Security](docs/SECURITY.md) · [Threat model](docs/THREAT_MODEL.md) | Controls and known gaps |
| [Deployment](docs/DEPLOYMENT.md) · [Environment variables](docs/ENVIRONMENT_VARIABLES.md) | Running it |
| [Testing](docs/TESTING.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) | Working on it |
| [Roadmap](docs/DEVELOPMENT_ROADMAP.md) · [Changelog](docs/CHANGELOG.md) · [ADRs](docs/adr/README.md) | History and plans |

## Contributing

Open an issue first for larger changes. Keep one concern per pull request, add a test that fails
without your change, and make sure CI passes (ruff, mypy, pytest, frontend lint/typecheck/tests).

## License

No license has been chosen yet. Until a `LICENSE` file is added, all rights are reserved.
