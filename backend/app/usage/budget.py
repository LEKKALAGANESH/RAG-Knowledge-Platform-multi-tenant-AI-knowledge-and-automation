"""Per-org monthly AI budget, checked before every LLM and embedding call (MASTER_PROMPT §20).
Over budget never switches to a cheaper model: `block` refuses with a readable error, `warn`
proceeds and flags it."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import Depends, Response
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.tenancy import Tenant, get_tenant
from app.llm.usage import UsageEvent
from app.orgs.models import Org

log = logging.getLogger("app.usage")
BudgetAction = Literal["block", "warn"]
WARNING_HEADER = "X-Budget-Warning"


class BudgetExceeded(AppError):
    def __init__(self, spent: Decimal, limit: Decimal) -> None:
        super().__init__(
            402,
            "budget_exceeded",
            f"This organization has used ${spent:.2f} of its ${limit:.2f} monthly AI budget. "
            "An owner or admin can raise the budget in Settings; it resets on the 1st (UTC).",
        )


def month_start(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def budget_of(settings: dict[str, JsonValue]) -> tuple[Decimal | None, BudgetAction]:
    raw = settings.get("monthly_budget_usd")
    action: BudgetAction = "warn" if settings.get("budget_action") == "warn" else "block"
    return (None if raw is None else Decimal(str(raw))), action


async def month_spend(db: AsyncSession, org_id: uuid.UUID) -> Decimal:
    """This UTC month's priced usage. Rows with an unknown price (NULL cost) count as 0."""
    total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.org_id == org_id, UsageEvent.created_at >= month_start()
        )
    )
    return Decimal(0) if total is None else total


async def check_budget(db: AsyncSession, org_id: uuid.UUID) -> bool:
    """Call right before a model call. Raises BudgetExceeded (402) when over a `block` budget;
    returns True when over a `warn` budget (the caller flags it), else False."""
    settings = await db.scalar(select(Org.settings).where(Org.id == org_id))
    limit, action = budget_of(settings or {})
    if limit is None:
        return False
    spent = await month_spend(db, org_id)
    if spent < limit:
        return False
    if action == "block":
        raise BudgetExceeded(spent, limit)
    log.warning(
        "monthly AI budget exceeded (warn)",
        extra={"org_id": str(org_id), "spent_usd": str(spent), "budget_usd": str(limit)},
    )
    return True


async def within_budget(response: Response, t: Tenant = Depends(get_tenant)) -> None:
    """Route dependency for endpoints that start model calls: 402 before any provider work (and
    before the 503 llm_not_configured check). Warn mode sets the X-Budget-Warning header on
    JSON responses; streaming routes flag it in their own events."""
    if await check_budget(t.session, t.org_id):
        response.headers[WARNING_HEADER] = "over_budget"
