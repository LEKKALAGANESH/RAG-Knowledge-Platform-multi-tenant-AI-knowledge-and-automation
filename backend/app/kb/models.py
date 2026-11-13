import uuid

from pydantic import JsonValue
from sqlalchemy import Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class KnowledgeBase(IdMixin, Base):
    __tablename__ = "knowledge_bases"
    org_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    embedding_index_id: Mapped[uuid.UUID | None]


class Document(IdMixin, Base):
    __tablename__ = "documents"
    org_id: Mapped[uuid.UUID]
    kb_id: Mapped[uuid.UUID]
    source_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    blob_key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Text, default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[JsonValue]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    external_key: Mapped[str | None] = mapped_column(Text)  # connector item key (0004)
    content_hash: Mapped[str | None] = mapped_column(Text)


class Chunk(IdMixin, Base):
    __tablename__ = "chunks"
    org_id: Mapped[uuid.UUID]
    kb_id: Mapped[uuid.UUID]
    document_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID | None]
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    loc: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
