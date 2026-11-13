"""blob -> parse -> chunk -> chunks rows -> finalize. Idempotent per document."""

import asyncio
import hashlib
import logging
import uuid

from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal, use_tenant
from app.ingestion.archives import Report, read_archive
from app.ingestion.chunking import chunk_sections
from app.ingestion.config import IngestionSettings, get_ingestion_settings
from app.ingestion.mime import ARCHIVES, detect_mime
from app.ingestion.parsers import ParseError, parse_isolated
from app.ingestion.storage import BlobStore, get_blob_store
from app.kb.models import Chunk, Document
from app.retrieval.indexing import embed_chunks

log = logging.getLogger("app.ingestion")


async def finalize_document(session: AsyncSession, doc: Document, n_chunks: int) -> None:
    """Last pipeline step: decides ready vs failed and commits (chunks are already flushed)."""
    if n_chunks == 0:
        reasons = "; ".join(str(w) for w in doc.warnings[:3])
        doc.status = "failed"
        doc.error = "No text could be extracted from this file." + (
            f" ({reasons})" if reasons else ""
        )
    else:
        await embed_chunks(session, doc)
        doc.status = "ready"
    await session.commit()


async def process_document(
    doc_id: uuid.UUID,
    org_id: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
    blobs: BlobStore | None = None,
) -> None:
    """Parse errors end in `failed`; blob/DB errors raise so the task queue retries."""
    async with sessions() as session:
        await use_tenant(session, org_id, None)  # RLS: the worker acts inside the doc's org
        doc = await session.get(Document, doc_id)
        if doc is None or doc.status == "deleted":
            return
        doc.status, doc.error, doc.warnings = "processing", None, []
        await session.commit()

        blobs = blobs or get_blob_store()
        data = await asyncio.to_thread(blobs.get, doc.blob_key)
        s = get_ingestion_settings()
        if doc.mime in ARCHIVES:
            await expand_archive(session, doc, data, s, blobs)
            return
        try:
            parsed = await asyncio.to_thread(parse_isolated, doc.mime, data, s)
            pieces = chunk_sections(
                parsed.sections, s.chunk_tokens, s.chunk_overlap, s.table_chunk_cap_tokens
            )
            if len(pieces) > s.max_chunks_per_doc:  # fail whole: never a silently partial index
                raise ParseError(
                    f"The file splits into {len(pieces):,} chunks; the limit is "
                    f"{s.max_chunks_per_doc:,}. Split it into smaller files."
                )
        except ParseError as exc:
            log.info("parse failed", extra={"document_id": str(doc.id)})
            doc.status, doc.error = "failed", str(exc)
            await session.commit()
            return

        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        session.add_all(
            Chunk(
                org_id=doc.org_id,
                kb_id=doc.kb_id,
                document_id=doc.id,
                ord=i,
                text=text,
                loc=dict(loc),
            )
            for i, (text, loc) in enumerate(pieces)
        )
        doc.warnings = list(parsed.warnings)
        if parsed.meta:
            doc.meta = {**doc.meta, **parsed.meta}  # new dict: JSONB changes need reassignment
        await session.flush()
        await finalize_document(session, doc, len(pieces))


Member = tuple[str, str, str, int]  # path, sha256, mime, size


def _depth(doc: Document) -> int:
    depth = doc.meta.get("depth")
    return depth if isinstance(depth, int) else 0


async def _add_member(
    session: AsyncSession, parent: Document, m: Member
) -> tuple[Document | None, str | None, bool]:
    """(document, skip reason, created). Dedupe is the upload rule: one copy per KB per sha."""
    path, sha, kind, size = m
    stmt = select(Document).where(
        Document.kb_id == parent.kb_id, Document.sha256 == sha, Document.source_id.is_(None)
    )
    existing = await session.scalar(stmt.order_by(Document.version.desc()).limit(1))
    if existing is not None:
        if existing.meta.get("archive") == str(parent.id):  # reprocess: already unpacked
            return existing, None, False
        return None, f"skipped: same content as {existing.name}"[:300], False
    child = Document(
        org_id=parent.org_id, kb_id=parent.kb_id, name=f"{parent.name}/{path}"[:255],
        mime=kind, sha256=sha, blob_key=f"{parent.org_id}/{parent.kb_id}/{sha}", warnings=[],
        meta={"size": size, "archive": str(parent.id), "member": path, "depth": _depth(parent) + 1},
        version=1,
    )  # fmt: skip
    try:
        async with session.begin_nested():
            session.add(child)
    except IntegrityError:  # a concurrent upload of the same file won
        return None, "skipped: same content as another document", False
    return child, None, True


def _summarize(
    doc: Document, report: Report, added: list[tuple[Document | None, str | None]]
) -> int:
    """Writes warnings + meta.archive_summary; returns how many members are now documents."""
    results = iter(added)  # one per accepted (reason None) report row, in the same order
    members: list[JsonValue] = []
    warnings: list[JsonValue] = []
    for path, reason in report:
        if reason is None:
            child, reason = next(results)
            if child is not None:
                members.append({"path": path, "document_id": str(child.id)})
                continue
        members.append({"path": path, "skipped": str(reason)})
        warnings.append(f"{path}: {reason}")
    ingested = len(members) - len(warnings)
    summary: JsonValue = {"ingested": ingested, "skipped": len(warnings), "members": members}
    doc.warnings, doc.meta = warnings, {**doc.meta, "archive_summary": summary}
    return ingested


async def expand_archive(
    session: AsyncSession, doc: Document, data: bytes, s: IngestionSettings, blobs: BlobStore
) -> None:
    """Each supported member becomes its own document in the KB (meta.archive = this doc); the
    archive itself ends `ready` with a member report, or `failed` when a limit is hit."""
    staged: list[Member] = []
    seen: dict[str, str] = {}  # sha256 -> first member path with that content

    def sink(path: str, body: bytes) -> str | None:
        kind = detect_mime(body, path)
        if kind is None:
            return "skipped: unsupported file type"
        if kind in ARCHIVES and _depth(doc) + 1 >= s.archive_max_depth:
            return f"skipped: archive inside an archive (nesting limit {s.archive_max_depth})"
        sha = hashlib.sha256(body).hexdigest()
        if sha in seen:
            return f"skipped: same content as {seen[sha]}"[:300]
        seen[sha] = path
        blobs.put(f"{doc.org_id}/{doc.kb_id}/{sha}", body)  # content-addressed, like uploads
        staged.append((path, sha, kind, len(body)))
        return None

    try:
        if doc.source_id is not None:
            raise ParseError("Archives from connectors are not unpacked; upload it directly.")
        report = await asyncio.to_thread(read_archive, doc.mime, data, doc.name, s, sink)
    except ParseError as exc:
        doc.status, doc.error = "failed", str(exc)
        await session.commit()
        return
    added = [await _add_member(session, doc, m) for m in staged]
    if _summarize(doc, report, [(child, reason) for child, reason, _ in added]):
        doc.status = "ready"
    else:
        reasons = "; ".join(str(w) for w in doc.warnings[:3])
        doc.status = "failed"
        doc.error = "No supported files were found in this archive." + (
            f" ({reasons})" if reasons else ""
        )
    await session.commit()
    from app.ingestion.tasks import enqueue  # tasks imports this module

    for child, _, created in added:
        if created and child is not None:
            enqueue(child)


async def mark_failed(
    doc_id: uuid.UUID,
    org_id: uuid.UUID,
    reason: str,
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    async with sessions() as session:
        await use_tenant(session, org_id, None)
        doc = await session.get(Document, doc_id)
        if doc is not None and doc.status in ("queued", "processing"):
            doc.status, doc.error = "failed", reason
            await session.commit()
