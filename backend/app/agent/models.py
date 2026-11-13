import uuid
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import DateTime, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin

RunStatus = Literal["planning", "running", "awaiting_approval", "succeeded", "failed", "cancelled"]
ACTIVE: tuple[RunStatus, ...] = ("planning", "running", "awaiting_approval")
StepKind = Literal["plan", "tool_call", "tool_result", "message"]
Risk = Literal["read", "reversible_write", "irreversible_write"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class AgentRun(IdMixin, Base):
    __tablename__ = "agent_runs"
    org_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    goal: Mapped[str] = mapped_column(Text)
    kb_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    status: Mapped[RunStatus] = mapped_column(Text, default="planning")
    max_steps: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentStep(IdMixin, Base):
    __tablename__ = "agent_steps"
    org_id: Mapped[uuid.UUID]
    run_id: Mapped[uuid.UUID]
    n: Mapped[int] = mapped_column(Integer)
    kind: Mapped[StepKind] = mapped_column(Text)
    tool: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    risk: Mapped[Risk | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Approval(IdMixin, Base):
    __tablename__ = "approvals"
    org_id: Mapped[uuid.UUID]
    run_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]  # the run's owner; the worker resumes the run as this user
    step_id: Mapped[uuid.UUID]
    tool: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    status: Mapped[ApprovalStatus] = mapped_column(Text, default="pending")
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(Text)


class Report(IdMixin, Base):
    __tablename__ = "reports"
    org_id: Mapped[uuid.UUID]
    run_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    blob_key: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
