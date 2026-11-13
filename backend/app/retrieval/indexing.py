"""Writing vectors. A KB has one active index; a reindex builds a new version beside it (AC8)."""

import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import use_tenant
from app.kb.models import Chunk, Document, KnowledgeBase
from app.llm.providers import Usage
from app.llm.usage import usage_event
from app.retrieval.embedders import (
    Embedder,
    configured_embedder,
    embedder_for,
    get_embedding_settings,
)
from app.retrieval.models import EmbeddingIndex, IndexStatus
from app.retrieval.vectorstore import vector_store
from app.usage.budget import BudgetExceeded, check_budget

REINDEX_BATCH = 256


def embedder_of(index: EmbeddingIndex) -> Embedder:
    return embedder_for(index.provider, index.model, index.dim)


async def _lock_kb(session: AsyncSession, kb_id: uuid.UUID) -> KnowledgeBase:
    # Row lock serializes index creation/switch per KB: no duplicate index, no write missed by a
    # reindex (a building version is either visible to the writer or created after its commit).
    kb = await session.get(KnowledgeBase, kb_id, with_for_update=True, populate_existing=True)
    if kb is None:
        raise LookupError(f"knowledge base {kb_id} not visible")
    return kb


async def _new_index(
    session: AsyncSession, kb: KnowledgeBase, emb: Embedder, status: IndexStatus
) -> EmbeddingIndex:
    last = await session.scalar(
        select(func.max(EmbeddingIndex.version)).where(EmbeddingIndex.kb_id == kb.id)
    )
    index = EmbeddingIndex(
        org_id=kb.org_id,
        kb_id=kb.id,
        provider=emb.provider,
        model=emb.model,
        dim=emb.dim,
        version=(last or 0) + 1,
        status=status,
    )
    session.add(index)
    await session.flush()
    await vector_store(session).create_index(index)
    return index


def approx_tokens(texts: list[str]) -> int:
    # ponytail: ~4 chars/token estimate (same as rag.pack_context), used only when the provider
    # reports no token count (fastembed, servers that omit `usage`).
    return sum(-(-len(t) // 4) for t in texts)


async def embed(
    session: AsyncSession, emb: Embedder, org_id: uuid.UUID, texts: list[str]
) -> list[list[float]]:
    """Embeds in provider-sized batches with a budget check before each one, so a large document
    stops at the budget instead of after it (S5). One usage row per batch (AC12) joins the caller's
    transaction (flushed before the next check, so it counts); user is the request's user, NULL in
    worker jobs. Over a `block` budget: BudgetExceeded. On any failure the batches already billed
    are also committed on their own: the caller's rollback would drop them and a retry would pay
    again unrecorded."""
    user_id = session.info.get("tenant", (None, None))[1]
    size = get_embedding_settings().embedding_batch
    vecs: list[list[float]] = []
    billed: list[Usage] = []
    try:
        for i in range(0, len(texts), size):
            part = texts[i : i + size]
            await check_budget(session, org_id)
            got, reported = await asyncio.to_thread(emb.embed, part)
            tokens = approx_tokens(part) if reported is None else reported
            billed.append(Usage(emb.provider, emb.model, tokens, 0))
            session.add(usage_event(org_id, user_id, "embed", billed[-1]))
            vecs += got
    except Exception:
        if billed:
            await _record_apart(session, org_id, user_id, billed)
        raise
    return vecs


async def _record_apart(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID | None, billed: list[Usage]
) -> None:
    async with AsyncSession(bind=session.bind) as own:
        await use_tenant(own, org_id, user_id)
        own.add_all(usage_event(org_id, user_id, "embed", u) for u in billed)
        await own.commit()


async def _embed_into(
    session: AsyncSession,
    index: EmbeddingIndex,
    rows: list[tuple[uuid.UUID, str]],
    replace_doc: uuid.UUID | None = None,
) -> None:
    vecs = await embed(session, embedder_of(index), index.org_id, [t for _, t in rows])
    store = vector_store(session)
    if replace_doc is not None:  # re-ingest: the old chunk ids are gone (pgvector: cascaded)
        await store.delete_documents(index, [replace_doc])
    await store.upsert(index, [c for c, _ in rows], vecs)


async def embed_chunks(session: AsyncSession, doc: Document) -> None:
    """Embed one document's chunks into its KB's active index and any building version.
    Each index uses its own model. Caller commits; re-running is idempotent (upsert)."""
    kb = await _lock_kb(session, doc.kb_id)
    if kb.embedding_index_id is None:
        kb.embedding_index_id = (await _new_index(session, kb, configured_embedder(), "active")).id
    stmt = select(Chunk.id, Chunk.text).where(Chunk.document_id == doc.id).order_by(Chunk.ord)
    rows = [(r[0], r[1]) for r in await session.execute(stmt)]
    targets = await session.scalars(
        select(EmbeddingIndex).where(
            EmbeddingIndex.kb_id == kb.id, EmbeddingIndex.status != "retired"
        )
    )
    try:
        for index in targets.all():
            await _embed_into(session, index, rows, doc.id)
    except BudgetExceeded as exc:  # fail the document readably; never embed with another model
        await session.rollback()
        doc.status, doc.error = "failed", exc.message
        await session.commit()
        raise


async def start_reindex(session: AsyncSession, kb_id: uuid.UUID) -> EmbeddingIndex:
    """New `building` version with the configured embedder, or the one already building."""
    kb = await _lock_kb(session, kb_id)
    building = await session.scalar(
        select(EmbeddingIndex).where(
            EmbeddingIndex.kb_id == kb.id, EmbeddingIndex.status == "building"
        )
    )
    index = building or await _new_index(session, kb, configured_embedder(), "building")
    await session.commit()
    return index


async def build_and_switch(session: AsyncSession, index_id: uuid.UUID) -> None:
    """Fill a building index from the KB's chunks, then make it active and retire the old one."""
    index = await session.get(EmbeddingIndex, index_id)
    if index is None or index.status != "building":
        return
    last: uuid.UUID | None = None
    while True:
        stmt = select(Chunk.id, Chunk.text).where(Chunk.kb_id == index.kb_id)
        if last is not None:
            stmt = stmt.where(Chunk.id > last)
        rows = [
            (r[0], r[1])
            for r in await session.execute(stmt.order_by(Chunk.id).limit(REINDEX_BATCH))
        ]
        if not rows:
            break
        await _embed_into(session, index, rows)
        await session.commit()
        last = rows[-1][0]
    await _switch(session, index)


async def _switch(session: AsyncSession, index: EmbeddingIndex) -> None:
    kb = await _lock_kb(session, index.kb_id)
    old = (
        await session.get(EmbeddingIndex, kb.embedding_index_id) if kb.embedding_index_id else None
    )
    kb.embedding_index_id, index.status = index.id, "active"
    if old is not None and old.id != index.id:
        old.status = "retired"
        await vector_store(session).drop_index(old)
    await session.commit()
