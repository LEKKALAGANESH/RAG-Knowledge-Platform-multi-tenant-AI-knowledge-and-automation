import asyncio
import uuid

import dramatiq
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import use_tenant, worker_sessions
from app.retrieval.indexing import build_and_switch
from app.worker import broker


async def reindex(
    index_id: uuid.UUID, org_id: uuid.UUID, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as session:
        await use_tenant(session, org_id, None)
        await build_and_switch(session, index_id)


@dramatiq.actor(broker=broker, queue_name="index", max_retries=3, min_backoff=10_000)
def reindex_kb(index_id: str, org_id: str) -> None:
    asyncio.run(reindex(uuid.UUID(index_id), uuid.UUID(org_id), worker_sessions()))
