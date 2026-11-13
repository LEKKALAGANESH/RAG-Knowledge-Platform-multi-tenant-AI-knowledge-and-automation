import uuid
from decimal import Decimal
from typing import Literal

from sqlalchemy import Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin
from app.core.errors import request_id_var
from app.llm.providers import Usage
from app.llm.registry import cost_usd

UsageKind = Literal["chat", "embed", "rewrite", "agent"]


class UsageEvent(IdMixin, Base):
    __tablename__ = "usage_events"
    org_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    in_tok: Mapped[int] = mapped_column(Integer)
    out_tok: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    request_id: Mapped[str | None] = mapped_column(Text)


def usage_cost(u: Usage) -> Decimal | None:
    """Sum of per-attempt costs (each at its own model's rates); None if any price is unknown."""
    if not u.attempts:
        return cost_usd(u.provider, u.model, u.in_tok, u.out_tok)
    total = Decimal(0)
    for model, in_tok, out_tok in u.attempts:
        cost = cost_usd(u.provider, model, in_tok, out_tok)
        if cost is None:
            return None
        total += cost
    return total


def usage_event(
    org_id: uuid.UUID, user_id: uuid.UUID | None, kind: UsageKind, u: Usage
) -> UsageEvent:
    return UsageEvent(
        org_id=org_id,
        user_id=user_id,
        kind=kind,
        provider=u.provider,
        model=u.model,
        in_tok=u.in_tok,
        out_tok=u.out_tok,
        cost_usd=usage_cost(u),
        request_id=request_id_var.get(),
    )
