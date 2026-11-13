"""Generic sync engine: connector items -> documents keyed by (source_id, external_key).

Unchanged items (same content hash) are skipped; changed ones get a new version whose chunks and
vectors replace the old ones. Documents missing from a run are deleted only when that run saw
everything (no fetch failures, discovery finished): a partial view must never delete (AC7).
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import dramatiq
from cryptography.fernet import InvalidToken
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import ConnectorError, Item
from app.connectors.models import RunStatus, Source, SyncRun
from app.connectors.oauth import ReconnectRequired
from app.connectors.registry import REGISTRY
from app.core.crypto import decrypt
from app.core.db import SessionLocal, use_tenant, worker_sessions
from app.ingestion.mime import SUPPORTED, detect_mime
from app.ingestion.pipeline import process_document
from app.ingestion.storage import BlobStore, get_blob_store
from app.kb.models import Chunk, Document
from app.retrieval.models import EmbeddingIndex
from app.retrieval.vectorstore import vector_store
from app.worker import broker

log = logging.getLogger("app.connectors.sync")
STAT_KEYS = ("discovered", "added", "updated", "unchanged", "deleted", "failed")
SYNC_TIME_LIMIT_MS = 4 * 3600 * 1000  # 2000 pages at 1 rps is ~35 min; dramatiq default is 10
# Past the actor's time limit a `running` run's worker is dead (killed, OOM, lost message).
STALE_AFTER = timedelta(milliseconds=SYNC_TIME_LIMIT_MS) + timedelta(minutes=10)


@dataclass(frozen=True)
class _Prev:
    id: uuid.UUID
    content_hash: str | None
    status: str


@dataclass
class _Ctx:
    session: AsyncSession
    source: Source
    sessions: async_sessionmaker[AsyncSession]
    blobs: BlobStore


async def drop_blob_if_orphan(session: AsyncSession, blobs: BlobStore, key: str) -> None:
    """Blobs are content-addressed and shared; delete only when no live document points at one."""
    live = exists().where(Document.blob_key == key, Document.status != "deleted")
    if not await session.scalar(select(live)):
        await asyncio.to_thread(blobs.delete, key)


async def _store(ctx: _Ctx, prev: _Prev | None, item: Item, data: bytes, h: str) -> Document:
    src = ctx.source
    mime = item.mime_hint if item.mime_hint in SUPPORTED else detect_mime(data, item.name)
    if mime not in SUPPORTED:
        raise ConnectorError("Unsupported content type.")
    sha = hashlib.sha256(data).hexdigest()
    key = f"{src.org_id}/{src.kb_id}/{sha}"
    await asyncio.to_thread(ctx.blobs.put, key, data)
    if prev is None:
        doc = Document(
            org_id=src.org_id, kb_id=src.kb_id, source_id=src.id, external_key=item.external_key,
            warnings=[], version=1,
        )  # fmt: skip
        ctx.session.add(doc)
    else:
        found = await ctx.session.get(Document, prev.id, populate_existing=True)
        if found is None:
            raise LookupError("document vanished during sync")
        doc, doc.version = found, found.version + 1
    doc.name, doc.mime, doc.sha256, doc.blob_key = item.name, mime, sha, key
    doc.content_hash, doc.meta = h, {**item.meta, "size": len(data)}
    doc.status, doc.error = "queued", None
    await ctx.session.commit()
    return doc


def _unchanged(prev: _Prev | None, h: str | None) -> bool:
    # A `failed` doc with the same hash failed permanently (parse); retrying cannot help.
    return bool(h) and prev is not None and prev.content_hash == h and prev.status in (
        "ready", "failed",
    )  # fmt: skip


async def _sync_item(ctx: _Ctx, prev: _Prev | None, item: Item) -> str:
    try:
        if _unchanged(prev, item.content_hash):  # connector's marker: skip without downloading
            return "unchanged"
        data = await item.fetch()
        h = item.content_hash or hashlib.sha256(data).hexdigest()
        if _unchanged(prev, h):
            return "unchanged"
        old_blob = None
        if prev is not None:
            old = await ctx.session.get(Document, prev.id)
            old_blob = old.blob_key if old else None
        doc = await _store(ctx, prev, item, data, h)
        await process_document(doc.id, doc.org_id, ctx.sessions, ctx.blobs)
    except Exception as exc:
        sid = ctx.source.id
        await ctx.session.rollback()
        await ctx.session.refresh(ctx.source)  # rollback expired it; later code reads it
        log.warning("sync item failed", extra={"source_id": str(sid), "error": _reason(exc)})
        return "failed"
    if old_blob and old_blob != doc.blob_key:
        await drop_blob_if_orphan(ctx.session, ctx.blobs, old_blob)
    return "added" if prev is None else "updated"


async def _delete_missing(ctx: _Ctx, ids: list[uuid.UUID]) -> int:
    for doc_id in ids:
        doc = await ctx.session.get(Document, doc_id, populate_existing=True)
        if doc is None:
            continue
        await ctx.session.execute(
            delete(Chunk).where(Chunk.document_id == doc_id)
        )  # vectors cascade
        doc.status, doc.error, doc.content_hash = "deleted", None, None
        await ctx.session.commit()
        await drop_blob_if_orphan(ctx.session, ctx.blobs, doc.blob_key)
    store = vector_store(ctx.session)  # an external store (Qdrant) does not cascade
    for index in await ctx.session.scalars(
        select(EmbeddingIndex).where(
            EmbeddingIndex.kb_id == ctx.source.kb_id, EmbeddingIndex.status != "retired"
        )
    ):
        await store.delete_documents(index, ids)
    return len(ids)


def _grant(secret_enc: bytes | None) -> object:
    """The OAuth grant id stored with the tokens (connectors/router._bind); None otherwise."""
    try:
        tokens = json.loads(decrypt(secret_enc)) if secret_enc else None
    except (InvalidToken, ValueError):
        return None
    return tokens.get("grant") if isinstance(tokens, dict) else None


def _reason(exc: Exception) -> str:
    # ConnectorError text is written to be user-safe; anything else is reduced to its type.
    return str(exc)[:300] if isinstance(exc, ConnectorError) else f"{type(exc).__name__}"


async def _crawl(ctx: _Ctx, stats: Counter[str]) -> None:
    src = ctx.source
    rows = await ctx.session.execute(
        select(Document.external_key, Document.id, Document.content_hash, Document.status).where(
            Document.source_id == src.id
        )
    )
    prev = {r[0]: _Prev(r[1], r[2], r[3]) for r in rows if r[0] is not None}
    seen: set[str] = set()
    secret = decrypt(src.secret_enc) if src.secret_enc else None
    cursor = dict(src.cursor)
    async for item in REGISTRY[src.type].discover(dict(src.config), secret, cursor):
        if item.external_key in seen:
            continue
        seen.add(item.external_key)
        stats["discovered"] += 1
        stats[await _sync_item(ctx, prev.get(item.external_key), item)] += 1
    incremental = bool(cursor.pop("incremental", False))  # connector yielded changed items only
    if stats["failed"] == 0:
        if not incremental:  # full view of the source: absent items are really gone
            gone = [p.id for k, p in prev.items() if k not in seen and p.status != "deleted"]
            stats["deleted"] = await _delete_missing(ctx, gone)
        src.cursor = cursor


async def run_sync(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
    blobs: BlobStore | None = None,
) -> None:
    async with sessions() as session:
        await use_tenant(session, org_id, None)  # RLS: the worker acts inside the source's org
        run = await session.get(SyncRun, run_id)
        source = await session.get(Source, run.source_id) if run else None
        if run is None or source is None or run.status != "running":
            return
        source.status = "syncing"
        grant = _grant(source.secret_enc)
        await session.commit()
        ctx = _Ctx(session, source, sessions, blobs or get_blob_store())
        stats: Counter[str] = Counter({k: 0 for k in STAT_KEYS})
        status: RunStatus = "succeeded"
        error: str | None = None
        reconnect = False
        try:
            await _crawl(ctx, stats)
            if stats["failed"]:
                status, error = "partial", f"{stats['failed']} item(s) failed; nothing was deleted."
        except Exception as exc:
            await session.rollback()
            await session.refresh(source)
            log.warning("sync failed", extra={"source_id": str(source.id), "error": _reason(exc)})
            status, error = "failed", f"Sync failed: {_reason(exc)}"
            reconnect = isinstance(exc, ReconnectRequired)
        # A reconnect landed mid-run: the dead grant was this run's, the source has a new one.
        superseded = reconnect and _grant(source.secret_enc) != grant
        await _finish(session, run, source, status, error, stats, reconnect, superseded)


async def _finish(
    session: AsyncSession, run: SyncRun, source: Source, status: RunStatus, error: str | None,
    stats: Counter[str], reconnect: bool = False, superseded: bool = False,
) -> None:  # fmt: skip
    await session.refresh(source, ["status"])  # an API PATCH may have disabled it meanwhile
    now = datetime.now(UTC)
    run.status, run.error, run.finished_at = status, error, now
    run.stats = {k: stats[k] for k in STAT_KEYS}
    if source.status != "disabled":
        source.status = "error" if status == "failed" and not superseded else "idle"
    if status != "failed":
        source.last_sync_at = now
    if not superseded:  # the reconnect already cleared these for the new grant
        # Readable run error (None on success); only a gone OAuth grant asks for a new sign-in.
        source.last_error, source.needs_reconnect = error, reconnect
    await session.commit()


@dramatiq.actor(broker=broker, queue_name="sync", max_retries=0, time_limit=SYNC_TIME_LIMIT_MS)
def sync_source(run_id: str, org_id: str) -> None:
    asyncio.run(run_sync(uuid.UUID(run_id), uuid.UUID(org_id), worker_sessions()))


async def reap_stale_runs(session: AsyncSession, source_id: uuid.UUID | None = None) -> None:
    """Fail dead `running` runs (in the session's org) and free their sources. Caller commits."""
    stmt = update(SyncRun).where(
        SyncRun.status == "running", SyncRun.started_at < datetime.now(UTC) - STALE_AFTER
    )
    if source_id is not None:
        stmt = stmt.where(SyncRun.source_id == source_id)
    stmt = stmt.values(status="failed", error="The sync worker stopped before finishing.")
    freed = list(await session.scalars(stmt.returning(SyncRun.source_id)))
    if freed:  # run_sync set `syncing`; with its worker gone nothing else would reset it
        await session.execute(
            update(Source)
            .where(Source.id.in_(freed), Source.status == "syncing")
            .values(status="idle")
        )


async def start_due_syncs(session: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    """Scheduler tick for one org (session scoped to it): reap dead runs, start due syncs.

    Due = `schedule_minutes` set, status `idle`, and last_sync_at (or created_at) + schedule <= now.
    `disabled` is off; `error` (last run failed outright, e.g. bad config/robots) waits for a manual
    sync so a broken source is not retried forever; `syncing` already runs. Safe with several
    schedulers: the conditional UPDATE to `syncing` claims each source once, and the
    sync_runs_one_running index rejects a run while a manual one is in flight.
    """
    await reap_stale_runs(session)
    await session.commit()
    due_at = func.coalesce(Source.last_sync_at, Source.created_at) + (
        Source.schedule_minutes * text("interval '1 minute'")
    )
    due = (Source.schedule_minutes.is_not(None), Source.status == "idle", due_at <= func.now())
    started: list[uuid.UUID] = []
    for source_id in list(await session.scalars(select(Source.id).where(*due))):
        claim = update(Source).where(Source.id == source_id, *due).values(status="syncing")
        if (await session.execute(claim)).rowcount == 0:  # type: ignore[attr-defined]
            await session.rollback()  # another scheduler claimed it first
            continue
        run = SyncRun(org_id=org_id, source_id=source_id, stats={})
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:  # a manual sync is already running
            await session.rollback()
            continue
        sync_source.send(str(run.id), str(org_id))
        started.append(source_id)
    return started
