# Testing

## Prerequisites

```bash
docker compose up -d db redis minio      # real Postgres + pgvector, Redis, MinIO (no SQLite, no mocks for the DB)
```

## Backend

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy app evals                      # strict
RAG_TEST_DB=rag_test_a uv run pytest -q --cov=app
```

PowerShell: `$env:RAG_TEST_DB="rag_test_a"; uv run pytest -q`.

- **Test database:** `RAG_TEST_DB` (default `rag_test`) must match `rag_test[a-z0-9_]*`; anything else
  (e.g. the dev DB `rag`) makes `conftest.py` refuse to start. If missing, the DB is created with the
  owner role and migrated to head. Use one DB per parallel runner (`rag_test_a`, `rag_test_b`) so
  concurrent runs do not collide.
- **Credentials:** the suite builds both DB URLs from `APP_DB_PASSWORD` and `POSTGRES_PASSWORD`
  (falling back to `rag_dev_app_pw` / `rag_dev_owner_pw`) and generates throwaway `JWT_SECRET` and
  `FERNET_KEYS`. Override with `TEST_DATABASE_URL` / `TEST_MIGRATION_DATABASE_URL`.
- **MinIO:** the S3 blob round-trip test is skipped unless `S3_ACCESS_KEY` / `S3_SECRET_KEY` (the
  MinIO root credentials from compose) are set in the shell. The S3 connector tests use the same
  MinIO.
- **Rate limits:** each run gets its own `RATE_LIMIT_NAMESPACE`, so tests never spend a dev server's
  auth bucket.
- **Live tests:** `pytest -m live` calls real providers; skipped without credentials
  (`test_live_anthropic_roundtrip` needs `ANTHROPIC_API_KEY`).

Counts change as work lands; rerun before trusting a number.

### What the suite covers

| Area | Files |
|---|---|
| Tenancy (AC1): every table has RLS on + forced; cross-org reads/writes blocked | `test_tenancy.py` |
| RBAC: every route × 4 roles; route table must match the policy | `test_rbac.py`, `test_auth_keys.py` |
| Auth, sessions, API keys, audit, rate limits, budgets, usage | `test_auth.py`, `test_sessions.py`, `test_auth_keys.py`, `test_audit.py`, `test_ratelimit.py`, `test_budget.py`, `test_usage.py` |
| Ingestion (AC2, AC3), real Dramatiq worker on a StubBroker, concurrency regressions | `test_ingestion*.py` |
| Retrieval, fusion, AC8 index versions, HNSW use | `test_retrieval*.py` |
| Answer flow (AC5, AC12), citation filter, abstention, partial saves | `test_rag.py`, `test_llm.py` |
| Agent (AC10, exactly-once, approvals, invoice demo) | `test_agent.py`, `test_agent_units.py` |
| Connectors: SSRF matrix, robots, sync engine, scheduler, S3, Postgres, OAuth contract tests | `test_connectors*.py` |
| Security: crypto rotation, secret scrubbing, 500 hygiene, worker imports | `test_security.py` |
| Eval harness | `test_evals*.py` |

LLM calls in tests use a scripted provider (a test double we own); it is never reachable from the app.

### Manual runs with the scripted model (no LLM key)

These start the real app against the **test** DB:

```bash
RAG_TEST_DB=rag_test_a uv run python -m tests.test_rag serve       # chat on :8079, scripted answer stream
RAG_TEST_DB=rag_test_a uv run python -m tests.test_agent serve --port 8079   # agent: invoice demo + approval
```

Stop them by killing the `python.exe` PID printed as `Started server process [PID]`.

## Retrieval eval

```bash
RAG_TEST_DB=rag_test_a uv run python -m evals.run_eval --set seed --mode all
```

Current: hybrid R@5 0.974, MRR 0.942. See [RAG_ARCHITECTURE.md](RAG_ARCHITECTURE.md#evaluation-backendevals).

## Frontend

```bash
cd frontend
npm run lint && npm run typecheck && npm test && npm run build
```

Vitest + Testing Library + jsdom; last recorded run 204 tests (task B6e). Includes BFF proxy path
guard, CSRF, SSE parsing, OAuth return handling and rate-limit countdown tests.

There is **no Playwright end-to-end suite** in the repo yet (no config, no dependency). Browser checks
so far were done by hand with a Playwright browser against live servers.

## CI (`.github/workflows/ci.yml`)

Runs on every push and pull request (and by hand via *Run workflow*):

| Job | What it runs |
|---|---|
| `backend` | `docker compose -p ragci up -d --wait db redis minio` (the repo's compose file, so `initdb` creates `rag_app`/`rag_owner`/`rag_definer`), then `uv sync --locked`, `ruff check .`, `mypy app`, `pytest -q` on the default `rag_test` DB. Tesseract is installed so the OCR tests run. |
| `frontend` | Node 22, `npm ci`, `lint`, `typecheck`, `test`, `build` |
| `security` | `pip-audit` on `uv export` of the lockfile, `npm audit --audit-level=high`, gitleaks over the full git history |
| `eval` | `pytest tests/test_evals.py` (AC4 thresholds) on its own; only on `main` and manual runs (it is also inside `backend`) |

Every DB/Redis/MinIO password is generated per run with `openssl rand` and masked; the repo and
GitHub secrets hold no credentials. `ruff format --check .` is not in CI yet: 4 files are unformatted
(see the TODO in the workflow).

## Rules

- A new regression test must fail on the old code.
- Never weaken or skip a test to go green.
- Rate-limit tests freeze the limiter clock (`frozen_window` fixture in `test_ratelimit.py`): fixed
  windows reset on the minute, so a test whose requests straddled the boundary used to flake.
- Coverage floor: 80% business logic, 60% integration paths.
