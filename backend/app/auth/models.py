import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin


class User(IdMixin, Base):
    """Global (not tenant-owned): one login spans orgs via memberships. RLS shows a session its
    own row and its current org's members; pw_hash is not readable by the app role (0010), so it
    is deferred (write-only here) and login reads it via auth_user_by_email()."""

    __tablename__ = "users"
    email: Mapped[str] = mapped_column(Text, unique=True)
    pw_hash: Mapped[str] = mapped_column(Text, deferred=True)


class ApiKey(IdMixin, Base):
    __tablename__ = "api_keys"
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(Text, unique=True)
    hash: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    """One refresh token. A login starts a family; each refresh adds a row (parent -> replaced_by).
    RLS is per user (app.user_id), not per org: one login spans the orgs it switches between."""

    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    family_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID | None]
    replaced_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_hash: Mapped[str | None] = mapped_column(Text)
    user_agent_hash: Mapped[str | None] = mapped_column(Text)
