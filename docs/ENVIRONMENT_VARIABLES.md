# Environment variables

Generated from the pydantic `BaseSettings` classes. Names are case-insensitive. Every backend settings
class reads the environment first, then `../.env` relative to the working directory (so run backend
commands from `backend/` to pick up the repo-root `.env`). Unknown variables are ignored.

Legend: **Req** = required (no default; startup fails without it). **Secret** = scrubbed from logs
and must never be committed.

## Core: `backend/app/core/config.py` (`Settings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `DATABASE_URL` | – | yes | yes | App connection as `rag_app` (`postgresql+asyncpg://rag_app:…@host:5432/rag`). RLS applies |
| `MIGRATION_DATABASE_URL` | – | yes | yes | `rag_owner` connection for Alembic (non-superuser, ADR 0009) |
| `REDIS_URL` | `redis://localhost:6379/0` | | yes | Dramatiq broker, rate limits, OAuth state. With compose: `redis://:<REDIS_PASSWORD>@localhost:6379/0` |
| `JWT_SECRET` | – | yes | yes | HS256 signing key, ≥ 32 chars |
| `ACCESS_TOKEN_TTL_MINUTES` | `15` | | | Access token lifetime (> 0) |
| `REFRESH_TOKEN_TTL_DAYS` | `7` | | | Refresh token / session lifetime (> 0) |
| `REFRESH_REUSE_GRACE_SECONDS` | `30` | | | 0–300. A rotated refresh token sent again inside this window returns the same new pair |
| `FERNET_KEYS` | – | yes | yes | Comma-separated Fernet keys; first encrypts, all decrypt. Validated at startup |
| `CORS_ORIGINS` | `http://localhost:3000` | | | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | | | Root log level |
| `EMBEDDING_API_KEY` | none | | yes | Read here only for log scrubbing; used by the embedder (below) |
| `QDRANT_API_KEY`, `RERANK_API_KEY` | none | | yes | Read here only for log scrubbing; used by `retrieval/qdrant_store` and `retrieval/rerank` (below) |
| `CONNECTORS_ALLOW_PRIVATE_ENDPOINTS` | `false` | | | Server gate for per-source `allow_private_endpoint` (RFC1918/ULA). Self-hosted/BYOC only |
| `API_KEY_PEPPER` | none (derived from `JWT_SECRET`) | | yes | HMAC key for API-key hashes and audit IP hashes. Set it in production |
| `RATE_LIMIT_AUTH` | `10` | | | Login + register requests per minute per client IP |
| `RATE_LIMIT_ANSWER` | `30` | | | `/v1/answer` + agent run starts per minute per user/key |
| `RATE_LIMIT_UPLOAD` | `60` | | | Uploads per minute per user/key |
| `RATE_LIMIT_SEARCH` | `120` | | | `/v1/search` per minute per user/key |
| `MAX_CONCURRENT_STREAMS` | `5` | | | Open SSE streams (`/v1/answer` + agent `/events`, together) per user/key; more -> `429 too_many_streams` with `Retry-After`. Fails open when Redis is down |
| `RATE_LIMIT_NAMESPACE` | `""` | | | Prefix for limiter keys so dev/test servers sharing one Redis keep separate buckets. Empty in production |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | | | Peers whose `X-Forwarded-For` sets the client IP (rate limits, audit/session IP hashes). Comma list of IPs/CIDRs; set it to the BFF's address. uvicorn reads the same variable. Never `*` when the API port is reachable directly |
| `CONNECTOR_GDRIVE_ENABLED` | `false` | | | Shows the Google Drive connector |
| `GOOGLE_OAUTH_CLIENT_ID` | none | | | Google OAuth client ("Web application") |
| `GOOGLE_OAUTH_CLIENT_SECRET` | none | | yes | |
| `OAUTH_REDIRECT_BASE` | `http://localhost:3000` | | | Public WEB app base; providers redirect to `<base>/oauth/finish` (that page POSTs `/v1/connectors/oauth/finish`) |
| `FRONTEND_URL` | `http://localhost:3000` | | | Where the OAuth callback redirects the browser |
| `CONNECTOR_ONEDRIVE_ENABLED` | `false` | | | Shows the OneDrive/SharePoint connector (in progress) |
| `MICROSOFT_OAUTH_CLIENT_ID` | none | | | Entra app registration |
| `MICROSOFT_OAUTH_CLIENT_SECRET` | none | | yes | |
| `MICROSOFT_OAUTH_TENANT` | `common` | | | `common`, `organizations`, `consumers`, or a tenant id/domain |
| `CONNECTOR_DROPBOX_ENABLED` | `false` | | | Shows the Dropbox connector (in progress) |
| `DROPBOX_OAUTH_CLIENT_ID` | none | | | Dropbox app key |
| `DROPBOX_OAUTH_CLIENT_SECRET` | none | | yes | Dropbox app secret |
| `CONNECTOR_REST_ENABLED` | `true` | | | Shows the generic REST connector and `POST /v1/connectors/rest/openapi` (needs no provider app) |
| `MAX_REQUEST_BODY_KB` | `1024` | | | Request body cap (413) for every route except document upload, which allows `MAX_UPLOAD_MB` + 1 MB. Enforced before routing/auth, also for chunked bodies |

## Ingestion: `backend/app/ingestion/config.py` (`IngestionSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `BLOB_BACKEND` | `local` | | | `local` or `s3` |
| `BLOB_DIR` | `./data/blobs` | | | Local blob root (relative to the working dir) |
| `S3_ENDPOINT_URL` | `http://localhost:9000` | | | S3-compatible endpoint (MinIO in compose). Unset for AWS |
| `S3_REGION` | `us-east-1` | | | |
| `S3_BUCKET` | `rag-documents` | | | Blob bucket |
| `S3_ACCESS_KEY` | none | | yes | Blob-store key; also the MinIO root user in compose |
| `S3_SECRET_KEY` | none | | yes | Blob-store secret; also the MinIO root password in compose |
| `MAX_UPLOAD_MB` | `50` | | | Upload and connector object size cap |
| `CHUNK_TOKENS` | `400` | | | Chunk size (≥ 50, whitespace words) |
| `CHUNK_OVERLAP` | `50` | | | Overlap between text chunks (≥ 0, capped at half the size) |
| `TABLE_CHUNK_CAP_TOKENS` | `1200` | | | Max table chunk before splitting into row groups (≥ 50) |
| `PARSE_TIMEOUT_S` | `120` | | | PDF/DOCX/XLSX/HTML are parsed in a child process killed after this; the document fails (no retry) |
| `PARSE_MEMORY_MB` | `2048` | | | Address-space limit of that child (Linux/macOS only; Windows relies on the caps below) |
| `MAX_UNZIPPED_MB` | `256` | | | DOCX/XLSX and ZIP/TAR archives: max total unpacked size |
| `MAX_ZIP_MEMBERS` | `10000` | | | DOCX/XLSX: max parts in the zip |
| `MAX_ZIP_RATIO` | `100` | | | DOCX/XLSX and archives: max compression ratio of any part (or a whole archive) over 1 MB unpacked |
| `MAX_PDF_PAGES` | `2000` | | | PDF page cap (also the frame cap of multi-page TIFF images) |
| `OCR_ENABLED` | auto | | | Unset = on when the `tesseract` binary is found (startup log says which). `false` turns OCR off; `true` without the binary logs a warning and stays off |
| `OCR_LANGS` | `eng` | | | Tesseract languages, `+`-joined (`eng+deu`); each needs its traineddata installed |
| `TESSERACT_CMD` | `tesseract` | | | Binary name or full path (Windows: `C:\Program Files\Tesseract-OCR\tesseract.exe`) |
| `OCR_DPI` | `300` | | | Render resolution of image-only PDF pages (72-600) |
| `MAX_IMAGE_PIXELS` | `50000000` | | | Pixel cap per image / rendered page (decompression-bomb guard); bigger images fail, big PDF pages render smaller |
| `ARCHIVE_MAX_MEMBERS` | `1000` | | | ZIP/TAR: max files in one archive (over = archive `failed`). Total size and ratio use `MAX_UNZIPPED_MB` / `MAX_ZIP_RATIO` |
| `ARCHIVE_MAX_DEPTH` | `1` | | | `1` = archives inside an archive are skipped with a reason; `2` unpacks one more level |
| `TRANSCRIBE_ENABLED` | auto | | | Unset = on when faster-whisper and the model are found locally (startup log says which). `false` = audio/video fail readably; SRT/VTT always work |
| `TRANSCRIBE_MODEL` | `small` | | | Whisper size (`tiny`, `base`, `small`, `medium`, `large-v3`) or a local model directory |
| `TRANSCRIBE_MODEL_DIR` | HF cache | | | Where cached models are looked up / downloaded (Hugging Face cache layout) |
| `TRANSCRIBE_ALLOW_DOWNLOAD` | `false` | | | `true` downloads a missing model on first use (needs network to huggingface.co) |
| `MAX_MEDIA_MINUTES` | `120` | | | Longest audio/video accepted; checked from the container header before decoding |
| `TRANSCRIBE_TIMEOUT_FACTOR` | `2.0` | | | Transcription timeout = `PARSE_TIMEOUT_S` + duration x this |
| `TRANSCRIBE_MEMORY_MB` | `4096` | | | Address-space cap of the transcription child (POSIX only) |
| `MAX_SUBTITLE_CUES` | `50000` | | | SRT/VTT cue cap per file |
| `MAX_SHEET_CELLS` | `1000000` | | | XLSX cell cap (workbook read in streaming mode) |
| `MAX_TEXT_CHARS` | `20000000` | | | Extracted text cap per document |
| `MAX_CHUNKS_PER_DOC` | `20000` | | | Chunk cap per document |

## LLM: `backend/app/llm/config.py` (`LLMSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `LLM_PROVIDER` | `anthropic` | | | `anthropic` or `openai_compat` |
| `LLM_MODEL` | `claude-sonnet-5-5` | | | Chat and agent model |
| `LLM_FALLBACK_MODEL` | none | | | Second model, tried on a retryable error before any text streamed |
| `LLM_REWRITE_MODEL` | none (`claude-haiku-5-5` for anthropic, else `LLM_MODEL`) | | | Follow-up question rewrite |
| `LLM_BASE_URL` | none | | | OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, Azure) |
| `LLM_TIMEOUT_S` | `60.0` | | | Per-call timeout (> 0) |
| `LLM_MAX_RETRIES` | `2` | | | SDK retries, 0–5 |
| `ANTHROPIC_API_KEY` | none | for `anthropic` | yes | Without it `/v1/answer` returns 503 `llm_not_configured` |
| `OPENAI_API_KEY` | none | for `openai_compat` unless `LLM_BASE_URL` | yes | |

## Embeddings: `backend/app/retrieval/embedders.py` (`EmbeddingSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `EMBEDDING_PROVIDER` | `fastembed` | | | `fastembed` (local ONNX, no key) or `openai` (OpenAI-compatible HTTP) |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | | | Model for new indexes |
| `EMBEDDING_DIM` | `384` | | | Vector size, 1–2000 (HNSW limit) |
| `EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | | | For `openai` |
| `EMBEDDING_API_KEY` | none | for `openai` | yes | |
| `EMBEDDING_BATCH` | `64` | | | Texts per request, 1–2048. The budget is checked and one usage row written per batch |
| `EMBEDDING_TIMEOUT_S` | `60.0` | | | Per-request timeout |

Changing the embedding settings affects new indexes only. Existing KBs keep their index's model until
`POST /v1/kbs/{id}/reindex`.

## Vector store: `backend/app/retrieval/qdrant_store.py` (`VectorStoreSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `VECTOR_STORE` | `pgvector` | | | `pgvector` (vectors in the app DB) or `qdrant` (external Qdrant, e.g. customer-owned). Deployment-wide; switching it needs a reindex of every KB (vector leg returns nothing for an index built in the other store; keyword still answers) |
| `QDRANT_URL` | `http://localhost:6333` | if `qdrant` | | Qdrant REST URL. Operator-set, never user input (not passed through the connector SSRF guard) |
| `QDRANT_API_KEY` | none | | yes | Qdrant API key. Mirrored in core config (above) so it is scrubbed from logs by value |
| `QDRANT_TIMEOUT_S` | `10` | | | Per-request timeout, seconds (> 0) |

## Reranker: `backend/app/retrieval/rerank.py` (`RerankSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `RERANK_PROVIDER` | `none` | | | `none`, `local` (fastembed ONNX cross-encoder on CPU, no key) or `cohere` (HTTP, contract-tested only, writes no usage row yet) |
| `RERANK_MODEL` | empty | | | Empty = `jinaai/jina-reranker-v2-base-multilingual` (local) / `rerank-v3.5` (cohere) |
| `RERANK_TOP_N` | `20` | | | Fused candidates reranked before the k cut, 1–150 |
| `RERANK_BASE_URL` | `https://api.cohere.com/v2` | | | For `cohere` |
| `RERANK_API_KEY` | none | for `cohere` | yes | |
| `RERANK_TIMEOUT_S` | `10.0` | | | Per-request timeout (`cohere`) |

## Sessions: `backend/app/auth/sessions.py` (`SessionSettings`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `SESSION_RETENTION_DAYS` | `30` | | | Login `sessions` rows expired or revoked longer than this are deleted on each scheduler tick (`prune_sessions`, floored at 1 day) |

## Docker Compose (`docker-compose.yml`)

| Variable | Default | Req | Secret | Purpose |
|---|---|---|---|---|
| `POSTGRES_PASSWORD` | – | yes | yes | Superuser `rag` password (bootstrap only) |
| `APP_DB_PASSWORD` | – | yes | yes | `rag_app` password (used by `initdb` on first start) |
| `OWNER_DB_PASSWORD` | – | yes | yes | `rag_owner` (table owner, migrations) password (used by `initdb` on first start) |
| `REDIS_PASSWORD` | – | yes | yes | Redis `requirepass`; put it in `REDIS_URL` too |
| `POSTGRES_PORT` | `5432` | | | Host port |
| `REDIS_PORT` | `6379` | | | Host port |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | – | yes | yes | MinIO root credentials (same vars as the blob store) |
| `MINIO_PORT` | `9000` | | | Host port (API) |
| `MINIO_CONSOLE_PORT` | `9001` | | | Host port (console) |
| `QDRANT_PORT` | `6333` | | | Host port (profile `qdrant`) |
| `RAG_ENV_FILE` | `.env` | | | Env file the `app` profile services read for app settings and secrets (`JWT_SECRET`, `FERNET_KEYS`, provider keys, ...) |
| `RAG_TAG` | `local` | | | Tag of the `rag-backend` / `rag-web` images built and run by the `app` profile |
| `API_PORT` | `8000` | | | Host port of the API (`127.0.0.1` only) |
| `WEB_PORT` | `3000` | | | Host port of the web app (loopback only); also sets the API's `CORS_ORIGINS`, `OAUTH_REDIRECT_BASE`, `FRONTEND_URL` to `http://localhost:<WEB_PORT>` |
| `RAG_EDGE_SUBNET` | `10.231.47.0/24` | | | Subnet of the `edge` network (web + api only); also the API's `FORWARDED_ALLOW_IPS` in compose. Change it if it overlaps a local network |

The backend image sets `FASTEMBED_CACHE_PATH=/models/fastembed`, `HF_HOME=/models/hf`,
`TRANSCRIBE_MODEL_DIR=/models/whisper` (model caches on the `/models` volume) and
`BLOB_DIR=/data/blobs`. Compose builds `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `REDIS_URL` and the
S3 settings for the `app` services from the variables above; see [DEPLOYMENT.md](DEPLOYMENT.md#containers).

## Tests and evals only (`backend/tests/conftest.py`, `backend/evals/run_eval.py`)

| Variable | Default | Purpose |
|---|---|---|
| `RAG_TEST_DB` | `rag_test` | Test DB name; must match `rag_test[a-z0-9_]*` |
| `TEST_DATABASE_URL` | built from `APP_DB_PASSWORD` (fallback `rag_dev_app_pw`), `POSTGRES_PORT`, `RAG_TEST_DB` | Overrides the test app URL (tests only) |
| `TEST_MIGRATION_DATABASE_URL` | `rag_owner` + `OWNER_DB_PASSWORD` (fallback `rag_dev_table_owner_pw`) | Overrides the test owner URL (tests only) |
| `TEST_ADMIN_DATABASE_URL` | `rag` + `POSTGRES_PASSWORD` (fallback `rag_dev_owner_pw`) | Superuser URL: creates the test DB + `vector`, and the source DBs of the Postgres-connector tests. A test DB still owned by the superuser is handed to `rag_owner` and migrated once as the superuser (0011) |

The test suite overwrites `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `JWT_SECRET`, `FERNET_KEYS` and
`RATE_LIMIT_AUTH`, and sets a unique `RATE_LIMIT_NAMESPACE`, before the app loads. The S3 round-trip
test runs only when `S3_ACCESS_KEY` and `S3_SECRET_KEY` are set.

## Frontend (`frontend/`)

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | API base. Read server-side by the BFF routes |
| `REFRESH_TOKEN_TTL_DAYS` | `7` | Server-only (1–90). Refresh-cookie lifetime; must equal the backend's `REFRESH_TOKEN_TTL_DAYS` |
| `TRUSTED_PROXY_HOPS` | `1` | Server-only (1–10). Reverse proxies in front of Next that append to `X-Forwarded-For`; the BFF forwards the entry the outermost one wrote as the client IP and drops anything left of it |
| `PUBLIC_ORIGIN` | unset | Server-only. The origin browsers use, e.g. `https://app.example.com` (http(s), no path or trailing slash; an invalid value stops the BFF routes). The CSRF check accepts only this `Origin`. Unset: the browser's `Host` header plus the scheme from `X-Forwarded-Proto` (the entry the outermost trusted proxy wrote). `X-Forwarded-Host` is never read. Recommended in production |
| `NODE_ENV` | set by Next.js | `production` makes session cookies `Secure` and adds HSTS |

An empty or blank value (`API_KEY_PEPPER=`) counts as unset for every optional backend setting.

A ready-to-copy template: [.env.example.md](.env.example.md).
