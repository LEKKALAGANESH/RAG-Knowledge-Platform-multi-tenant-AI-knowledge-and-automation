"""Hybrid retrieval: per-index vector search + Postgres FTS, fused by relative score."""

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import use_tenant
from app.core.errors import not_found
from app.kb.models import Chunk, Document, KnowledgeBase
from app.llm.providers import Usage
from app.llm.usage import usage_event
from app.retrieval.embedders import Embedder
from app.retrieval.indexing import approx_tokens, embedder_of
from app.retrieval.models import EmbeddingIndex
from app.retrieval.rerank import configured_reranker, get_rerank_settings
from app.retrieval.vectorstore import vector_store
from app.usage.budget import check_budget

log = logging.getLogger("app.retrieval")

Mode = Literal["hybrid", "vector", "keyword"]
Ranked = list[tuple[uuid.UUID, float]]
# Weight of the keyword list in `fuse`; every vector list weighs 1 (vector share ~0.67). Seed eval
# (A3.2): keyword weight 1.0 -> R@5 0.974 MRR 0.906, 0.5 -> 0.974/0.942, 0.33 -> 0.953/0.968;
# vector-only 0.953/0.938. Rank fusion (RRF) lost to vector-only at every weight tried.
KEYWORD_WEIGHT = 0.5


@dataclass(frozen=True)
class Hit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    kb_id: uuid.UUID
    text: str
    loc: dict[str, JsonValue]
    score: float
    # Cosine and embedding model when the chunk came from a vector list (None for keyword-only
    # hits). Lets /v1/answer gate on similarity even when `score` is a fused score.
    cosine: float | None = None
    embed_model: str | None = None


def fuse(lists: list[Ranked], weights: list[float]) -> Ranked:
    """Relative score fusion: each list's scores are min-max scaled to [0, 1] (a lone or all-equal
    list scales to 1), then summed with the list weights. Unlike rank fusion, a weak keyword match
    cannot outrank a much closer vector hit just by appearing in both lists (cross-language)."""
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranked, w in zip(lists, weights, strict=True):
        if not ranked:
            continue
        hi, lo = ranked[0][1], ranked[-1][1]  # lists arrive sorted best first
        for chunk_id, s in ranked:
            scores[chunk_id] += w * ((s - lo) / (hi - lo) if hi > lo else 1.0)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


async def visible_kbs(session: AsyncSession, kb_ids: list[uuid.UUID] | None) -> list[uuid.UUID]:
    """The requested KBs (404 if any is not visible to this tenant), or all of the tenant's KBs."""
    stmt = select(KnowledgeBase.id)
    if kb_ids is not None:
        stmt = stmt.where(KnowledgeBase.id.in_(kb_ids))
    found = list((await session.scalars(stmt)).all())
    if kb_ids is not None and set(found) != set(kb_ids):
        raise not_found("Knowledge base")
    return found


# Question words and function words of EN/DE/ES that survive the length filter in KEYWORD_SQL.
# The 'simple' config keeps every word (the corpus is multilingual), and per-language configs would
# need the query language: one small shared list is enough because ts_rank still weights matches.
# ponytail: hand list; switch to per-language configs once documents carry a detected language.
STOPWORDS = sorted(
    set(
        # EN
        "the and for are was were what which who whom how does did can could our with from that "
        "this these have has had many much when where why there their your you about into any all "
        "will would should must they them its not but get long "
        # DE
        "der die das und ist wie wer wann für mit von den dem des ein eine einen nicht auf sind "
        "werden wird lange "
        # ES
        "los las del que qué por para con una uno cómo como cuál cuánto cuántos cuándo son está "
        "hay sus más".split()
    )
)
# OR over the query's content words, tokenized by the same parser as chunks.tsv. A query with
# "quoted phrases" keeps strict web-search syntax. Words of <= 2 letters drop unless they hold a
# digit (Q2, L4). No content word left -> NULL tsquery -> no rows.
KEYWORD_SQL = """
SELECT c.id, ts_rank(c.tsv, q) AS score FROM chunks c, (
SELECT CASE WHEN strpos(:q, '"') > 0 THEN websearch_to_tsquery('simple', :q) ELSE (
  SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | '))
  FROM unnest(to_tsvector('simple', :q))
  WHERE (length(lexeme) > 2 OR lexeme ~ '[0-9]') AND lexeme <> ALL(CAST(:stop AS text[]))
) END) AS t(q)
WHERE c.kb_id = ANY(CAST(:kbs AS uuid[])) AND c.tsv @@ q
ORDER BY score DESC LIMIT :n"""


async def keyword(session: AsyncSession, query: str, kb_ids: list[uuid.UUID], n: int) -> Ranked:
    rows = await session.execute(
        text(KEYWORD_SQL),
        {"q": query, "kbs": kb_ids, "n": n, "stop": STOPWORDS},
    )
    return [(r[0], float(r[1])) for r in rows]


async def _embed_query(
    session: AsyncSession, emb: Embedder, org_id: uuid.UUID, query: str
) -> list[float]:
    """Budget check, then commit so the pooled connection is released while the (possibly remote,
    possibly slow) model runs, then the usage row in its own short session (AC12): it survives even
    if a later search step fails. Same pattern as the agent loop (H3)."""
    user_id = session.info.get("tenant", (None, None))[1]
    await check_budget(session, org_id)
    await session.commit()
    (vec,), reported = await asyncio.to_thread(emb.embed, [query])
    tokens = approx_tokens([query]) if reported is None else reported
    async with AsyncSession(bind=session.bind) as own:
        await use_tenant(own, org_id, user_id)
        own.add(usage_event(org_id, user_id, "embed", Usage(emb.provider, emb.model, tokens, 0)))
        await own.commit()
    return vec


async def vector(
    session: AsyncSession, query: str, kb_ids: list[uuid.UUID], n: int
) -> list[tuple[str, Ranked]]:
    """(model, ranked list) per embedding model; each KB's index is queried with its own model
    (AC8)."""
    indexes = await session.scalars(
        select(EmbeddingIndex)
        .join(KnowledgeBase, KnowledgeBase.embedding_index_id == EmbeddingIndex.id)
        .where(KnowledgeBase.id.in_(kb_ids))
    )
    groups: dict[tuple[str, str, int], list[EmbeddingIndex]] = defaultdict(list)
    for index in indexes.all():
        groups[(index.provider, index.model, index.dim)].append(index)
    store, lists = vector_store(session), []
    for same_model in groups.values():
        index = same_model[0]
        qvec = await _embed_query(session, embedder_of(index), index.org_id, query)
        merged = [hit for index in same_model for hit in await store.search(index, qvec, n)]
        lists.append((index.model, sorted(merged, key=lambda h: h[1], reverse=True)[:n]))
    return lists


async def _hydrate(
    session: AsyncSession, ranked: Ranked, cosines: dict[uuid.UUID, tuple[float, str]]
) -> list[Hit]:
    ids = [c for c, _ in ranked]
    stmt = (
        select(Chunk, Document.name)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(ids), Document.status == "ready")
    )
    rows = {c.id: (c, name) for c, name in await session.execute(stmt)}
    hits = [
        Hit(
            c.id,
            c.document_id,
            name,
            c.kb_id,
            c.text,
            c.loc,
            score,
            *cosines.get(c.id, (None, None)),
        )
        for chunk_id, score in ranked
        if chunk_id in rows
        for c, name in [rows[chunk_id]]
    ]
    return hits


async def rerank(query: str, hits: list[Hit], top_n: int) -> list[Hit]:
    """Reorders the first `top_n` hits by the configured cross-encoder; `score` becomes its score
    (cosine is kept, so the /v1/answer gate is unchanged). Any reranker failure is logged and the
    fused order returned: reranking improves a search, it must never fail one."""
    head = hits[:top_n]
    try:
        reranker = configured_reranker()
        if reranker is None or not head:
            return hits
        scores = await asyncio.to_thread(reranker.scores, query, [h.text for h in head])
        if len(scores) != len(head):
            raise ValueError(f"reranker returned {len(scores)} scores for {len(head)} texts")
    except Exception as e:  # any provider/model error degrades to the fused order
        log.warning("rerank failed, using fused order: %s: %s", type(e).__name__, e)
        return hits
    order = sorted(zip(scores, head, strict=True), key=lambda p: p[0], reverse=True)
    return [replace(h, score=s) for s, h in order] + hits[top_n:]


async def search(
    session: AsyncSession,
    query: str,
    kb_ids: list[uuid.UUID],
    k: int = 10,
    mode: Mode = "hybrid",
    use_rerank: bool = True,
) -> list[Hit]:
    """Tenant-scoped retrieval over KBs already checked by `visible_kbs`. Scores: cosine (vector,
    single model), ts_rank (keyword), `fuse` score (hybrid, or vector across several models), or
    the reranker's score when RERANK_PROVIDER is set (hybrid/vector only, top RERANK_TOP_N)."""
    if not kb_ids:
        return []
    n = min(max(k * 3, 20), 150)  # candidate pool per ranker, before fusion and status filter
    by_model = [] if mode == "keyword" else await vector(session, query, kb_ids, n)
    cosines = {c: (cos, model) for model, ranked in by_model for c, cos in ranked}
    lists, weights = [ranked for _, ranked in by_model], [1.0] * len(by_model)
    if mode != "vector":
        lists.append(await keyword(session, query, kb_ids, n))
        weights.append(KEYWORD_WEIGHT)
    ranked = lists[0] if len(lists) == 1 else fuse(lists, weights)
    hits = await _hydrate(session, ranked, cosines)
    if use_rerank and mode != "keyword" and get_rerank_settings().rerank_provider != "none":
        await session.commit()  # no pooled connection held while the cross-encoder runs
        hits = await rerank(query, hits, get_rerank_settings().rerank_top_n)
    return hits[:k]
