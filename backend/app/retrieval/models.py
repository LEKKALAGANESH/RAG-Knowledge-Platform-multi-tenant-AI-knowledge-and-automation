import uuid
from typing import Literal

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

IndexStatus = Literal["building", "active", "retired"]


class EmbeddingIndex(IdMixin, Base):
    __tablename__ = "embedding_indexes"
    org_id: Mapped[uuid.UUID]
    kb_id: Mapped[uuid.UUID]
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[IndexStatus] = mapped_column(Text, default="building")
