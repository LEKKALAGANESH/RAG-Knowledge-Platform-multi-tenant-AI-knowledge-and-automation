import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.errors import AppError, not_found
from app.core.ratelimit import by_principal
from app.core.tenancy import EDITORS, Tenant, get_tenant, need, require_role
from app.retrieval import tasks
from app.retrieval.embedders import EmbeddingError
from app.retrieval.indexing import start_reindex
from app.retrieval.models import IndexStatus
from app.retrieval.search import Mode, search, visible_kbs

router = APIRouter(tags=["search"])


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)
    k: int = Field(default=10, ge=1, le=50)
    mode: Mode = "hybrid"


class HitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    kb_id: uuid.UUID
    text: str
    loc: dict[str, JsonValue]
    score: float


class SearchOut(BaseModel):
    results: list[HitOut]


class IndexOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kb_id: uuid.UUID
    provider: str
    model: str
    dim: int
    version: int
    status: IndexStatus


@router.post("/search", dependencies=[need("search"), by_principal("search")])
async def search_chunks(body: SearchIn, t: Tenant = Depends(get_tenant)) -> SearchOut:
    kb_ids = await visible_kbs(t.session, body.kb_ids)
    try:
        hits = await search(t.session, body.query, kb_ids, body.k, body.mode)
    except EmbeddingError as exc:
        raise AppError(
            502,
            "embedding_unavailable",
            "The embedding provider failed. Retry, or use keyword mode.",
        ) from exc
    return SearchOut(results=[HitOut.model_validate(h) for h in hits])


@router.post("/kbs/{kb_id}/reindex", status_code=202, dependencies=[need("kb:write")])
async def reindex_kb(kb_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))) -> IndexOut:
    """Builds a new index version with the configured embedder; the KB switches when it is done."""
    try:
        index = await start_reindex(t.session, kb_id)
    except LookupError as exc:
        raise not_found("Knowledge base") from exc
    tasks.reindex_kb.send(str(index.id), str(t.org_id))
    return IndexOut.model_validate(index)
