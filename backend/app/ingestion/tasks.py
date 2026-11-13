import asyncio
import uuid
from typing import Any

import dramatiq
from dramatiq.middleware import CurrentMessage
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import use_tenant, worker_sessions
from app.ingestion.config import get_ingestion_settings
from app.ingestion.parsers.media import log_transcribe_status
from app.ingestion.parsers.ocr import log_ocr_status
from app.ingestion.pipeline import mark_failed, process_document
from app.kb.models import Document
from app.usage.budget import BudgetExceeded
from app.worker import broker

log_ocr_status(get_ingestion_settings())  # once per API / worker process, at startup
log_transcribe_status(get_ingestion_settings())

RETRY_EXHAUSTED = "Processing failed after repeated errors. Use Reprocess to try again."
MAX_RETRIES = 5

if not any(isinstance(m, CurrentMessage) for m in broker.middleware):
    broker.add_middleware(CurrentMessage())  # exposes message.options["retries"] to the actor


@dramatiq.actor(broker=broker, queue_name="ingest", max_retries=0)
def ingest_retries_exhausted(message: dict[str, Any], _info: dict[str, Any]) -> None:
    doc_id, org_id = message["args"]
    asyncio.run(
        mark_failed(uuid.UUID(doc_id), uuid.UUID(org_id), RETRY_EXHAUSTED, worker_sessions())
    )


def _reason(exc: Exception) -> str:
    # Raw exception text can carry storage paths/hosts; users get a category instead.
    if isinstance(exc, FileNotFoundError):
        return "the stored file could not be found"
    if isinstance(exc, SQLAlchemyError):
        return "the database was unavailable"
    if isinstance(exc, OSError):
        return "a storage or network service was unavailable"
    return f"an unexpected error occurred ({type(exc).__name__})"


async def _mark_retrying(doc_id: uuid.UUID, org_id: uuid.UUID, error: str) -> None:
    async with worker_sessions()() as session:
        await use_tenant(session, org_id, None)  # RLS: act inside the doc's org
        doc = await session.get(Document, doc_id)
        if doc is not None and doc.status in ("queued", "processing"):
            doc.status, doc.error = "processing", error
            await session.commit()


@dramatiq.actor(
    broker=broker,
    queue_name="ingest",
    max_retries=MAX_RETRIES,
    min_backoff=5_000,
    max_backoff=300_000,
    on_retry_exhausted=ingest_retries_exhausted.actor_name,
    throws=(BudgetExceeded,),  # the doc is already failed readably; retrying can't help
)
def ingest_document(doc_id: str, org_id: str) -> None:
    doc, org = uuid.UUID(doc_id), uuid.UUID(org_id)
    try:
        asyncio.run(process_document(doc, org, worker_sessions()))
    except BudgetExceeded:
        raise
    except Exception as exc:
        message = CurrentMessage.get_current_message()
        retries = message.options.get("retries", 0) if message else MAX_RETRIES
        if retries < MAX_RETRIES:  # a retry follows; the last failure goes to on_retry_exhausted
            note = f"Retrying (attempt {retries + 1} of {MAX_RETRIES}): {_reason(exc)}."
            asyncio.run(_mark_retrying(doc, org, note))
        raise


def enqueue(doc: Document) -> None:
    ingest_document.send(str(doc.id), str(doc.org_id))
