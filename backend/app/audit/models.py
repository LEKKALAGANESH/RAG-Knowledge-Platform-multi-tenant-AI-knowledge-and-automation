import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditLog(Base):
    """Append-only: rag_app has SELECT + INSERT only (0007)."""

    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    actor_user_id: Mapped[uuid.UUID | None]
    actor_api_key_id: Mapped[uuid.UUID | None]
    actor_email: Mapped[str | None] = mapped_column(Text)  # snapshot at write time (0012)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None]
    request_id: Mapped[str | None] = mapped_column(Text)
    ip_hash: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
