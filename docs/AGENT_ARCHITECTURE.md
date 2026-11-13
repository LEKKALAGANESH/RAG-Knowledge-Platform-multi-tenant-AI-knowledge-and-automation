# Agent architecture

Code: `backend/app/agent/` (`loop.py`, `tools.py`, `numbers.py`, `tasks.py`, `router.py`, `models.py`).
Status: implemented, not fully tested. Every path is tested with a scripted model; **no live LLM has
run the agent (blocked by credentials)**.

## Shape

One orchestrator, all state in Postgres. The Dramatiq actor `run_agent` (queue `agent`, no retries,
1 h limit) calls `loop.advance`, which:

1. Rebuilds the transcript from `agent_steps`.
2. Finishes tool calls the model already asked for.
3. Asks the model for its next turn (`tool_turn` on the configured provider).
4. Stops when the run ends or a tool needs approval.

A decision on an approval sends `run_agent` again, which resumes from the DB.

## Tools (`tools.py`)

The tool list and their risk classes are constants in code. The model cannot add tools or change a
risk class.

| Tool | Risk | Does |
|---|---|---|
| `search_knowledge` | read | Hybrid search over the run's KBs |
| `read_chunks` | read | Re-reads chunks by id; only ids already seen in this run |
| `calculate` | read | Exact Decimal arithmetic via an AST whitelist (`+ - * / ( )`, `sum/min/max/abs/round`); no `eval`, no floats. Bounded work: ≤ 400 AST nodes, literals ≤ 50 digits and ≤ 1e100, 28-digit context with exponents ±1000 (overflow = tool error), `round` places ≤ 28 |
| `extract_table` | read | Parses invoice fields (number, vendor, category, date, currency, amount) from chunks; flags duplicates. Linear-time regexes (no catastrophic backtracking) |
| `create_xlsx_report` | reversible_write | Builds an XLSX (sheets + a Sources sheet) and stores it as a report |
| `delete_report` | irreversible_write | Deletes a report this run created |

## Safeguards

- **Approvals:** `irreversible_write` tools always pause the run (`awaiting_approval`) and create a
  `pending` approval. `reversible_write` tools also pause when the org setting
  `settings.agent_approve_reversible_writes` is true (no API sets it yet; it is a DB-level setting).
  Only a person decides, through `POST /v1/approvals/{id}`: the run's owner or an org owner/admin. The
  pending → decided change is one atomic UPDATE (second click → 409). A rejected call returns
  "rejected" to the model.
- **Exactly once:** a tool call is claimed with `UPDATE … SET started_at WHERE started_at IS NULL`
  before it runs. A claimed call with no result is reported as "interrupted, not retried" and never
  re-run. Unique keys `(run_id, n)` and `(run_id, kind, tool_use_id)` stop a second worker from
  inserting the same step.
- **Prompt injection:** tool results reach the model only wrapped as `{"untrusted_data": ...}`; the
  system prompt says such text is never an instruction. The tool list and system prompt are identical
  on every turn. Unknown tools and extra arguments are rejected as errors (AC10 test with a "rogue"
  scripted model).
- **Limits:** `max_steps` (default 12, max 25) counts tool calls, including invalid ones; 400k
  input+output tokens per run; 16k tokens per turn. A `max_tokens` stop or a refusal fails the run with
  a readable error. Budget is checked before every model turn.
- **CPU bounds:** `calculate` and `extract_table` run in C code that holds the GIL, so no timeout can
  interrupt them; the input bounds above keep each call to milliseconds instead (200k-char
  pathological text < 0.3 s).
- **Event stream:** `GET /v1/agent/runs/{id}/events` opens a short DB session per 1 s poll and yields
  only after closing it, so an open stream holds no pooled connection.
- **Status changes** are atomic `UPDATE … WHERE status IN (active)`, so a cancel during a model turn
  is never overwritten.
- **Reports:** the report name is ASCII-only (it goes into `Content-Disposition`). Every cell, header
  row and Sources sheet included, starting with `= + - @`, tab or CR is stored as a quote-prefixed
  string, so document text cannot become a formula. Money stays as decimal
  values; currencies are never converted.

## Tenancy

`agent_runs` are private to their author (RLS `org_id AND user_id`). Steps and reports follow their
run. Approvals are org-wide in RLS so admins can decide; the API still checks owner-or-admin. The
worker acts as the run's owner (`approvals.user_id`).

## Result and citations

On success `result = {answer, citations, reports}`. The answer keeps `[chunk:<id>]` markers only for
chunks the run actually saw; each citation carries `chunk_id, document_name, loc, document_id, kb_id,
excerpt`. Every model turn writes a `usage_events` row (`kind=chat`).

## Live updates

`GET /v1/agent/runs/{id}/events?after=n` polls every 1 s and streams `step`, `step_update`,
`approval`, `status`, `end`, `timeout`. Contract: [API_DOCUMENTATION.md](API_DOCUMENTATION.md#agent-sse-events-eventsaftern).

## Known gaps

- No client-side provider fallback chain for the agent (uses the primary model; Anthropic server-side
  fallback only).
- SSE uses polling; LISTEN/NOTIFY would scale better with many viewers.
- `GET /v1/agent/runs/{id}` returns full tool outputs (chunk texts); large runs should paginate.
- Agent model turns are recorded as usage kind `chat` (a separate `agent` kind is planned).

## Trying it without an LLM key

`cd backend && RAG_TEST_DB=rag_test_a uv run python -m tests.test_agent serve [--port 8079]` seeds a
user and 6 invoice documents in the **test** DB, prints the login, and serves the real app with a
scripted model and an in-process worker. Test use only.
