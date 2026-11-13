import asyncio
import base64
import hashlib
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import Select, exists, select, tuple_
from sqlalchemy.exc import IntegrityError

from app.audit.service import emit
from app.core.errors import AppError, not_found
from app.core.ratelimit import by_principal
from app.core.tenancy import EDITORS, Tenant, get_tenant, need, require_role
from app.ingestion.config import get_ingestion_settings
from app.ingestion.mime import LABELS, SUPPORTED, detect_mime
from app.ingestion.storage import get_blob_store
from app.ingestion.tasks import enqueue
from app.kb.models import Chunk, Document, KnowledgeBase
from app.retrieval.models import EmbeddingIndex
from app.retrieval.vectorstore import vector_store

router = APIRouter(tags=["knowledge-bases"])
DocStatus = Literal["queued", "processing", "ready", "failed", "deleted"]


class KBIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    settings: dict[str, JsonValue] = {}


class KBPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, JsonValue] | None = None


class KBOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    settings: dict[str, JsonValue]
    embedding_index_id: uuid.UUID | None
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kb_id: uuid.UUID
    source_id: uuid.UUID | None
    name: str
    mime: str
    sha256: str
    version: int
    status: DocStatus
    error: str | None
    warnings: list[JsonValue]
    meta: dict[str, JsonValue]
    created_at: datetime


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ord: int
    text: str
    loc: dict[str, JsonValue]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def _invalid_cursor() -> AppError:
    return AppError(422, "invalid_cursor", "The pagination cursor is invalid.")


async def _page[M: (KnowledgeBase, Document)](
    t: Tenant, stmt: Select[M], model: type[M], cursor: str | None, limit: int
) -> tuple[list[M], str | None]:
    """Keyset pagination, newest first. Cursor = base64("<created_at iso>|<id>")."""
    if cursor:
        try:
            at, _, raw_id = base64.urlsafe_b64decode(cursor).decode().partition("|")
            key = (datetime.fromisoformat(at), uuid.UUID(raw_id))
        except ValueError as exc:
            raise _invalid_cursor() from exc
        stmt = stmt.where(tuple_(model.created_at, model.id) < key)
    stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    rows = list((await t.session.scalars(stmt)).all())
    if len(rows) <= limit:
        return rows, None
    last = rows[limit - 1]
    nxt = base64.urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
    return rows[:limit], nxt


async def _get_kb(t: Tenant, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = await t.session.get(KnowledgeBase, kb_id)  # RLS hides other orgs' rows -> 404
    if kb is None:
        raise not_found("Knowledge base")
    return kb


async def _get_doc(t: Tenant, doc_id: uuid.UUID) -> Document:
    doc = await t.session.get(Document, doc_id)
    if doc is None:
        raise not_found("Document")
    return doc


@router.get("/kbs", dependencies=[need("kb:read")])
async def list_kbs(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    t: Tenant = Depends(get_tenant),
) -> Page[KBOut]:
    rows, nxt = await _page(t, select(KnowledgeBase), KnowledgeBase, cursor, limit)
    return Page(items=[KBOut.model_validate(r) for r in rows], next_cursor=nxt)


@router.post("/kbs", status_code=201, dependencies=[need("kb:write")])
async def create_kb(body: KBIn, t: Tenant = Depends(require_role(*EDITORS))) -> KBOut:
    kb = KnowledgeBase(org_id=t.org_id, name=body.name, settings=body.settings)
    t.session.add(kb)
    await emit(t, "kb.create", kb)
    await t.session.commit()
    await t.session.refresh(kb)
    return KBOut.model_validate(kb)


@router.get("/kbs/{kb_id}", dependencies=[need("kb:read")])
async def get_kb(kb_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> KBOut:
    return KBOut.model_validate(await _get_kb(t, kb_id))


@router.patch("/kbs/{kb_id}", dependencies=[need("kb:write")])
async def update_kb(
    kb_id: uuid.UUID, body: KBPatch, t: Tenant = Depends(require_role(*EDITORS))
) -> KBOut:
    kb = await _get_kb(t, kb_id)
    if body.name is not None:
        kb.name = body.name
    if body.settings is not None:
        kb.settings = body.settings
    await t.session.commit()
    return KBOut.model_validate(kb)


@router.delete("/kbs/{kb_id}", status_code=204, dependencies=[need("kb:write")])
async def delete_kb(kb_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))) -> Response:
    kb = await _get_kb(t, kb_id)
    # Rows cascade, but each index's partial HNSW is a named DB object that would be orphaned.
    for index in await t.session.scalars(
        select(EmbeddingIndex).where(EmbeddingIndex.kb_id == kb.id)
    ):
        await vector_store(t.session).drop_index(index)
    await emit(t, "kb.delete", kb)
    await t.session.delete(kb)  # documents + chunks cascade in the DB
    await t.session.commit()
    return Response(status_code=204)


@router.get("/kbs/{kb_id}/documents", dependencies=[need("kb:read")])
async def list_documents(
    kb_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    t: Tenant = Depends(get_tenant),
) -> Page[DocumentOut]:
    await _get_kb(t, kb_id)
    stmt = select(Document).where(Document.kb_id == kb_id)
    rows, nxt = await _page(t, stmt, Document, cursor, limit)
    return Page(items=[DocumentOut.model_validate(r) for r in rows], next_cursor=nxt)


@router.get("/documents/{doc_id}", dependencies=[need("kb:read")])
async def get_document(doc_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> DocumentOut:
    return DocumentOut.model_validate(await _get_doc(t, doc_id))


@router.get("/documents/{doc_id}/chunks", dependencies=[need("kb:read")])
async def list_chunks(
    doc_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    t: Tenant = Depends(get_tenant),
) -> Page[ChunkOut]:
    """Chunks in reading order. Cursor = base64("<last ord>"); ord is unique per document."""
    await _get_doc(t, doc_id)
    stmt = select(Chunk).where(Chunk.document_id == doc_id)
    if cursor:
        try:
            stmt = stmt.where(Chunk.ord > int(base64.urlsafe_b64decode(cursor)))
        except ValueError as exc:
            raise _invalid_cursor() from exc
    rows = list((await t.session.scalars(stmt.order_by(Chunk.ord).limit(limit + 1))).all())
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = base64.urlsafe_b64encode(str(rows[-1].ord).encode()).decode()
    return Page(items=[ChunkOut.model_validate(r) for r in rows], next_cursor=nxt)


@router.delete("/documents/{doc_id}", status_code=204, dependencies=[need("documents:write")])
async def delete_document(
    doc_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))
) -> Response:
    doc = await _get_doc(t, doc_id)
    await emit(t, "document.delete", doc, {"kb_id": str(doc.kb_id)})
    gone = [doc, *await _archive_members(t, doc)]
    ids, kb_id = [d.id for d in gone], doc.kb_id
    for d in gone:
        await t.session.delete(d)  # chunks cascade in the DB
    await t.session.commit()
    store = vector_store(t.session)  # an external store (Qdrant) does not cascade
    for index in await t.session.scalars(
        select(EmbeddingIndex).where(
            EmbeddingIndex.kb_id == kb_id, EmbeddingIndex.status != "retired"
        )
    ):
        await store.delete_documents(index, ids)
    for key in {d.blob_key for d in gone}:
        if not await t.session.scalar(select(exists().where(Document.blob_key == key))):
            await asyncio.to_thread(get_blob_store().delete, key)
    return Response(status_code=204)


async def _archive_members(t: Tenant, doc: Document) -> list[Document]:
    """Documents unpacked from this archive (meta.archive), nested archives included.
    ponytail: unindexed JSONB lookup per level; add an index on (meta->>'archive') if deletes
    in very large KBs get slow."""
    found: list[Document] = []
    parents = [doc]
    while parents:
        ids = [str(p.id) for p in parents]
        stmt = select(Document).where(Document.meta["archive"].astext.in_(ids))
        parents = list((await t.session.scalars(stmt)).all())
        found += parents
    return found


async def _read_limited(file: UploadFile) -> bytes:
    limit = get_ingestion_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise AppError(413, "file_too_large", f"Files must be at most {limit // 2**20} MB.")
    if not data:
        raise AppError(422, "empty_file", "The uploaded file is empty.")
    return data


async def _latest_upload(t: Tenant, kb_id: uuid.UUID, sha256: str) -> Document | None:
    """Newest uploaded (not crawled) row for this content; crawled docs never dedupe an upload."""
    stmt = select(Document).where(
        Document.kb_id == kb_id, Document.sha256 == sha256, Document.source_id.is_(None)
    )
    return await t.session.scalar(stmt.order_by(Document.version.desc()).limit(1))


@router.post(
    "/kbs/{kb_id}/documents",
    status_code=201,
    dependencies=[need("documents:write"), by_principal("upload")],
)
async def upload_document(
    kb_id: uuid.UUID,
    response: Response,
    file: UploadFile = File(...),
    t: Tenant = Depends(require_role(*EDITORS)),
) -> DocumentOut:
    """201 = new document queued; 200 = identical file already in this KB (not reprocessed)."""
    await _get_kb(t, kb_id)
    data = await _read_limited(file)
    name = (file.filename or "upload")[:255]
    mime = detect_mime(data, name)
    if mime not in SUPPORTED:
        raise AppError(415, "unsupported_type", f"Supported: {', '.join(LABELS.values())}.")
    sha = hashlib.sha256(data).hexdigest()
    prev = await _latest_upload(t, kb_id, sha)
    if prev is not None and prev.status != "deleted":
        response.status_code = 200
        return DocumentOut.model_validate(prev)
    key = f"{t.org_id}/{kb_id}/{sha}"
    await asyncio.to_thread(get_blob_store().put, key, data)
    doc = Document(
        org_id=t.org_id,
        kb_id=kb_id,
        name=name,
        mime=mime,
        sha256=sha,
        blob_key=key,
        warnings=[],
        meta={"size": len(data)},
        version=prev.version + 1 if prev else 1,  # (kb, sha, version) is unique among uploads
    )
    t.session.add(doc)
    try:
        await emit(t, "document.upload", doc, {"kb_id": str(kb_id)})
        await t.session.commit()
    except IntegrityError:  # concurrent upload of the same file won the race
        await t.session.rollback()
        response.status_code = 200
        return DocumentOut.model_validate(await _latest_upload(t, kb_id, sha))
    await t.session.refresh(doc)
    enqueue(doc)
    return DocumentOut.model_validate(doc)


@router.post(
    "/documents/{doc_id}/reprocess", status_code=202, dependencies=[need("documents:write")]
)
async def reprocess_document(
    doc_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))
) -> DocumentOut:
    doc = await _get_doc(t, doc_id)
    doc.status, doc.error, doc.warnings = "queued", None, []
    await t.session.commit()
    enqueue(doc)
    return DocumentOut.model_validate(doc)
