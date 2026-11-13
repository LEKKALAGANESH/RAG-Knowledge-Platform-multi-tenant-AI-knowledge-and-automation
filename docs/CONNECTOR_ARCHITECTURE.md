# Connector architecture

Code: `backend/app/connectors/`. A connector only reads its source; one generic engine (`sync.py`)
does all DB work.

## Contract (`base.py`)

```python
class Connector(Protocol):
    meta: ConnectorMeta          # type, title, description, category, auth, config_schema (JSON Schema)
    def validate_config(self, raw) -> Config
    async def test(self, config, secret) -> str | Checked        # Checked.tables for databases
    def discover(self, config, secret, cursor) -> AsyncIterator[Item]
```

`Item` = `external_key`, `name`, `fetch()` (bytes, called lazily), `mime_hint`, `content_hash`
(change marker), `meta`. Rules:

- `discover` may update `cursor` in place; the engine saves it only after a successful run.
- An empty cursor (first run, `?full=true`, config changed) means a full view.
- `cursor["incremental"] = True` marks a run that yields only changed items; absent items are then
  not deleted.
- `ConnectorError` messages are shown to users and must not contain secrets.

The registry (`registry.py`) lists only connectors that work. Credential-gated ones join only when
their flag is on.

## Sync engine (`sync.py`)

1. A run row (`sync_runs`, status `running`) is created by the API (`POST /sources/{id}/sync`) or the
   scheduler. A partial unique index allows one running run per source.
2. Actor `sync_source` (queue `sync`, no retries, 4 h time limit) marks the source `syncing` and
   iterates `discover`.
3. Per item: same `content_hash` as the stored document → `unchanged`, no fetch. Otherwise fetch,
   store the blob, create a new document version keyed by `(source_id, external_key)` and run the
   normal ingestion pipeline on it.
4. Deletion: documents missing from the run are deleted **only** when the run saw everything (no
   failed item, not incremental). A partial view never deletes (AC7).
5. Run ends `succeeded`, `partial` (some items failed; nothing deleted) or `failed`. The source goes
   back to `idle` (or `error` after a failure; `disabled` stays).

Stale runs (worker killed) are failed after the time limit + 10 min by `reap_stale_runs`, which runs
on every manual sync and every scheduler tick.

## Scheduler (`scheduler.py`)

`python -m app.connectors.scheduler [--interval 60]`. Each tick calls the SECURITY DEFINER function
`due_sync_orgs(stale_after)` (org ids only), then per org in an RLS session: reap stale runs, claim
due `idle` sources with a conditional `UPDATE … status='syncing'`, insert runs, enqueue. Due =
`schedule_minutes` set and `last_sync_at` (or `created_at`) + interval ≤ now. `disabled` and `error`
sources are not scheduled. Several scheduler copies are safe. One failing org does not stop the others.

## Connectors

| Type | Auth | Status | Change marker | Deletes |
|---|---|---|---|---|
| `website` | none | implemented+tested (live crawl of peps.python.org) | hash of the page body (`_page_hash`) | full run |
| `s3` | api_key (`{access_key_id, secret_access_key}`) | implemented+tested (live MinIO) | `etag:<etag>:<size>` from the listing (no GET when unchanged) | full run |
| `postgres` | basic (`{user, password}`) | implemented+tested (live Postgres) | sha256 of the rendered row document | full run (every `full_sync_every`-th, default 12) |
| `gdrive` | oauth2 | implemented, not fully tested. **Live blocked by credentials.** Flag `CONNECTOR_GDRIVE_ENABLED` (off; FX-1 browser binding is in, so it may be turned on once an OAuth app exists) | `md5:<md5Checksum>` or `v:<version>:<modifiedTime>` | full run |
| `onedrive` | oauth2 | partial: in progress (task A7b), not verified. Flag `CONNECTOR_ONEDRIVE_ENABLED` (off) | from Graph metadata | full run |
| `dropbox` | oauth2 | partial: in progress (task A7b), not verified. Flag `CONNECTOR_DROPBOX_ENABLED` (off) | `dbx:<content_hash>` (or `rev:<rev>`) | full run |
| `rest` | per config: none / api_key / bearer / basic / oauth2 client-credentials (secret) | implemented+tested (MockTransport suite; live 1 page of a public placeholder API). Flag `CONNECTOR_REST_ENABLED` (on) | sha256 of the rendered record document | full run (every `full_sync_every`-th, default 12) |

### Website (`website.py`)

Config: `start_urls` (1–20), `sitemap_url`, `max_depth` (0–5, default 2), `max_pages` (≤ 2000,
default 100), `allowed_domains`, `include_patterns`, `exclude_patterns`, `rate_limit_rps` (≤ 5,
default 1), `allow_nonstandard_ports`.

- robots.txt is honored per origin, including `Crawl-delay`. An unreachable robots.txt, or one that
  answers 401/403/5xx, is treated as "disallow everything".
- Meta `noindex` skips a page; `nofollow` stops link following.
- 401/403/404/410/451 pages count as absent: logins are never bypassed.
- Every request goes through `SafeFetcher` (see SSRF below). 5 MB body cap, 15 s timeout.

### S3-compatible (`s3.py`)

AWS S3, R2, B2, MinIO, GCS interop. Config: `bucket`, `prefix`, `region`, `endpoint_url`,
`include_extensions`, `max_objects` (≤ 50,000), `allow_private_endpoint`. Listing is paginated; GET
uses `If-Match` on the listed ETag and a size cap of `MAX_UPLOAD_MB`. Only the configured keys are
used, never the server's own IAM identity.

### PostgreSQL (`postgres.py`)

Turns rows of configured tables into one markdown document per row. Config: `host`, `port`,
`database`, `schema`, `tables[]` (`name`, `id_column`, `text_columns`, `title_column`,
`updated_column`), `sslmode` (`disable|require|verify-full`), `max_rows_per_table`,
`full_sync_every`, `allow_private_endpoint`.

- No user SQL ever. Identifiers must match a strict pattern, exist in the role's catalog, and are
  always quoted.
- Session: `default_transaction_read_only=on`, `statement_timeout` 15 s, `idle_in_transaction` 60 s,
  `lock_timeout` 5 s.
- A superuser, or a role with INSERT/UPDATE/DELETE/TRUNCATE on a configured table, is refused at
  save time (`test()` runs before saving) and at every sync.
- Incremental with `updated_column`: rows with `updated >= watermark`; no deletes on those runs.
- `test()` returns the readable tables and columns (schema browser in the UI).

### Generic REST (`rest.py`, `openapi.py`) (C2)

One markdown document per JSON record from configured GET endpoints. Config: `base_url` (https; http
only with `allow_private_endpoint`), `endpoints[]` (`path`, `method` GET only, `query{}`,
`items_path` = dotted keys + `[*]`, `id_field`, `title_field`, `text_fields[]`, `updated_field`,
`since_param`), `pagination` (`none|page|offset|cursor|link_header`, param names, `page_size` ≤ 1000,
`max_pages` ≤ 1000), `headers{}` (non-secret; Authorization/Cookie/Host etc. refused), `auth`
(`kind`, `api_key_in` header|query, `api_key_name`, `token_url`, `scope`, `client_auth`),
`rate_limit_rps` (≤ 20, default 2), `max_items` (≤ 50,000), `full_sync_every`,
`allow_nonstandard_ports`, `allow_private_endpoint`.

- Secret by auth kind: api_key/bearer = the key/token as text; basic = `{username, password}`;
  oauth2 = `{client_id, client_secret}` (token cached in memory per process, renewed on 401).
- External key `<path>[?query]#<id>`; meta `{record, endpoint}`. Records without a usable id are skipped.
- Every request (API and token URL) goes through the SSRF guard; redirects are refused, not followed.
  Next links (Link header, or a URL in the cursor field) must be on base_url's origin. A repeated
  page URL or identical page body fails the run (loop). 429/503: `Retry-After` honored up to 60 s,
  3 retries. Response cap 5 MB, 15 s timeout.
- Incremental with `updated_field`: records older than the watermark are skipped (`since_param` sends
  it), no deletes on those runs.
- S1 `target_fields`: `base_url`, `auth`, `allow_private_endpoint`.
- `POST /v1/connectors/rest/openapi` turns an OpenAPI 3 / Swagger 2 spec (JSON or YAML, ≤ 500,000
  chars, ≤ 100,000 nodes, depth ≤ 64; YAML safe loader without aliases; only local `$ref`) into a
  suggested config. GET operations without path params only; the API is never called.
- Follow-up: GraphQL (single read-only query via POST), webhooks.

### OAuth connectors (`oauth.py`, `gdrive.py`, `onedrive.py`, `dropbox.py`)

- Authorization code + PKCE (S256). `start()` stores the pending source, the verifier and a hash of
  the browser binding (`X-OAuth-Binding`, from the BFF's httpOnly `rag_oauth` cookie) in Redis under a
  random single-use `state` (10 min). Redirect URI = `${OAUTH_REDIRECT_BASE}/oauth/finish`, a web app
  page (FX-1: `OAUTH_REDIRECT_BASE` is now the public **web app** URL, default `http://localhost:3000`;
  register that exact URI with each provider). The API has no public callback.
- The finish page posts code+state to `POST /v1/connectors/oauth/finish` (editor+). It consumes the
  state atomically (`GETDEL`; unknown, reused or expired → 400 `invalid_state`), then requires the same
  user, org and browser binding as the start (else 403 `oauth_not_yours`, code never exchanged: a
  victim opening an attacker's authorize link cannot link their drive into the attacker's org, S2).
  Then the KB must still exist and the source is created. Tokens are stored only in `sources.secret_enc` (Fernet JSON with
  `source_id` and `org_id` bound in). Manual secrets are refused for oauth2 types (`422 oauth_only`).
- Tokens refresh when < 60 s remain and once after a 401. `invalid_grant` fails the run with a
  "reconnect" message and sets the source to `error`.
- Deleting a source revokes the token (best effort).
- **ACLs are not enforced per user** for Drive, OneDrive or Dropbox: anyone with access to the KB can
  retrieve every synced file. Sharing info is kept in `documents.meta` only.
- Google Drive: scope `drive.readonly` (restricted scope: a public launch needs Google verification).
  Exports Docs → DOCX, Sheets → XLSX, Slides → text. Native files > 10 MB are skipped.
  Incremental runs use `changes.list`; removed files are deleted on the next full run.

## SSRF and private endpoints (`ssrf.py`)

- Every hop (first request and each redirect): http/https only, no userinfo, ports 80/443 unless the
  source opts in, no numeric host tricks (`0x7f.1`, decimal IPs).
- Every DNS answer must be public unicast; one bad answer refuses the host (rebinding tell). The
  request then connects to the checked IP with the real Host header and TLS SNI (pinning), so a
  second lookup cannot swap in a private address. Environment proxies are ignored.
- S3 (boto3) cannot pin without breaking TLS, so its custom endpoints check the **connected socket's
  peer address** on every new connection (S13): a DNS answer that flips after the first check is
  refused before any HTTP is sent, with Host/SNI unchanged.
- IPv4 hidden in IPv6 (mapped, 6to4, Teredo, NAT64) is unwrapped and checked.
- **Private endpoints** (S3/Postgres inside a customer network): a source sets
  `allow_private_endpoint`, and it only works while the server gate
  `CONNECTORS_ALLOW_PRIVATE_ENDPOINTS=true` is on (re-read on every test and sync). Even then only
  RFC1918 and IPv6 ULA ranges are added; loopback, link-local, cloud metadata, CGNAT, multicast and
  reserved stay blocked ([ADR 0007](adr/0007-private-endpoint-gate.md)).

## Adding a connector

1. Implement the protocol in `app/connectors/<type>.py` with a pydantic config model.
2. Register it in `registry.py`. Put credential-gated ones behind a `CONNECTOR_<TYPE>_ENABLED` flag.
3. Use `SafeFetcher` / `resolve_checked` for every outbound connection.
4. Contract tests against recorded responses; live tests marked `@pytest.mark.live`.
