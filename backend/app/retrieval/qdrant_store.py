"""Qdrant vector store (VECTOR_STORE=qdrant, e.g. a customer's own Qdrant, BYOC).

One collection per embedding index version, so a reindex never mixes dims (AC8). Points hold only
ids (chunk id as point id; org_id, kb_id, document_id payload): chunk text stays in Postgres and is
hydrated there under RLS. Qdrant has no RLS, so the store enforces the tenant itself: the org comes
from the session's RLS tenant (never from a caller argument), every read and delete is filtered by
that org and the index's KB, and an index row of another org is refused outright.

Writes are not part of the Postgres transaction. A rollback after an upsert leaves points whose
chunks do not exist; hydration drops them, so the failure direction is "missing", never "leaked".
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Literal, TypeVar

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    SearchParams,
    VectorParams,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kb.models import Chunk
from app.retrieval.models import EmbeddingIndex

log = logging.getLogger("app.retrieval")
T = TypeVar("T")
UPSERT_BATCH = 256


class VectorStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    vector_store: Literal["pgvector", "qdrant"] = "pgvector"
    # Operator-configured (env), never user input, so it is not run through the SSRF guard.
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout_s: int = Field(default=10, gt=0)


@lru_cache
def get_vector_store_settings() -> VectorStoreSettings:
    return VectorStoreSettings()


@lru_cache
def qdrant_client() -> QdrantClient:
    s = get_vector_store_settings()
    key = s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None
    return QdrantClient(url=s.qdrant_url, api_key=key, timeout=s.qdrant_timeout_s)


def collection(index: EmbeddingIndex) -> str:
    return f"rag_{uuid.UUID(str(index.id)).hex}"


def _eq(key: str, value: uuid.UUID) -> FieldCondition:
    return FieldCondition(key=key, match=MatchValue(value=str(value)))


class QdrantVectorStore:
    """Same contract as PgVectorStore. The sync client runs in a thread: one client serves every
    event loop (worker jobs each run their own `asyncio.run`)."""

    def __init__(self, session: AsyncSession, client: QdrantClient | None = None) -> None:
        self.s = session
        self.c = client or qdrant_client()

    async def _run(self, fn: Callable[..., T], *args: object, **kw: object) -> T:
        return await asyncio.to_thread(fn, *args, **kw)

    def _scope(self, index: EmbeddingIndex, *more: FieldCondition) -> Filter:
        """org (from the RLS tenant) AND kb [AND more]: the only filter any read or delete uses."""
        org = self.s.info.get("tenant", (None, None))[0]
        if org is None:
            raise PermissionError("vector store needs a tenant-scoped session")
        if index.org_id != org:
            raise PermissionError("embedding index belongs to another organization")
        return Filter(must=[_eq("org_id", org), _eq("kb_id", index.kb_id), *more])

    async def create_index(self, index: EmbeddingIndex) -> None:
        self._scope(index)
        name = collection(index)
        if await self._run(self.c.collection_exists, name):
            return
        cfg = VectorParams(size=int(index.dim), distance=Distance.COSINE)
        await self._run(self.c.create_collection, name, vectors_config=cfg)
        for field in ("org_id", "kb_id", "document_id"):
            await self._run(
                self.c.create_payload_index, name, field, field_schema=PayloadSchemaType.KEYWORD
            )

    async def drop_index(self, index: EmbeddingIndex) -> None:
        self._scope(index)
        await self._run(self.c.delete_collection, collection(index))

    async def upsert(
        self, index: EmbeddingIndex, chunk_ids: list[uuid.UUID], vectors: list[list[float]]
    ) -> None:
        self._scope(index)
        if not chunk_ids:
            return
        # Document ids come from Postgres under RLS: a chunk this tenant cannot see is refused.
        rows = await self.s.execute(
            select(Chunk.id, Chunk.document_id).where(
                Chunk.id.in_(chunk_ids), Chunk.kb_id == index.kb_id
            )
        )
        docs = {r[0]: r[1] for r in rows}
        if missing := set(chunk_ids) - docs.keys():
            raise LookupError(f"{len(missing)} chunks not visible in knowledge base {index.kb_id}")
        payload = {"org_id": str(index.org_id), "kb_id": str(index.kb_id)}
        points = [
            PointStruct(id=str(c), vector=v, payload={**payload, "document_id": str(docs[c])})
            for c, v in zip(chunk_ids, vectors, strict=True)
        ]
        for i in range(0, len(points), UPSERT_BATCH):
            await self._run(self.c.upsert, collection(index), points[i : i + UPSERT_BATCH])

    async def _on_collection(
        self, index: EmbeddingIndex, fn: Callable[..., T], *args: object, **kw: object
    ) -> T | None:
        """None when the collection is missing: an index built while VECTOR_STORE named the other
        store. Reads and deletes find nothing (keyword search still answers) until a reindex."""
        try:
            return await self._run(fn, collection(index), *args, **kw)
        except UnexpectedResponse as e:
            if e.status_code != 404:
                raise
        log.warning("qdrant collection missing; reindex the KB", extra={"index": str(index.id)})
        return None

    async def delete_documents(self, index: EmbeddingIndex, doc_ids: list[uuid.UUID]) -> None:
        docs = FieldCondition(key="document_id", match=MatchAny(any=[str(d) for d in doc_ids]))
        scope = self._scope(index, docs)
        if doc_ids:
            await self._on_collection(index, self.c.delete, FilterSelector(filter=scope))

    async def search(
        self, index: EmbeddingIndex, qvec: list[float], k: int
    ) -> list[tuple[uuid.UUID, float]]:
        """Top-k (chunk_id, cosine similarity) inside the tenant's org and the index's KB."""
        res = await self._on_collection(
            index,
            self.c.query_points,
            query=qvec,
            query_filter=self._scope(index),
            limit=k,
            search_params=SearchParams(hnsw_ef=max(k, 40)),
        )
        return [] if res is None else [(uuid.UUID(str(p.id)), float(p.score)) for p in res.points]
