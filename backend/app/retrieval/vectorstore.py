import json
import uuid
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.models import EmbeddingIndex
from app.retrieval.qdrant_store import QdrantVectorStore, get_vector_store_settings


class VectorStore(Protocol):
    """Vectors of one embedding index; every call is scoped to the session's tenant."""

    async def create_index(self, index: EmbeddingIndex) -> None: ...
    async def drop_index(self, index: EmbeddingIndex) -> None: ...
    async def upsert(
        self, index: EmbeddingIndex, chunk_ids: list[uuid.UUID], vectors: list[list[float]]
    ) -> None: ...
    async def delete_documents(self, index: EmbeddingIndex, doc_ids: list[uuid.UUID]) -> None: ...
    async def search(
        self, index: EmbeddingIndex, qvec: list[float], k: int
    ) -> list[tuple[uuid.UUID, float]]: ...


def vector_store(session: AsyncSession) -> VectorStore:
    """VECTOR_STORE picks the store for the whole deployment; switching it needs a reindex."""
    if get_vector_store_settings().vector_store == "qdrant":
        return QdrantVectorStore(session)
    return PgVectorStore(session)


def _vec(v: list[float]) -> str:
    return json.dumps(v)  # pgvector text format: "[0.1, 0.2, ...]"


def _cast(dim: int) -> str:
    # Inlined, never a bind param: the planner only matches a partial HNSW whose predicate and
    # expression it can see. Both values are typed (int, UUID) before they reach the SQL string.
    return f"vector({int(dim)})"


class PgVectorStore:
    """pgvector in the app DB; one partial HNSW per embedding index. RLS scopes every statement."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create_index(self, index: EmbeddingIndex) -> None:
        # ponytail: CREATE INDEX (non-concurrent) scans the whole table and blocks writes while it
        # runs; fine at MVP size, move to CONCURRENTLY from a non-transactional job past ~1M rows.
        # Its SHARE lock is self-compatible but blocks the caller's later upsert, so two first
        # ingests (different KBs) deadlocked; SHARE ROW EXCLUSIVE is self-exclusive, covers both.
        await self.s.execute(text("LOCK TABLE chunk_embeddings IN SHARE ROW EXCLUSIVE MODE"))
        await self.s.execute(text("SELECT embedding_hnsw(:i, true)"), {"i": index.id})

    async def drop_index(self, index: EmbeddingIndex) -> None:
        # DROP INDEX needs ACCESS EXCLUSIVE on the table. Take it before the DELETE: upgrading
        # from the DELETE's ROW EXCLUSIVE deadlocked two concurrent drops (two switches/KB deletes).
        await self.s.execute(text("LOCK TABLE chunk_embeddings IN ACCESS EXCLUSIVE MODE"))
        await self.s.execute(
            text("DELETE FROM chunk_embeddings WHERE index_id = :i"), {"i": index.id}
        )
        await self.s.execute(text("SELECT embedding_hnsw(:i, false)"), {"i": index.id})

    async def upsert(
        self, index: EmbeddingIndex, chunk_ids: list[uuid.UUID], vectors: list[list[float]]
    ) -> None:
        if not chunk_ids:
            return
        await self.s.execute(
            text(
                "INSERT INTO chunk_embeddings (chunk_id, index_id, org_id, embedding) "
                "SELECT c, :i, :o, CAST(e AS vector) "
                "FROM unnest(CAST(:cs AS uuid[]), CAST(:es AS text[])) AS t(c, e) "
                "ON CONFLICT (index_id, chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding"
            ),
            {"i": index.id, "o": index.org_id, "cs": chunk_ids, "es": [_vec(v) for v in vectors]},
        )

    async def delete_documents(self, index: EmbeddingIndex, doc_ids: list[uuid.UUID]) -> None:
        pass  # chunk_embeddings rows cascade with their chunks

    async def search(
        self, index: EmbeddingIndex, qvec: list[float], k: int
    ) -> list[tuple[uuid.UUID, float]]:
        """Top-k (chunk_id, cosine similarity). The index is per KB, so no KB filter is needed."""
        vt, iid = _cast(index.dim), uuid.UUID(str(index.id))
        dist = f"embedding::{vt} <=> CAST(CAST(:q AS text) AS {vt})"
        await self.s.execute(
            text("SELECT set_config('hnsw.ef_search', :ef, true)"), {"ef": str(max(k, 40))}
        )
        rows = await self.s.execute(
            text(
                f"SELECT chunk_id, 1 - ({dist}) FROM chunk_embeddings "  # noqa: S608
                f"WHERE index_id = '{iid}' ORDER BY {dist} LIMIT :k"
            ),
            {"q": _vec(qvec), "k": k},
        )
        return [(r[0], float(r[1])) for r in rows]
