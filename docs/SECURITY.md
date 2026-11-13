# Security

Controls that exist in the code today, with where they live and how they are tested. Gaps are listed
at the end and in [THREAT_MODEL.md](THREAT_MODEL.md). No external security review has been done.

## Tenant isolation

- Postgres RLS, `ENABLE` + `FORCE`, on every tenant table; policies read transaction-local
  `app.org_id` / `app.user_id` set at the start of every transaction (`app/core/db.py`).
- The app connects as `rag_app` (NOSUPERUSER, NOBYPASSRLS). Migrations run as `rag_owner` (NOSUPERUSER, NOBYPASSRLS); cross-tenant SECURITY DEFINER
  functions are owned by `rag_definer` (NOLOGIN, BYPASSRLS, narrow grants); see ADR 0009.
- Compose publishes Postgres, Redis and MinIO on loopback only; Redis requires a password.
- Composite `(org_id, …)` foreign keys stop cross-org references (FK checks ignore RLS).
- Conversations (and their messages), agent runs (and their steps and reports) and sessions are private per user, not
  just per org.
- Other orgs' ids return 404, never 403 (no existence leak).
- Cross-org reads happen only through four SECURITY DEFINER functions with fixed `search_path`,
  `EXECUTE` for `rag_app` only, and narrow outputs ([DATABASE_SCHEMA.md](DATABASE_SCHEMA.md#security-definer-functions)).
- Tests: `tests/test_tenancy.py` enumerates every table (RLS on and forced, cross-org reads/writes
  blocked); per-module 404 tests.

## Authentication

- Passwords: argon2 (pwdlib). Unknown emails are verified against a dummy hash, so login timing does
  not reveal which accounts exist. Failed logins go to the log (user id only if the account exists),
  never the email.
- Access JWT (HS256, `JWT_SECRET` ≥ 32 chars, 15 min). Refresh JWT whose `jti` is a `sessions` row:
  rotation on every refresh, 30 s reuse grace for parallel refreshes, then reuse = theft → the whole
  login family is revoked and `session.reuse_detected` is audited ([ADR 0008](adr/0008-refresh-session-grace-window.md)).
- Users RLS (0010): a session sees its own row and its current org's members; `rag_app` cannot
  read `pw_hash` at all (column grants), login uses the `auth_user_by_email` definer function.
- Joining an org needs the invitee's consent: adding a member creates an invite (7 days) that the
  signed-in user with that email accepts. The invite answer is the same whether or not an account
  exists. **Email verification is deferred** until an email provider exists: whoever registered an
  address first sees invites to it, so admins should confirm the person before they accept (the
  pending list and audit `invite.accept` show who joined).
- Logout, logout-everywhere and member removal revoke sessions. Access tokens stay valid until they
  expire (≤ 15 min).
- API keys: `rag_<12-hex prefix>_<256-bit secret>`; only `HMAC-SHA256(API_KEY_PEPPER, secret)` is
  stored and compared in constant time ([ADR 0006](adr/0006-hmac-api-keys.md)). Keys expire, can be
  revoked (immediate), act with the creator's current role, are capped by scopes, and cannot mint
  wider keys.

## Authorization

- Every route declares a scope (`need(...)`) and a role check. `tests/test_rbac.py` fails if any
  route is missing from its policy table, and checks every route × 4 roles.
- Owner rules: only owners grant or remove owner; the last owner cannot be removed or demoted (owner
  rows are locked during the check).

## Frontend (BFF)

- Tokens live in httpOnly cookies (`rag_at`, `rag_rt`; `Secure` in production; SameSite Lax). Browser
  JS never sees them.
- CSRF: session routes and every proxied non-GET request reject a cross-site `Sec-Fetch-Site` or a
  mismatched `Origin` (403), and session POSTs require `application/json` (415). The expected origin
  is `PUBLIC_ORIGIN` when set, else the request's `Host` with the trusted proxy's `X-Forwarded-Proto`;
  `X-Forwarded-Host` is never trusted.
- Proxy path guard: `.`/`..`/empty segments, encoded slashes or backslashes, and malformed escapes →
  400; the resolved URL must keep the API origin and `/v1/` prefix.
- Open redirects: `?next=` is resolved against the app origin and must stay same-origin.
- OAuth return: the authorize URL must be https on an allowlisted host per connector type.
- OAuth binding (FX-1, S2; RFC 9700 §4.7): the provider redirect lands on the web app page `/oauth/finish`,
  which posts code+state to the authenticated `POST /v1/connectors/oauth/finish`. The state is bound to the
  starting user + org and to a per-browser random value (httpOnly `rag_oauth` cookie, SameSite=Lax, path
  `/api/v1/connectors/oauth`, 10 min, set by the BFF at start; page JS can neither read nor send it). Any
  mismatch → 403 `oauth_not_yours` before the code is exchanged; the state is single-use either way.
  PKCE (S256) stays. **FX-1 is the precondition for turning any `CONNECTOR_*_ENABLED` OAuth flag on.**

## Outbound requests (SSRF)

Every user-supplied URL or host (website, S3 endpoint, Postgres host, REST base/token URL, OneDrive download redirect) is
resolved and every DNS answer must be public unicast; the connection is pinned to the checked IP.
http/https only, no userinfo, standard ports unless opted in, redirects re-checked. Private
endpoints need both a per-source opt-in and the server gate `CONNECTORS_ALLOW_PRIVATE_ENDPOINTS`, and
even then loopback, link-local and metadata stay blocked. Details:
[CONNECTOR_ARCHITECTURE.md](CONNECTOR_ARCHITECTURE.md#ssrf-and-private-endpoints-ssrfpy).

## Secrets

- Connector credentials and OAuth tokens: Fernet (`MultiFernet`; first key encrypts, any key
  decrypts, so keys rotate by prepending). Never returned by the API (`has_secret` only).
- Startup validates `JWT_SECRET` length and every Fernet key.
- Logs: JSON lines; every configured secret value is replaced with `[REDACTED]`, and
  `password|secret|token|api_key|authorization=...` pairs are scrubbed. Request logs record the path
  only (query strings can carry tokens); uvicorn's access log is disabled.
- Tests assert no secret reaches logs (`tests/test_security.py`, connector tests, API key tests).

## Abuse limits

- Rate limits (Redis, 60 s window): auth per IP (fails closed), answer/agent start, upload, search per
  user or key (fail open). The client IP comes from `X-Forwarded-For` only when the peer is in
  `FORWARDED_ALLOW_IPS` (the BFF), and the BFF forwards only the entry its trusted proxy wrote
  (`TRUSTED_PROXY_HOPS`); see [DEPLOYMENT.md](DEPLOYMENT.md#reverse-proxy).
- Concurrent streams: at most `MAX_CONCURRENT_STREAMS` open answer/agent-event streams per user or
  key (Redis sorted set, one entry per stream). Checked before the stream starts (`429
  too_many_streams`, nothing stored); released when the stream ends or the client disconnects; an
  entry left by a crashed process stops counting after 960 s. Fails open (warning log) if Redis is down.
- Budgets: per-org monthly USD limit checked before every LLM call and every embedding batch (402 in
  block mode). Usage of batches already billed is committed even when a later batch or the job fails.
- Request body cap before routing and auth (`MAX_REQUEST_BODY_KB`; uploads `MAX_UPLOAD_MB` + 1 MB):
  413 on a large `Content-Length`, and chunked bodies are cut off at the limit. In production also set
  the proxy's `client_max_body_size`.
- Upload size cap (`MAX_UPLOAD_MB`), type detected from content.
- Parser bombs: DOCX/XLSX zip checks (unpacked size, part count, compression ratio) before opening,
  XLSX read in streaming mode with a cell cap, PDF page cap, extracted-text and chunk caps. PDF, DOCX,
  XLSX and HTML parse in a spawned child with a wall-clock timeout (and a memory limit on POSIX), so a
  bomb kills only the child. Any limit hit = document `failed` with a readable reason, never partially
  indexed and never retried. Chunks without whitespace are hard-split so a provider never gets one
  oversized input.
- Images/OCR: images and rendered PDF pages capped at `MAX_IMAGE_PIXELS` (PIL bomb check at open);
  OCR runs in the same isolated child. `OCR_LANGS` is pattern-validated (it reaches the tesseract
  command line).
- Audio/video: duration checked from the container header against `MAX_MEDIA_MINUTES` before decoding; decoding and transcription run in the isolated child with a duration-scaled timeout and `TRANSCRIBE_MEMORY_MB` (POSIX). No model is downloaded unless `TRANSCRIBE_ALLOW_DOWNLOAD=true`. Subtitles: `MAX_SUBTITLE_CUES` + `MAX_TEXT_CHARS`.
- Archives (ZIP/TAR/TAR.GZ/GZ): never extracted to disk; member names never become paths (blobs are
  keyed by sha256). Absolute, drive-letter and `..` names, symlinks, hard links and devices are refused
  and reported. Member count, total unpacked size and compression ratio are checked while walking the
  headers, so a bomb fails the archive before members are read; each member is capped at
  `MAX_UPLOAD_MB` (`ZipExtFile` stops at the declared size and checks the CRC). Nested archives are
  skipped beyond `ARCHIVE_MAX_DEPTH` (default 1). Archive reading runs in the worker process (stdlib,
  bounded by these byte caps), not the parse child; each member is then parsed in its own job.

## Data handling

- Audit log: append-only for the app role; actions record ids and field names only, IPs as
  `HMAC(pepper, "ip:<addr>")`. Audit rows commit in the same transaction as the action.
  Decision (H3, 0012): the actor's email is snapshotted into `actor_email` and nowhere else in the
  row (never in `meta`). Owners/admins, the only readers, already see member emails; without the
  snapshot a removed member's actions would show only a user id that users RLS no longer resolves.
- Login sessions: rows expired or revoked more than `SESSION_RETENTION_DAYS` (30) ago are deleted by
  the scheduler through the `prune_sessions` definer function; `rag_app` still has no DELETE.
- CSV exports and agent XLSX reports neutralize formula-leading cells (`= + - @`, tab, CR), XLSX
  header rows included.
- Streaming endpoints (`/v1/answer`, agent run events) hold no pooled DB connection while a model
  generates or a stream waits; agent `calculate`/`extract_table` inputs are bounded so their GIL-held
  C work stays in milliseconds.
- Retrieved text is untrusted: escaped and fenced in prompts; agent tool outputs wrapped as
  `untrusted_data`; citation markers validated against the real context.
- REST connector: API responses are data. Only GET; no redirects followed (credentials stay on
  base_url's origin and the token URL); next-page links must be same-origin; bodies are never
  echoed in errors; credential headers are refused in plain config; an api key sent as a query
  param is stripped from httpx's request log lines. OpenAPI import parses the spec as data (safe
  YAML without aliases, local `$ref` only, size/node/depth caps) and never calls the API.
- Postgres connector: read-only session, catalog-checked quoted identifiers, write-capable roles
  refused. No raw SQL from users or models anywhere.
- Errors: 500s return a generic message with a request id; LLM provider errors are mapped to fixed
  messages.

## HTTP

- CORS: explicit allowlist (`CORS_ORIGINS`), credentials allowed, fixed methods and headers.
- Next (`next.config.ts`): `Content-Security-Policy` (`default-src 'self'`, `frame-ancestors 'none'`,
  `object-src 'none'`, `base-uri`/`form-action`/`connect-src 'self'`), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, a deny-all `Permissions-Policy`, HSTS in
  production. `script-src` keeps `'unsafe-inline'`: the App Router streams inline bootstrap scripts and
  a nonce would force per-request rendering (incompatible with `cacheComponents`). React's escaping
  remains the XSS defense.
- API: `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY` on every response.

## Known gaps

See [THREAT_MODEL.md](THREAT_MODEL.md#known-gaps). Main ones: no email verification; connector ACLs
(Drive/OneDrive/Dropbox) are not enforced per user; script CSP allows inline scripts; no CI secret scanning or dependency audit yet.
