# ADR 0010: Optional Qdrant vector store behind the same store contract

**Status:** accepted (Q1). Extends ADR 0002; pgvector stays the default.

## Context

The spec (§6, §10) asks for provider-independent vector storage, kept apart from the relational
data, and lets BYOC customers bring their own Qdrant. Until Q1 every vector lived in
`chunk_embeddings`, guarded by Postgres RLS. Qdrant has no RLS, so moving vectors there could
weaken tenant isolation (AC1) unless the store enforces it itself.

## Decision

- `retrieval/vectorstore.py` defines a `VectorStore` Protocol (`create_index`, `drop_index`,
  `upsert`, `delete_documents`, `search`). `PgVectorStore` and `QdrantVectorStore` implement it.
  `vector_store(session)` picks one from `VECTOR_STORE=pgvector|qdrant` (default `pgvector`).
- **One collection per embedding index version** (`rag_<index id hex>`, cosine, dim from the
  index). A reindex builds a new collection beside the old one and drops the old one on switch, so
  dims are never mixed (AC8). Retire and KB delete drop the collection.
- **Ids only in Qdrant.** Point id = chunk id. Payload = `org_id`, `kb_id`, `document_id`, with
  keyword payload indexes. Chunk text stays in Postgres and is hydrated there under RLS, so RLS
  still decides what a caller receives.
- **Tenant enforcement in the store.** The org comes from the session's RLS tenant
  (`session.info["tenant"]`), never from a caller argument. Every search and delete filter is
  `org_id = tenant org AND kb_id = index.kb_id [AND ...]`, built in one place (`_scope`). A session
  without a tenant, or an index row of another org, raises `PermissionError` before the client is
  called. Upsert takes `document_id` from Postgres under RLS and refuses chunks it cannot see.
- Keyword search and fusion are unchanged (FTS stays in Postgres).
- Deletes: re-ingest deletes the document's points before upserting the new chunks. Document delete
  deletes points after the Postgres commit.

## Consequences

- Seed eval (k=5): pgvector and Qdrant give identical metrics. Hybrid R@5 0.974, MRR 0.942. Vector
  0.953 / 0.938. Search p50 is about 15–25 ms slower on Qdrant (an extra network hop, sync client in
  a thread).
- Qdrant writes are outside the Postgres transaction. A rollback after an upsert leaves orphan
  points. Hydration drops them, so a failure shows up as missing results, never leaked ones.
- `VECTOR_STORE` is deployment-wide. Indexes do not record their store (that would need a
  migration). Switching stores means reindexing every KB. Until then the vector leg returns nothing
  for indexes built in the other store, and a warning is logged.
- Known gaps (follow-ups): connector `_delete_missing` (`connectors/sync.py`) deletes chunks without
  calling the store, which leaves orphan points (harmless, filtered at hydration). Org deletion does
  not drop collections. `QDRANT_API_KEY` is not yet in `core/config.secret_values`.

## Alternatives considered

- **One shared collection with an org/KB payload filter.** Fewer collections, but a reindex would
  mix dims or need named vectors, and one missing filter would expose every tenant. Rejected.
- **Collection per org.** Still mixes models and dims across KBs. Rejected.
- **Chunk text in the payload** (skips hydration). This would copy tenant content into a store
  without RLS. Rejected.
- **A `store` column on `embedding_indexes`** (mixed stores per index). Needs a migration, and no
  current need justifies it. Deferred.
