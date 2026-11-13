import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, SessionTransaction, mapped_column
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


engine = create_async_engine(
    get_settings().database_url.get_secret_value(), pool_pre_ping=True, pool_size=10
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_SET_TENANT = text(
    "SELECT set_config('app.org_id', :org, true), set_config('app.user_id', :user, true)"
)


def _tenant_params(session: Session) -> dict[str, str] | None:
    tenant = session.info.get("tenant")
    if tenant is None:
        return None
    org_id, user_id = tenant
    return {"org": str(org_id or ""), "user": str(user_id or "")}


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, _tx: SessionTransaction, conn: Connection) -> None:
    # Every transaction re-applies SET LOCAL, so a commit mid-request never drops the tenant.
    params = _tenant_params(session)
    if params is not None:
        conn.execute(_SET_TENANT, params)


async def use_tenant(
    session: AsyncSession, org_id: uuid.UUID | None, user_id: uuid.UUID | None
) -> None:
    """Scope this session to an org/user for RLS; applies immediately and to later transactions."""
    session.info["tenant"] = (org_id, user_id)
    if session.in_transaction():
        await session.execute(_SET_TENANT, _tenant_params(session.sync_session))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


_worker_local = threading.local()


def worker_sessions() -> async_sessionmaker[AsyncSession]:
    """Session factory for Dramatiq actors, one engine per worker thread.

    An engine guards its first connect with an asyncio.Lock; a job waiting on it from another
    thread's loop is never woken, so a shared engine hung one of 2+ simultaneous first jobs.
    NullPool: each task runs in its own asyncio.run loop; asyncpg connections can't cross loops.
    """
    sessions: async_sessionmaker[AsyncSession] | None = getattr(_worker_local, "sessions", None)
    if sessions is None:
        url = get_settings().database_url.get_secret_value()
        engine = create_async_engine(url, poolclass=NullPool)
        sessions = _worker_local.sessions = async_sessionmaker(engine, expire_on_commit=False)
    return sessions
