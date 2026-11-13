# API reference

Base URL: `http://localhost:8000`. All product routes are under `/v1`. The live OpenAPI schema is at
`GET /openapi.json` (interactive docs at `/docs`). This page adds what the schema does not show:
scopes, roles, error codes and SSE event contracts. Source: `backend/app/*/router.py`,
`backend/app/auth/keys.py`.

## Authentication

| Method | Header | Notes |
|---|---|---|
| Access token (JWT, HS256, 15 min default) | `Authorization: Bearer <access_token>` | From register/login/refresh. Role-based access only |
| API key | `Authorization: Bearer rag_<prefix>_<secret>` or `X-API-Key: rag_...` | Acts as its creator's **current** role in the key's org, further limited to the key's scopes |

Scopes: `kb:read`, `kb:write`, `documents:write`, `search`, `answer`, `agent:run`, `sources:read`,
`sources:write`, `admin`. `admin` does not imply the others. A key can only create keys with a subset
of its own scopes.

Roles: `owner`, `admin`, `editor`, `viewer`. In the tables below: **all** = every role, **edit** =
owner/admin/editor, **admin** = owner/admin, **public** = no token.

## Conventions

- **Errors:** `{"error": {"code": "...", "message": "...", "request_id": "..."}}`. Every response has
  an `X-Request-ID` header (yours is echoed if it matches `[A-Za-z0-9._-]{1,64}`).
- **Common errors:** `401 not_authenticated` (no token), `401 invalid_token` (bad/expired JWT),
  `401 invalid_api_key`, `403 forbidden` (role, or membership removed), `403 insufficient_scope`,
  `404 not_found` (also for other orgs' ids), `422 validation_error`, `429 rate_limited` (with
  `Retry-After`), `500 internal_error`, unknown route `404 http_404`.
- **Pagination:** keyset. `?cursor=&limit=` → `{"items": [...], "next_cursor": "..." | null}`. Bad
  cursor → `422 invalid_cursor`.
- **Rate limits** (per minute, fixed 60 s window, Redis): `auth` per client IP (fails closed: `503
  rate_limiter_unavailable`), `answer`, `upload`, `search` per user or API key (fail open).
- **Budget:** routes that start model calls check the org's monthly budget first: `402
  budget_exceeded` (block mode) or header `X-Budget-Warning: over_budget` (warn mode, JSON routes).
- CORS exposes `X-Request-ID`, `Retry-After`, `X-Budget-Warning`.

## Routes

61 routes. The scope column is what an API key needs; signed-in users are checked by role only.

### Auth and profile

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/auth/register` | – | public | `{email, password(8–128)}` → 201 `TokenPair` | 409 `email_taken`, 429, 503 `rate_limiter_unavailable` |
| POST | `/v1/auth/login` | – | public | `{email, password}` → `TokenPair` | 401 `invalid_credentials`, 403 `no_org`, 429, 503 |
| POST | `/v1/auth/refresh` | – | public | `{refresh_token, org_id?}` → `TokenPair` | 401 `invalid_token`, 401 `session_revoked`, 403 `forbidden` (not a member of `org_id`) |
| POST | `/v1/auth/logout` | – | public | `{refresh_token}` → 204 | 401 `invalid_token` |
| POST | `/v1/auth/logout-all` | any | all | – → `{revoked}` | 403 `forbidden` when called with an API key |
| GET | `/v1/me` | any | all | → `{user:{id,email}, org:{id,name}, role}` | |

`TokenPair` = `{access_token, refresh_token, token_type: "bearer", expires_in}`. Refresh rotates the
token. Sending the same refresh token again within `REFRESH_REUSE_GRACE_SECONDS` (30) returns the
same new pair; later reuse revokes the whole login family (`401 session_revoked`). Register and login
are limited by the `auth` bucket; refresh is not.

### Orgs and members

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/orgs` | admin | all | → `[{id, name, role, created_at}]` (orgs the user belongs to) | |
| POST | `/v1/orgs` | admin | all | `{name}` → 201 `OrgOut` (caller becomes owner) | |
| GET | `/v1/orgs/{org_id}/members` | admin | all | → `[{user_id, email, role}]` | 404 other org |
| POST | `/v1/orgs/{org_id}/members` | admin | admin | `{email, role}` → 201 `InviteOut` (pending invite, nobody is added; same answer whether or not the email has an account; replaces a pending invite to that email) | 403 non-owner inviting owner, 409 `already_member` |
| GET | `/v1/orgs/{org_id}/invites` | admin | admin | → `InviteOut[]` (pending, unexpired) | 404 other org |
| DELETE | `/v1/orgs/{org_id}/invites/{invite_id}` | admin | admin | → 204 | 403 admin revoking an owner invite, 404 not pending |
| GET | `/v1/invites` | admin | all (signed in; API keys 403) | → `[{id, org_id, org_name, role, created_at, expires_at}]` for the caller's email, any org | |
| POST | `/v1/invites/{invite_id}/accept` | admin | all (signed in) | → `{org_id, role}`; membership created with the invited role | 404 not yours / expired / revoked / answered |
| POST | `/v1/invites/{invite_id}/decline` | admin | all (signed in) | → 204 | 404 as accept |
| PATCH | `/v1/orgs/{org_id}/members/{user_id}` | admin | admin | `{role}` → `MemberOut` | 403 owner rules, 404, 409 `last_owner` |
| DELETE | `/v1/orgs/{org_id}/members/{user_id}` | admin | admin | → 204 (revokes their sessions in this org) | 403, 404, 409 `last_owner` |
| GET | `/v1/orgs/{org_id}/settings` | admin | admin | → `{monthly_budget_usd, budget_action, month_spend_usd}` | 404 |
| PATCH | `/v1/orgs/{org_id}/settings` | admin | admin | `{monthly_budget_usd?: decimal(≤2dp, 0–10,000,000) or null, budget_action?: "block"\|"warn"}` → same | 422 |

`{org_id}` must be the token's org; any other id returns 404. Only an owner can grant or remove the
owner role, and an org always keeps one owner. `InviteOut` = `{id, email, role, created_at, expires_at}`;
invites expire after 7 days. No email is sent (no provider yet): the invitee sees the invite after
signing in with that email, then switches into the org with `POST /v1/auth/refresh {org_id}`.

### Knowledge bases and documents

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/kbs` | kb:read | all | `?cursor&limit(1–200, 50)` → Page `KBOut` | 422 `invalid_cursor` |
| POST | `/v1/kbs` | kb:write | edit | `{name(1–200), settings?}` → 201 `KBOut` | |
| GET | `/v1/kbs/{kb_id}` | kb:read | all | → `KBOut` | 404 |
| PATCH | `/v1/kbs/{kb_id}` | kb:write | edit | `{name?, settings?}` → `KBOut` | 404 |
| DELETE | `/v1/kbs/{kb_id}` | kb:write | edit | → 204 (drops its HNSW indexes; rows cascade) | 404 |
| GET | `/v1/kbs/{kb_id}/documents` | kb:read | all | `?cursor&limit` → Page `DocumentOut` | 404, 422 |
| POST | `/v1/kbs/{kb_id}/documents` | documents:write | edit | multipart `file` → 201 `DocumentOut` (queued), or 200 with the existing document when the same bytes were already uploaded | 404, 413 `file_too_large`, 415 `unsupported_type`, 422 `empty_file`, 429 |
| GET | `/v1/documents/{doc_id}` | kb:read | all | → `DocumentOut` | 404 |
| GET | `/v1/documents/{doc_id}/chunks` | kb:read | all | `?cursor&limit` → Page `{id, ord, text, loc}` in reading order | 404, 422 |
| DELETE | `/v1/documents/{doc_id}` | documents:write | edit | → 204 (blob deleted when no other row uses it) | 404 |
| POST | `/v1/documents/{doc_id}/reprocess` | documents:write | edit | → 202 `DocumentOut` (queued) | 404 |

`KBOut` = `{id, name, settings, embedding_index_id, created_at}`.
`DocumentOut` = `{id, kb_id, source_id, name, mime, sha256, version, status, error, warnings, meta, created_at}`;
`status` ∈ `queued|processing|ready|failed|deleted`.

### Search and indexing

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/search` | search | all | `{query(1–2000), kb_ids?(1–50), k(1–50, 10), mode: hybrid\|vector\|keyword}` → `{results:[{chunk_id, document_id, document_name, kb_id, text, loc, score}]}` | 404 (a `kb_ids` entry not visible), 429, 502 `embedding_unavailable` |
| POST | `/v1/kbs/{kb_id}/reindex` | kb:write | edit | → 202 `{id, kb_id, provider, model, dim, version, status}` (new index; KB switches when built) | 404 |

`score` is cosine (vector mode), `ts_rank` (keyword mode), or the fused score 0–1.5 (hybrid).
Omitting `kb_ids` searches every KB of the org.

### Answers and conversations

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/answer` | answer | all | `{question(1–4000), kb_ids?(1–50), conversation_id?, k(1–30, 8)}` → `text/event-stream` | 402 `budget_exceeded`, 404 KB/conversation, 429, 503 `llm_not_configured` |
| GET | `/v1/conversations` | answer | all | `?cursor&limit` → `{items:[{id, title, kb_ids, created_at}], next_cursor}` (own only) | 422 |
| GET | `/v1/conversations/{conversation_id}` | answer | all | → conversation + `messages:[{id, role, content, citations, usage, created_at, abstained}]` | 404 |

#### `/v1/answer` SSE events

Order: `meta`, then `citation` and `token` interleaved, then `done`. `error` can end the stream at any
point. Each frame is `event: <name>\ndata: <json>\n\n`.

| Event | Data |
|---|---|
| `meta` | `{conversation_id, message_id}` |
| `citation` | `{n, chunk_id, document_id, document_name, kb_id, loc, excerpt(≤300 chars)}`. Sent before the token containing `[cN]` |
| `token` | `{text}`. Contains `[cN]` markers; markers that do not map to a retrieved chunk are removed |
| `done` | `{usage: {provider, model, in_tok, out_tok, cost_usd} \| null, abstained: bool, budget_warning?: true}` |
| `error` | `{code, message}`. Codes: `llm_*` (`llm_timeout`, `llm_unavailable`, `llm_auth_failed`, `llm_rate_limited`, `llm_model_unavailable`, `llm_bad_request`, `llm_refused`), `budget_exceeded`, `embedding_unavailable`, `internal_error` |

An abstained answer is the single token `I couldn't find this in your sources.` with no citations.
If the stream is cut (error or client disconnect), what was already sent is saved with
`usage.interrupted = true`.

### Sources and connectors

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| GET | `/v1/connectors` | sources:read | all | → `{items:[{type, title, description, category, auth, config_schema}]}` (JSON Schema per connector; only enabled ones) | |
| POST | `/v1/connectors/rest/openapi` | sources:write | edit | `{spec}` (OpenAPI 3/Swagger 2 as JSON or YAML text, ≤ 500,000 chars) → `{config, skipped:[{method, path, reason}]}`: a suggested `rest` source config (GET operations only; never calls the API) | 404 (REST connector off), 422 `invalid_spec` (not a spec, too large/deep, YAML alias, non-local `$ref`) |
| POST | `/v1/kbs/{kb_id}/sources` | sources:write | edit | `{type, name, config, secret?, schedule_minutes?(≥15)}` → 201 `SourceOut` | 404, 422 `unknown_connector` / `invalid_config` / `secret_not_allowed` / `oauth_only` / `private_endpoints_disabled` / `source_test_failed` |
| GET | `/v1/kbs/{kb_id}/sources` | sources:read | all | → `{items: SourceOut[]}` (max 500) | 404 |
| GET | `/v1/sources/{source_id}` | sources:read | all | → `SourceOut` | 404 |
| PATCH | `/v1/sources/{source_id}` | sources:write | edit | `{name?, config?, secret?, schedule_minutes?, enabled?}` → `SourceOut`. New config resets the cursor. Changing a connection-target field (Postgres `host`/`port`/`database`/`sslmode`, S3 `endpoint_url`/`region`/`bucket`, REST `base_url`/`auth`/`allow_private_endpoint`; any config field for a secret-bearing connector that declares none) needs `secret` in the same request (not when the source has no stored secret) | 404, 409 `sync_running`, 422 as for create + `secret_required_for_target_change` |
| DELETE | `/v1/sources/{source_id}` | sources:write | edit | → 204 (documents cascade; OAuth tokens revoked best-effort) | 404, 409 `sync_running` |
| POST | `/v1/sources/{source_id}/test` | sources:write | edit | → `{ok, message, schema: {tables:[{name, columns}]} \| null}` (schema for Postgres only) | 404 |
| POST | `/v1/sources/{source_id}/sync` | sources:write | edit | `?full=true` (ignore cursor; may delete) → 202 `RunOut` | 404, 409 `source_disabled` / `sync_running` |
| GET | `/v1/sources/{source_id}/runs` | sources:read | all | → `{items: RunOut[]}` (last 50) | 404 |
| POST | `/v1/kbs/{kb_id}/sources/oauth/start` | sources:write | edit | header `X-OAuth-Binding` (32–128 chars, per browser; the BFF sends its `rag_oauth` cookie) + `{type, name, config, schedule_minutes?}` → `{authorize_url}` | 404, 422 `unknown_connector` / `invalid_config` / `not_oauth` / `oauth_not_configured` / `oauth_binding_required`, 503 `oauth_unavailable` |
| POST | `/v1/sources/{source_id}/oauth/reconnect` | sources:write | edit | header `X-OAuth-Binding` → `{authorize_url}`; the finish call puts new tokens on this same source (documents kept) | 404, 422 `not_oauth` / `oauth_not_configured` / `oauth_binding_required`, 503 `oauth_unavailable` |
| POST | `/v1/connectors/oauth/finish` | sources:write | edit | header `X-OAuth-Binding` (same value as at start) + `{state, code?, error?}` from the provider redirect → `{redirect}` (frontend path, see below) | 400 `invalid_state` (unknown/reused/expired), 403 `oauth_not_yours` (another user, org or browser started it: state spent, code never exchanged) / `forbidden` |

`SourceOut` = `{id, kb_id, type, name, config, has_secret, status, last_sync_at, schedule_minutes, created_at,
last_error, needs_reconnect}` (the secret is never returned). `last_error` is the readable error of the last
failed or partial run (null after a clean run); `needs_reconnect` is true when that run failed because the OAuth
grant was revoked or expired. `RunOut` = `{id, source_id, status, started_at, finished_at, stats, error}`;
`status` ∈ `running|succeeded|failed|partial`.

The provider redirects to the web app page `${OAUTH_REDIRECT_BASE}/oauth/finish` (FX-1; there is no public API
callback any more), which posts code+state to `/v1/connectors/oauth/finish` as the signed-in user. Finish
`redirect` values: `/kbs/{kb}?tab=sources&connected={source_id}` (new source; its first
sync is queued), `...&reconnected={source_id}` (reconnect; clears `needs_reconnect`/`last_error`, sets an `error`
source back to `idle` and queues a sync only for a source that was in `error`; the old grant is not revoked, as
for Google that would revoke the new tokens too), or
`...&oauth_error=denied|exchange_failed|scope_denied|forbidden|unavailable|source_gone` (`source_gone`: the
reconnected source was deleted or no longer matches the flow's org/KB/type; refused flows revoke the new grant). The
finish page sends 400/403 refusals to `/integrations?oauth_error=<code>`.

### Agent

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/agent/runs` | agent:run | all | `{goal(1–4000), kb_ids?(1–50), max_steps(1–25, 12)}` → 202 `RunOut` | 402, 404 KB, 429 (`answer` bucket) |
| GET | `/v1/agent/runs` | agent:run | all | `?limit(1–200, 50)` → `{items: RunOut[]}` (own, newest first) | |
| GET | `/v1/agent/runs/{run_id}` | agent:run | all | → `RunOut` + `steps: StepOut[]` + `approvals: ApprovalOut[]` | 404 |
| GET | `/v1/agent/runs/{run_id}/events` | agent:run | all | `?after=n` → `text/event-stream` | 404 |
| POST | `/v1/agent/runs/{run_id}/cancel` | agent:run | all | → `RunOut` | 404, 409 `run_finished` |
| POST | `/v1/approvals/{approval_id}` | agent:run | all (run owner, or any owner/admin) | `{decision: approve\|reject}` → `ApprovalOut` | 404, 409 `already_decided` |
| GET | `/v1/reports/{report_id}` | agent:run | all (own runs) | → XLSX file, `Content-Disposition: attachment` | 404 |

- `RunOut` = `{id, goal, kb_ids, status, max_steps, result, error, created_at, finished_at}`; `status` ∈
  `planning|running|awaiting_approval|succeeded|failed|cancelled`. On success `result` =
  `{answer, citations:[{chunk_id, document_name, loc, document_id, kb_id, excerpt}], reports:[report_id]}`;
  on failure `{error_code}`.
- `StepOut` = `{id, n, kind(plan|message|tool_call|tool_result), tool, input, output, risk, started_at, finished_at, error, created_at}`.
- `ApprovalOut` = `{id, run_id, step_id, tool, input, status(pending|approved|rejected), decided_by, decided_at, created_at}`.
- Without a configured LLM, the run is accepted (202) and then fails with `result.error_code =
  "llm_not_configured"`.

#### Agent SSE events (`/events?after=n`)

The server polls the DB every second.

| Event | Data | When |
|---|---|---|
| `step` | `StepOut` | each step with `n > after` |
| `step_update` | `StepOut` | a step already sent changed (`started_at`, `finished_at`, `error`). Replace by `n`. On every (re)connect, tool_call steps with `n ≤ after` are re-sent once |
| `approval` | `ApprovalOut` | created (pending) and again when decided; all approvals re-sent once on (re)connect |
| `status` | `RunOut` | the run status changed (first poll always sends it) |
| `end` | `{status}` | run finished; stream closes |
| `timeout` | `{after}` | after 15 min; reconnect with `?after=` |

Duplicates are safe to apply again.

### API keys, audit, usage

| Method | Path | Scope | Roles | Body → Response | Errors |
|---|---|---|---|---|---|
| POST | `/v1/api-keys` | admin | admin | `{name(1–100), scopes[≥1], expires_at?}` → 201 `ApiKeyOut` + `key` (shown once) | 403 `insufficient_scope` (key minting wider key), 422 `invalid_expiry` |
| GET | `/v1/api-keys` | admin | admin | → `ApiKeyOut[]` (max 500) | |
| DELETE | `/v1/api-keys/{key_id}` | admin | admin | → 204 (immediate; idempotent) | 404 |
| GET | `/v1/audit` | admin | admin | `?cursor&limit&action` → `{items: AuditOut[], next_cursor}` | 422 |
| GET | `/v1/audit/export` | admin | admin | `?action` → `text/csv` (`audit-log.csv`) | |
| GET | `/v1/usage` | admin | all | `?from&to&group_by=day\|kind\|model\|user` → `{start, end, group_by, scope, totals, series}` | 422 `invalid_range` |
| GET | `/v1/usage/export` | admin | all | `?from&to` → `text/csv` (`usage.csv`) | 422 |

- `ApiKeyOut` = `{id, name, prefix, scopes, user_id, created_at, expires_at, last_used_at, revoked_at}`.
- `AuditOut` = `{id, action, actor_user_id, actor_email, actor_api_key_id, target_type, target_id, request_id, ip_hash, meta, created_at}`.
  Actions: `auth.register`, `auth.login`, `auth.logout_all`, `session.reuse_detected`, `invite.create`,
  `invite.accept`, `invite.decline`, `invite.revoke`, `member.add` (before 0010),
  `member.role_change`, `member.remove`, `org.settings_update`, `api_key.create`, `api_key.revoke`,
  `kb.create`, `kb.delete`, `document.upload`, `document.delete`, `source.create`, `source.update`,
  `source.delete`, `source.sync`, `agent_run.start`, `approval.approved`, `approval.rejected`.
- `actor_email` is the actor's email as it was when the row was written (0012; null for older rows),
  also a CSV column after `actor_user_id`. It stays after the member leaves the org.
- Usage `kind`: `chat` (answers), `rewrite` (follow-up rewrites), `embed`, `agent` (agent model turns;
  `chat` before 0012).
- Usage: `from`/`to` need a timezone, `to` is exclusive, span ≤ 366 days, default = this UTC month.
  Owners/admins get `scope: "org"`; other roles get `scope: "self"` (own rows only). `totals` and each
  `series` bucket = `{in_tok, out_tok, cost_usd, calls, unpriced_calls}`; buckets add `key` and `label`
  (email for `group_by=user`).
- CSV cells starting with `= + - @`, tab or CR get a `'` prefix (formula injection).

### Ops

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/healthz` | public | `{"status":"ok"}` |
| GET | `/readyz` | public | `{"status":"ready"}` after `SELECT 1`; 500 if the DB is down |
