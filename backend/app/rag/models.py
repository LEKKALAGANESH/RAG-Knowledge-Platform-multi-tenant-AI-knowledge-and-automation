import uuid

from pydantic import JsonValue
from sqlalchemy import Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class Conversation(IdMixin, Base):
    __tablename__ = "conversations"
    org_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    kb_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)  # [] = all KBs
    title: Mapped[str] = mapped_column(Text)


class Message(IdMixin, Base):
    __tablename__ = "messages"
    org_id: Mapped[uuid.UUID]
    conversation_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[JsonValue]] = mapped_column(JSONB, default=list)
    usage: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
