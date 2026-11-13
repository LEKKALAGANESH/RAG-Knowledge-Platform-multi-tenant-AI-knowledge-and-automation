# Troubleshooting

Real problems hit while building this, with causes and fixes.

## Backend will not start: missing `.env` / validation error

**Symptom:** `ValidationError` for `database_url`, `migration_database_url`, `jwt_secret` or
`fernet_keys`; or `docker compose up` stops with `set POSTGRES_PASSWORD in .env`.

**Cause:** there is no `.env` in the repo root (it is git-ignored, and agents cannot write it), or
the process runs from a directory where `../.env` is not the repo root.

**Fix:** create `.env` from [.env.example.md](.env.example.md) and run backend commands from
`backend/`. For a throwaway session, load a generated env into the shell instead (writes no file;
secrets are new on every run, so sign-ins and stored connector tokens from an earlier run stop working):
`uv run python scripts/dev_env.py --shell powershell | Invoke-Expression` or
`eval "$(uv run python scripts/dev_env.py --shell bash)"`. A variable already set in the shell wins
(e.g. `REDIS_URL` without a password for a Redis started before compose required one).

## Worker hangs with more than one thread (fixed)

**Symptom:** with `dramatiq app.worker --threads 2` (or the defaults), one of two jobs that start at
the same time never finishes. Postgres shows an idle connection that never sent a query.

**Root cause:** the worker shared one SQLAlchemy engine across threads. Each Dramatiq thread runs its
own `asyncio.run` loop. The engine guards its first connect with an `asyncio.Lock`; thread A held it,
thread B parked a future on loop B, and A's release called `set_result` from the wrong loop, which
never woke B.

**Fix (A2.2, A2.3):** `app/core/db.py` `worker_sessions()` keeps one `NullPool` engine per worker
thread (`threading.local`), used by every actor. Regression tests run a real Dramatiq worker with 2
threads. If you add an actor, use `worker_sessions()`, never `SessionLocal` or a module-level engine.

## `deadlock detected` during reindex or KB delete (fixed)

**Cause:** `drop_index` ran a DELETE (ROW EXCLUSIVE) then `DROP INDEX` (upgrade to ACCESS EXCLUSIVE)
in one transaction; two concurrent drops deadlocked. `create_index` had the same shape (CREATE INDEX
takes SHARE, then an upsert takes ROW EXCLUSIVE).

**Fix:** `retrieval/vectorstore.py` takes `LOCK TABLE chunk_embeddings` first (ACCESS EXCLUSIVE before
drop, SHARE ROW EXCLUSIVE before create). Keep lock order "strongest first" for new DDL paths.

## Documents stay `processing` or fail right after an upgrade

**Cause:** the database is behind the code (e.g. the pipeline embeds into `embedding_indexes`, which
needs migration 0002).

**Fix:** `cd backend && uv run alembic upgrade head`, then restart the worker. Check with
`uv run alembic current`. A document whose `error` says "Retrying (attempt n of 5)" is mid-retry;
`POST /v1/documents/{id}/reprocess` re-queues it.

## Scheduler does not stop on Windows

**Symptom:** sending a signal to the `uv run python -m app.connectors.scheduler` process does nothing.

**Cause:** on Windows, `uv run` starts a wrapper process that does not forward signals to the
`python.exe` child.

**Fix:** press Ctrl+C in the scheduler's own console, or stop the child `python.exe` by its PID. In
containers, run `python -m app.connectors.scheduler` directly so SIGTERM reaches it.

## `429 rate_limited` on register/login during local work

**Cause:** auth limits are per client IP in a shared Redis. Every local server, test run and helper
uses 127.0.0.1, so they all spend one bucket (default 10 per minute).

**Fix:** give each dev/test server its own `RATE_LIMIT_NAMESPACE` (pytest already does this), or
raise `RATE_LIMIT_AUTH` for local runs. Behind a reverse proxy, every user sharing one bucket means
the client IP is lost: check the proxy appends `X-Forwarded-For`, `TRUSTED_PROXY_HOPS` matches the
proxy count, and `FORWARDED_ALLOW_IPS` lists the BFF's address.

## `429 too_many_streams` on answer or agent events

**Cause:** the user or API key already has `MAX_CONCURRENT_STREAMS` (default 5) answer/agent-event
streams open, e.g. several tabs following runs. **Fix:** close a stream or wait `Retry-After`
seconds; raise `MAX_CONCURRENT_STREAMS` if this is normal use. A process that crashed holding
streams frees its slots after 960 s.

## `503 rate_limiter_unavailable` on login

**Cause:** Redis is down. Login and register fail closed by design; other routes keep working.
**Fix:** `docker compose up -d redis`. If Redis is up but logs show `NOAUTH Authentication required`
or `invalid password`, `REDIS_URL` lacks `REDIS_PASSWORD`: use `redis://:<REDIS_PASSWORD>@localhost:6379/0`.

## Compose: `set REDIS_PASSWORD in .env` / `set OWNER_DB_PASSWORD in .env`

**Cause:** FX-6 added two required compose secrets. **Fix:** add both to `.env` (see
`.env.example.md`). On an existing DB volume, also run the one-off owner-role steps in DEPLOYMENT.

## Migration fails: `roles rag_owner/rag_definer missing` or `must be owner of table`

**Cause:** the DB volume predates `02-owner-role.sh`, or 0011 ran as `rag_owner` on tables still
owned by the superuser. **Fix:** DEPLOYMENT, "Volume created before FX-6": run the script, then
migrate to 0011 once as the superuser.

## `query would be affected by row-level security policy` in a migration

**Cause:** a new SECURITY DEFINER function with `row_security = off` is owned by `rag_owner`, which
has no BYPASSRLS. **Fix:** grant `rag_definer` what the body touches and
`ALTER FUNCTION ... OWNER TO rag_definer` (ADR 0009).

## `503 llm_not_configured`

**Cause:** no model credentials. **Fix:** set `ANTHROPIC_API_KEY`, or `LLM_PROVIDER=openai_compat`
plus `OPENAI_API_KEY` or `LLM_BASE_URL`, then restart the API **and** the worker (agent runs fail in
the worker with the same code).

## Frontend returns 500 "invalid utf-8"

**Cause:** a source file was written in cp1252 (a Windows default) instead of UTF-8, e.g. a `·`
character. Lint and typecheck do not catch it; Next.js fails at runtime.

**Fix:** write every file as UTF-8 without BOM. To find bad files:

```bash
python - <<'EOF'
import pathlib
for p in pathlib.Path("frontend").rglob("*"):
    if p.suffix in {".ts", ".tsx", ".css", ".md"} and not {"node_modules", ".next"} & set(p.parts):
        try:
            p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            print("not UTF-8:", p)
EOF
```

## S3 test skipped

**Cause:** `S3_ACCESS_KEY` / `S3_SECRET_KEY` are not in the shell environment (pytest does not read
`.env` for them). **Fix:** export the MinIO root credentials from your `.env`, then rerun
`pytest -k s3`.

## Test run refuses to start: `test DB must match rag_test[a-z0-9_]*`

**Cause:** `RAG_TEST_DB` (or a `TEST_*DATABASE_URL`) points at a non-test database. This guard
protects the dev DB. **Fix:** use a `rag_test*` name.

## Everyone must sign in again after deploying 0008

Expected: refresh tokens issued before the `sessions` table have no row and get `401
session_revoked`. Users sign in once.

## `dramatiq app.worker` fails with a circular ImportError (fixed)

**Cause:** a package `__init__` imported an actor module, which imported `app.worker`. **Rule:**
package `__init__.py` files stay empty; `app/worker.py` imports every actor module after creating the
broker. `tests/test_security.py` imports the worker in a fresh interpreter to catch regressions.

## CI: tests fail with `connection refused` / `the database system is shutting down` right after start (fixed)

**Cause:** the old compose healthcheck (`pg_isready` over the socket) also passed on initdb's
temporary server, which listens on the socket only and stops again before Postgres starts for real.
**Fix:** the healthcheck is now `pg_isready -h 127.0.0.1` (TCP), so `docker compose up --wait` returns
only once the real server accepts TCP. If you see this, check your compose file still has `-h 127.0.0.1`.

## Flaky `[401, 401, 401]` where a rate-limit test expects a 429 (fixed)

**Cause:** fixed one-minute windows: when a test's requests crossed the minute, they were counted in
two buckets. **Fix:** `test_ratelimit.py` freezes the limiter clock mid-window.

## Helper server: first login returns 500

Known (task T2): after `python -m tests.test_agent serve` starts, the first `POST /v1/auth/login` can
fail with a Redis `'NoneType' object has no attribute 'send'`; the second works. Likely the Redis
asyncio pool is bound to the seeding event loop.
