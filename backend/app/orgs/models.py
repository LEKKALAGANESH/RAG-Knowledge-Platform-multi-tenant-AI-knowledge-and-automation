import uuid
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

Role = Literal["owner", "admin", "editor", "viewer"]
ROLES: tuple[Role, ...] = ("owner", "admin", "editor", "viewer")


class Org(IdMixin, Base):
    __tablename__ = "orgs"
    name: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)


class Membership(Base):
    __tablename__ = "memberships"
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[Role] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrgInvite(IdMixin, Base):
    """Pending until the invitee (matched by signed-in email) accepts or declines in the app."""

    __tablename__ = "org_invites"
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"))
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(Text)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
