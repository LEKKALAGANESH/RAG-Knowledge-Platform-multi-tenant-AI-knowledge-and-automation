# RAG architecture

Code: `backend/app/retrieval/` (search), `backend/app/rag/` (answer flow), `backend/app/llm/`
(providers), `backend/evals/` (evaluation).

## Retrieval (`retrieval/search.py`)

Three modes on `POST /v1/search`; `/v1/answer` always uses `hybrid`.

- **Vector:** for each KB, its active embedding index is queried with that index's own model (KBs on
  the same model are queried together). pgvector cosine distance over a per-index partial HNSW index
  (`embedding::vector(dim)`). With `VECTOR_STORE=qdrant` the same calls go to Qdrant instead
  (`retrieval/qdrant_store.py`, ADR 0010): one cosine collection `rag_<index id>` per index version,
  points hold only ids (`org_id`, `kb_id`, `document_id` payload), and every query is filtered by
  the session's RLS org plus the index's KB. Chunk text is still hydrated from Postgres under RLS.
  Seed eval parity (k=5, same corpus): hybrid R@5 0.974 / MRR 0.942 and vector 0.953 / 0.938 on
  both stores.
- **Keyword:** Postgres FTS on `chunks.tsv` (`simple` config, so any language works). The query's
  content words are OR-ed: words of ≤ 2 letters are dropped unless they hold a digit, and a short
  shared English/German/Spanish stopword list is removed. A query with `"quoted phrases"` keeps strict
  `websearch_to_tsquery` syntax. Ranked by `ts_rank`.
- **Hybrid:** relative score fusion. Each list is min-max scaled to [0, 1], then summed with weights
  (each vector list 1.0, keyword 0.5). Fused score range 0–1.5.

Each ranker returns `min(max(3k, 20), 150)` candidates. Hits are hydrated with document names and
filtered to `ready` documents. RLS keeps everything inside the caller's org; `visible_kbs` returns
404 for any requested KB the caller cannot see.

### How fusion was chosen

Seed eval, 47 items, k = 5, real MiniLM embeddings and Postgres ([ADR 0003](adr/0003-relative-score-fusion.md)):

| Step | Hybrid R@5 / MRR / nDCG@5 | Vector R@5 / MRR | Keyword R@5 / MRR |
|---|---|---|---|
| Before (keyword AND-ed every word) | 0.953 / 0.938 / 0.921 | 0.953 / 0.938 | 0.000 / 0.000 |
| Keyword OR fix + RRF (k = 60) | 0.923 / 0.865 / 0.872 | 0.953 / 0.938 | 0.812 / 0.808 |
| Keyword OR fix + relative score fusion, keyword weight 0.5 (**current**) | **0.974 / 0.942 / 0.936** | 0.953 / 0.938 | 0.812 / 0.821 |

RRF lost to vector-only at every setting tried (keyword weight 1 / 0.5 / 0.3 × k 60 / 20; best
0.940 / 0.871). On cross-language items a weak keyword match that also sat in the vector top-20
outranked the vector's rank-1 hit. Relative score fusion beat vector-only at every weight
(1.0 → 0.974 / 0.906, 0.5 → 0.974 / 0.942, 0.33 → 0.953 / 0.968); 0.5 wins on both metrics.
A per-document cap of 2 or 3 hits changed nothing and was reverted.

### Reranker (`retrieval/rerank.py`, off by default)

`RERANK_PROVIDER=local|cohere` reranks the first `RERANK_TOP_N` (20) fused candidates of a hybrid or
vector search with a cross-encoder, before the k cut; keyword mode is never reranked. A reranked
hit's `score` is the reranker's score; its `cosine` is kept, so the `/v1/answer` similarity gate is
unchanged. Any reranker error (model load, HTTP, wrong score count) logs a warning and returns the
fused order: reranking never fails a search. The session commits before the model call, so no
pooled DB connection is held while it runs (the query embedding does the same: budget check,
commit, embed, usage row in its own short session).

- `local`: fastembed ONNX `jinaai/jina-reranker-v2-base-multilingual` (multilingual, CPU, no new
  dependency). `BAAI/bge-reranker-base` is English/Chinese only; `ms-marco-MiniLM` is English only
  (scored a German query against the German passage at -11); `bge-reranker-v2-m3-int8` was slower.
- `cohere`: `POST /v2/rerank`, contract-tested only. It writes no usage row and skips the budget
  check (`usage_events.kind` has no `rerank`; needs a migration), so keep it off until then.

Not the default: on this 8-core dev CPU, 20 candidates of ~400 tokens took a p50 of ~25 s (jina),
~34 s (bge-m3 int8) and ~8 s (English-only MiniLM-L12) per query, against a 300 ms budget. The
before/after eval with reranking did not complete (the host killed it for low memory), so there are
no quality numbers yet. Baseline with the latency columns the runner now prints:
hybrid R@5 0.974 / MRR 0.942, p50 187 ms / p95 337 ms per search (query embedding included).

## Answer flow (`rag/answer.py`)

1. Route checks: scope `answer`, rate limit, budget (402), LLM configured (503).
2. Conversation (new or own) and user message are committed; SSE `meta` is sent.
3. If the conversation has history (last 6 messages), the follow-up is rewritten into a standalone
   query by the rewrite model (`LLM_REWRITE_MODEL`, default `claude-haiku-5-5` for Anthropic). A
   failed rewrite falls back to the raw question.
4. Hybrid search with `k` (default 8).
5. **Similarity gate:** if every hit that has a cosine score is below the model's cut, the answer
   abstains without calling the LLM. Keyword-only hits never gate.
6. Context packing: best hits that fit the model's context window (≈4 chars per token).
7. Generation: sources are HTML-escaped and fenced in `<sources>` as untrusted data; the system
   prompt requires `[cN]` markers and the exact abstain sentence. The stream goes through
   `CitationFilter`: a marker is emitted only if `N` is in the packed context, its `citation` event is
   sent before the token, and unknown markers are stripped (AC5).
8. Persist the assistant message (`citations`, `usage` with `abstained`), one usage row per model
   call (chat, rewrite), then `done`.

The answer flow commits (ends its read transaction) before the rewrite and answer model calls, so a
long generation holds no pooled DB connection; persistence runs in its own short transaction.
Interrupted streams (error, client disconnect) still save what was sent (`usage.interrupted = true`)
and the billed usage.

### How the gate was chosen

`MIN_COSINE` in `rag/answer.py`: MiniLM = 0.32; untuned models = 0.0 (gate off rather than guess a
scale). On the seed set the lowest answerable top cosine is 0.326 and the highest unanswerable is
0.386, so the ranges overlap. 0.39 scores best on abstention (0.957 vs 0.936) but would abstain on an
answerable item ("Do you ship to Switzerland?", 0.326). 0.32 is the highest cut that loses no
answerable item; it drops 5 of 8 unanswerable items before the LLM, and the prompt's abstain rule
covers the rest. This is tuned on the data it is scored on; grow the eval set before trusting it more.

## LLM providers (`llm/`)

- `AnthropicProvider` (default model `claude-sonnet-5-5`; server-side fallback for supported models)
  and `OpenAICompatProvider` (OpenAI, Ollama, vLLM, LM Studio, Azure via `LLM_BASE_URL`).
- `LLM_FALLBACK_MODEL`: client-side fallback, used only on a retryable error before any text streamed.
- SDK errors map to readable codes (`llm_timeout`, `llm_rate_limited`, …); raw provider text is
  logged, never returned.
- `llm/registry.py`: context window, max output and price per model. Unknown models get 8k / 1k
  limits and `cost_usd = NULL` (shown as "unpriced"). fastembed costs 0.

## Evaluation (`backend/evals/`)

```bash
cd backend
RAG_TEST_DB=rag_test_a uv run python -m evals.run_eval --set seed --mode all
```

Builds two throwaway orgs in the test DB, ingests `evals/corpus/` through the real pipeline, runs the
47 seed questions (single-doc, multi-doc, table, cross-language, ambiguous, unanswerable, injection,
…) and prints Recall@K, MRR, nDCG, precision, cross-org leak count and an abstention sweep. Then it
deletes both orgs. Options: `--mode hybrid|vector|keyword|all`, `--k`, `--abstain-cosine`, `--json`.

| Metric | Status |
|---|---|
| Retrieval (AC4): R@5 0.974, MRR 0.942, leak 0 | implemented+tested |
| Answer-level: citation correctness, abstention rate, faithfulness, first-token latency (AC5, AC6) | blocked by credentials: the runner prints SKIPPED without an LLM and never invents a number |

Known weak spots: ambiguous queries, a 3-part multi-question item, and short table questions (table
rows rank below prose). A cross-encoder reranker exists but is off (CPU latency, see above).
Planned: query decomposition, a bigger eval set.
