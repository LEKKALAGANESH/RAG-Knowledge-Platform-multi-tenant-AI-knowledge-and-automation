import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import JsonValue
from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

SourceStatus = Literal["idle", "syncing", "error", "disabled"]
RunStatus = Literal["running", "succeeded", "failed", "partial"]


class Source(IdMixin, Base):
    __tablename__ = "sources"
    org_id: Mapped[uuid.UUID]
    kb_id: Mapped[uuid.UUID]
    type: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet, core/crypto only
    status: Mapped[SourceStatus] = mapped_column(Text, default="idle")
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)  # last failed/partial run, user-safe text
    needs_reconnect: Mapped[bool] = mapped_column(Boolean, default=False)  # OAuth grant is gone


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID]
    source_id: Mapped[uuid.UUID]
    status: Mapped[RunStatus] = mapped_column(Text, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
