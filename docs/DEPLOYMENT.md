# Deployment

Only a local Docker Compose setup exists. There are no production manifests (Kubernetes, Terraform)
and no CI pipeline yet; treat this page as the runbook for running the processes, not as a
production-readiness claim.

## Processes

| Process | Command (from `backend/` unless noted) | Scale | Notes |
|---|---|---|---|
| Postgres 16 + pgvector | `docker compose up -d db` | 1 | Image `pgvector/pgvector:pg16` |
| Qdrant (optional) | `docker compose --profile qdrant up -d qdrant` | 1 | Only with `VECTOR_STORE=qdrant` (or point `QDRANT_URL` at a customer-owned Qdrant). Image `qdrant/qdrant:v1.20.0`, loopback-only, volume `qdrantdata`. Switching stores = reindex every KB |
| Redis 7 | `docker compose up -d redis` | 1 | Broker, rate limits, OAuth state |
| MinIO (optional) | `docker compose up -d minio` | 1 | Only if `BLOB_BACKEND=s3` against MinIO, or for S3 tests. Image `bitnamilegacy/minio:2025.7.23` |
| Migrations | `uv run alembic upgrade head` | once per deploy | Uses `MIGRATION_DATABASE_URL` (owner role) |
| API | `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` | N | Stateless; connects as `rag_app` |
| Worker | `uv run dramatiq app.worker` | N | Defaults: one process per CPU, 8 threads each. Queues `ingest`, `index`, `sync`, `agent`. Limit with `--queues`, `--processes`, `--threads` |
| Scheduler | `uv run python -m app.connectors.scheduler --interval 60` | 1+ | Safe to run several (conditional claims). Each tick also prunes dead login sessions (`SESSION_RETENTION_DAYS`) |
| Frontend | `cd frontend && npm ci && npm run build && npm start` | N | Set `NEXT_PUBLIC_API_URL` to the API base |

**OCR (optional):** install the `tesseract` binary on every worker host (Debian/Ubuntu or a
Debian-based image: `apt-get install -y tesseract-ocr` + `tesseract-ocr-<lang>` per `OCR_LANGS`;
Windows: `winget install UB-Mannheim.TesseractOCR` + `TESSERACT_CMD`). Without it, image uploads fail
with a readable reason and scanned PDF pages are skipped with a warning; the worker logs which at
startup. See [INGESTION_PIPELINE.md](INGESTION_PIPELINE.md#ocr).

**Transcription (optional):** faster-whisper and PyAV are Python wheels (FFmpeg libraries are bundled; no system ffmpeg). Put the Whisper model on every worker host — pre-fetch it into `TRANSCRIBE_MODEL_DIR` (or mount it and point `TRANSCRIBE_MODEL` at the directory), or set `TRANSCRIBE_ALLOW_DOWNLOAD=true`. Without a model, audio/video uploads fail with a readable reason; SRT/VTT still work. See [INGESTION_PIPELINE.md](INGESTION_PIPELINE.md#audio-video-and-subtitles).

The first `docker compose up` runs `backend/docker/initdb/*.sh` once per new volume. `01-app-role.sh`
creates `rag_app` (`APP_DB_PASSWORD`) and the `rag_test` database. `02-owner-role.sh` creates
`rag_owner` (`OWNER_DB_PASSWORD`) and `rag_definer`, hands both databases to `rag_owner`, and creates
the `vector` extension (ADR 0009). The scripts must keep LF line endings (a CRLF checkout breaks them
on Linux containers).

**Volume created before FX-6 (S14)**, once per volume:

```sh
docker compose exec -e OWNER_DB_PASSWORD=... db sh /docker-entrypoint-initdb.d/02-owner-role.sh
# migrate to 0011 AS THE SUPERUSER: it moves table/function ownership off `rag`
MIGRATION_DATABASE_URL=postgresql+asyncpg://rag:<POSTGRES_PASSWORD>@localhost:5432/rag uv run alembic upgrade head
```

Then set `MIGRATION_DATABASE_URL` to `rag_owner`. The test suite does the same for old `rag_test*`
databases on its next run. (Git Bash: prefix `docker compose exec` with `MSYS_NO_PATHCONV=1`.)

## Network exposure (compose)

Postgres, Redis, MinIO and the MinIO console publish on loopback only (`127.0.0.1` and `[::1]`;
binding only IPv4 makes `localhost` clients on Windows wait ~2 s on `::1` first). Redis requires
`REDIS_PASSWORD` (`--requirepass`), so `REDIS_URL` must carry it:
`redis://:<REDIS_PASSWORD>@localhost:6379/0`. To apply this to an existing stack:
`docker compose up -d --force-recreate db redis minio` (volumes are kept).

## Database roles

| Env var | Role | Rights |
|---|---|---|
| `MIGRATION_DATABASE_URL` | `rag_owner` | DDL; owns tables, indexes, `embedding_hnsw`. NOSUPERUSER NOBYPASSRLS. Only Alembic uses it |
| – (NOLOGIN) | `rag_definer` | Owns the cross-tenant SECURITY DEFINER functions (`row_security = off`); BYPASSRLS, grants limited to what those bodies touch |
| `DATABASE_URL` | `rag_app` | DML only, no BYPASSRLS; RLS applies to every query |
| – (bootstrap) | `rag` | Superuser from `POSTGRES_USER`: initdb (roles, extension) and one-off maintenance only |

Never point `DATABASE_URL` at the owner role: it owns the tables, so it could turn RLS off.
Both URLs use the async driver: `postgresql+asyncpg://user:pass@host:5432/rag`.

## Secrets

| Secret | Notes |
|---|---|
| `JWT_SECRET` | ≥ 32 chars. Rotating it signs everyone out (and kills API keys if `API_KEY_PEPPER` is unset) |
| `FERNET_KEYS` | Comma-separated. The first encrypts; all decrypt. Rotate by prepending a new key, re-saving secrets, then dropping the old key |
| `API_KEY_PEPPER` | Set it in production so `JWT_SECRET` rotation does not invalidate API keys. Changing it invalidates every API key, and new audit IP hashes no longer match old ones |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `OWNER_DB_PASSWORD` | Compose: superuser, `rag_app`, `rag_owner`. The last two are embedded in the two DB URLs |
| `REDIS_PASSWORD` | Compose `requirepass`; embedded in `REDIS_URL` |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | MinIO root credentials in compose and blob-store credentials |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `EMBEDDING_API_KEY` | Provider keys |
| `GOOGLE_OAUTH_CLIENT_SECRET`, `MICROSOFT_OAUTH_CLIENT_SECRET`, `DROPBOX_OAUTH_CLIENT_SECRET` | OAuth app secrets |

All of these are scrubbed from logs by value. Keep them in the environment or a secret manager;
`.env` is git-ignored. Full list: [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).

## Reverse proxy

- Client IP chain: browser -> reverse proxy (nginx/LB) -> Next BFF -> API. The proxy must append
  (or set) `X-Forwarded-For`; Next must not be reachable directly in production, because Next keeps a
  client-sent `X-Forwarded-For` as is. Set `TRUSTED_PROXY_HOPS` (frontend, default 1) to the number of
  such proxies: the BFF sends the entry the outermost one wrote as `X-Forwarded-For`/`X-Real-IP` and
  drops anything to its left. Set `FORWARDED_ALLOW_IPS` (API, default `127.0.0.1`) to the BFF's
  address: the app believes `X-Forwarded-For` only from those peers (uvicorn's own
  `--forwarded-allow-ips` reads the same variable). Without this every login shares one rate-limit
  bucket and audit IP hashes all hash the BFF.
- Do not buffer SSE: `/v1/answer` and `/v1/agent/runs/{id}/events` send `X-Accel-Buffering: no` and
  `Cache-Control: no-cache`. Allow long reads (agent streams run up to 15 min).
- Security headers are set by the app: Next sends CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy`, `Permissions-Policy` and (production) HSTS on every response; the API sends
  `nosniff` + `X-Frame-Options: DENY`. Serve over HTTPS so HSTS takes effect.
- `CORS_ORIGINS` must list the frontend origin(s).
- OAuth: `OAUTH_REDIRECT_BASE` is the public WEB app base (default `http://localhost:3000`); the
  redirect URI registered with each provider is `<OAUTH_REDIRECT_BASE>/oauth/finish`. That page
  POSTs `/v1/connectors/oauth/finish` through the BFF (same user, org and browser as the start).

## Signals and shutdown

- Scheduler: handles SIGINT and SIGTERM and exits within one tick. Signal the `python` process
  itself. On Windows, `uv run` starts a wrapper that does not forward signals: press Ctrl+C in the
  scheduler's own console, or stop the child `python.exe` by PID. In Docker or systemd, run
  `python -m app.connectors.scheduler` directly (not through `uv run`) so SIGTERM reaches it.
- Worker: Dramatiq handles SIGTERM/SIGINT with a graceful stop. A killed worker leaves sync runs
  `running`; the reaper fails them after the 4 h time limit + 10 min. Messages a killed worker held are
  redelivered by Dramatiq maintenance later; `POST /v1/documents/{id}/reprocess` recovers a stuck
  document at once.
- API: uvicorn graceful shutdown. A cut SSE answer saves the partial message.

## Upgrades

1. Back up Postgres.
2. `uv run alembic upgrade head` (one transaction per migration).
3. Restart API, workers and scheduler. After `0008`, refresh tokens issued before it have no session
   row, so users sign in once more. After `0012`, agent model usage is reported as kind `agent`
   (earlier agent rows stay `chat`), and audit rows carry `actor_email` from then on.

## CI

`.github/workflows/ci.yml` checks every push and pull request; it does not deploy. It starts
Postgres/Redis/MinIO from `docker-compose.yml` with per-run generated passwords, so the role
bootstrap in `backend/docker/initdb` is exercised on every run. Process start: `import app.worker`
no longer loads the `anthropic`/`openai` SDKs (they load on the first LLM call), about 4 s instead
of 11 s.

## Health

`GET /healthz` (process up) and `GET /readyz` (DB answers `SELECT 1`). No metrics endpoint yet.
