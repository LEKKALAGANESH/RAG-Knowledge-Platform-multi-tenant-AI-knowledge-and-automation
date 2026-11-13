# SDK and integration examples

- **Python SDK**: `sdk/python`, package `rag_platform` (httpx, Python >= 3.10, typed, `py.typed`).
- **TypeScript**: `examples/typescript`, a copy-in typed `fetch` client plus three runnable scripts.
  There is no npm package.

Both talk to the same `/v1` API described in [API_DOCUMENTATION.md](API_DOCUMENTATION.md).

## Install (Python)

```bash
pip install -e sdk/python          # or: uv pip install -e sdk/python
```

## Authentication

Use an **API key** for scripts and services. Create one in the app (Settings, API keys) or with the
SDK while signed in as an owner/admin. The key is shown once; keep it in a secret store or env var.

```python
import os
from rag_platform import Client

client = Client("https://rag.example.com", api_key=os.environ["RAG_API_KEY"])
```

A key acts as its creator's current role in the key's org, limited to its scopes:
`kb:read`, `kb:write`, `documents:write`, `search`, `answer`, `agent:run`, `sources:read`,
`sources:write`, `admin` (`admin` does not imply the others). Give each key only what it needs.

Password login (JWT, 15 minutes, no auto-refresh) is for tooling and tests; use it to mint a key:

```python
client = Client("http://localhost:8000")
client.login("owner@example.com", "...")
key = client.api_keys.create("ci-search", ["search", "answer", "kb:read"])
client.set_credentials(key["key"])   # all later calls use the key
```

The SDK never logs, and `repr(client)` does not include the credential.

## Examples (Python)

```python
kb = client.kbs.create("Handbook")
doc = client.documents.upload(kb["id"], "handbook.pdf")        # path, bytes (+filename) or file object
client.documents.wait_until_ready(doc["id"], timeout=300)

hits = client.search("refund policy", kb_ids=[kb["id"]], k=5)   # mode: hybrid | vector | keyword

answer = client.answer.ask("How long do refunds take?", kb_ids=[kb["id"]])
print(answer.text)
for c in answer.citations:
    print(c.n, c.document_name, c.loc, c.excerpt)
```

### Streaming answers

`answer.stream()` yields typed events: `Meta`, then `Citation` and `Token` interleaved, then `Done`.
A `StreamError` can end the stream at any point (`llm_timeout`, `budget_exceeded`, ...).

```python
from rag_platform import Citation, Done, StreamError, Token

for e in client.answer.stream("How long do refunds take?", kb_ids=[kb_id]):
    if isinstance(e, Token):
        print(e.text, end="", flush=True)
    elif isinstance(e, Citation):
        sources[e.n] = e
    elif isinstance(e, StreamError):
        raise SystemExit(f"stopped: {e.code}")
    elif isinstance(e, Done) and e.abstained:
        print("(not found in your sources)")
```

Follow-up questions: pass `conversation_id=answer.conversation_id`.
`client.conversations.list()` / `.get(id)` read the stored history.

### Agent runs, approvals, reports

```python
run = client.agent.create("Compare 2025 invoice totals with 2024", kb_ids=[kb_id])
for e in client.agent.events(run["id"]):
    if e.event == "step":
        print(e.data["n"], e.data["kind"], e.data["tool"])
    elif e.event == "approval" and e.data["status"] == "pending":
        client.approvals.decide(e.data["id"], "approve")      # or "reject"
done = client.agent.get(run["id"])
for report_id in (done["result"] or {}).get("reports", []):
    open(f"{report_id}.xlsx", "wb").write(client.reports.download(report_id))
```

`agent.events()` reconnects by itself with `?after=<last step n>`: on the server's 15-minute
`timeout` event and on a dropped connection (up to `max_reconnects`, default 5, in a row; then
`RagError("stream_lost")`). Events after a reconnect can repeat: apply `step_update` by `n` and
`approval` by `id`. To resume later yourself, call `agent.events(run_id, after=n)`.
`agent.cancel(run_id)` stops a run; `agent.list()` lists your runs.

### Sources and connectors

```python
client.sources.connectors()              # enabled types + JSON Schema of each config

# Generic REST API connector. The secret (here a bearer token) is stored encrypted, never returned.
src = client.sources.create(
    kb_id, "rest", "Help center",
    config={
        "base_url": "https://api.example.com/v1",
        "endpoints": [{"path": "/articles", "items_path": "data[*]", "id_field": "id",
                       "title_field": "title", "text_fields": ["body"],
                       "updated_field": "updated_at"}],
        "pagination": {"type": "cursor", "cursor_param": "cursor", "cursor_path": "next"},
        "auth": {"kind": "bearer"},
    },
    secret=os.environ["HELP_CENTER_TOKEN"],
    schedule_minutes=60,
)
# Or start from an OpenAPI spec: client.sources.suggest_rest_config(spec_text)["config"]
client.sources.test(src["id"])           # {ok, message, schema}
client.sources.sync(src["id"])           # 202 run; full=True ignores the cursor
client.sources.runs(src["id"])           # last 50 runs
client.sources.update(src["id"], enabled=False)
```

OAuth connectors (Google Drive, OneDrive, Dropbox) need the browser flow in the web app; they cannot
be created from the SDK.

### Anything else

`client.request(method, path, params=..., json_body=...)` calls any route without a helper
(orgs, members, audit, usage) with the same auth, errors and retries.
`client.stream(method, path, ...)` opens any SSE route and yields `ServerEvent(event, data)`.

## Errors

Every non-2xx response raises `RagError(code, message, request_id, status)`, or a subclass:

| Status | Class | Typical `code` |
|---|---|---|
| 401 | `AuthenticationError` | `invalid_api_key`, `invalid_token`, `not_authenticated` |
| 402 | `BudgetExceededError` | `budget_exceeded` |
| 403 | `PermissionDeniedError` | `forbidden`, `insufficient_scope` |
| 404 | `NotFoundError` | `not_found` (also another org's ids) |
| 409 | `ConflictError` | `sync_running`, `already_decided`, `run_finished` |
| 422 | `ValidationError` | `validation_error`, `invalid_cursor`, `invalid_config` |
| 429 | `RateLimitError` (`.retry_after` seconds) | `rate_limited`, `too_many_streams` |

Quote `request_id` when reporting a problem; it matches the server log.

```python
from rag_platform import RateLimitError, RagError

try:
    client.search("q")
except RateLimitError as e:
    time.sleep(e.retry_after or 1)
except RagError as e:
    log.warning("search failed: %s (request %s)", e.code, e.request_id)
```

## Retries and timeouts

- `Client(timeout=30.0, max_retries=2)`. Only **GET** requests that get **429 or 503** are retried,
  waiting `Retry-After` (capped at 60 s) or 0.5 s, 1 s, 2 s ... when the header is missing.
- POST/PATCH/DELETE are never retried automatically (an upload or answer could run twice); catch
  `RateLimitError` and decide yourself.
- Pagination is keyset: pass `next_cursor` back as `cursor` until it is `None`.
- The client is sync. From async code, wrap calls in `asyncio.to_thread(...)`.

## TypeScript

```bash
cd examples/typescript
npm install && npm run typecheck                    # tsc strict
export RAG_BASE_URL=http://localhost:8000 RAG_API_KEY=rag_...
node src/search.ts "refund policy" <kb_id>                         # scope: search
node src/upload-and-ask.ts <kb_id> "How long do refunds take?" handbook.pdf   # documents:write, kb:read, answer
node src/agent-run-with-approval.ts <kb_id> "Compare 2025 invoices with 2024" # agent:run
```

Node >= 22.18 runs the `.ts` files directly (type stripping). `src/client.ts` is the part to copy:
a `Rag` class over `fetch`, a `RagError` with `status/code/requestId/retryAfter`, and `sse()`, which
parses `event:`/`data:` frames from a `ReadableStream` (chunks may split lines anywhere). In a browser,
call your own backend instead; never ship an API key to the client.

## Testing the SDK

```bash
cd sdk/python
uv run ruff check . && uv run mypy src
uv run pytest -q          # unit tests (MockTransport) + integration
```

The integration test starts `tests.test_agent serve` (free port) and `tests.test_rag serve` (port 8079)
from `backend/` against `RAG_TEST_DB` (default `rag_test_b`) with the scripted model, then runs an
answer stream, an agent run with an approval and a report download through the SDK. It is skipped
when the helpers cannot start (no Postgres/Redis, port 8079 busy).
