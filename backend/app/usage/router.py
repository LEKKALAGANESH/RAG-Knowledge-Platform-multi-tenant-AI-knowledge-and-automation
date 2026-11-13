"""Usage and cost reporting (AC12, MASTER_PROMPT §20). Owners/admins see the whole org;
other roles see only their own calls. Times are UTC; `to` is exclusive."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, SQLColumnExpression, Text, cast, func, select

from app.auth.models import User
from app.core.csv_export import csv_text
from app.core.db import SessionLocal, use_tenant
from app.core.errors import AppError
from app.core.tenancy import Tenant, get_tenant, need
from app.llm.usage import UsageEvent
from app.usage.budget import month_start

router = APIRouter(tags=["usage"])
GroupBy = Literal["day", "kind", "model", "user"]
MAX_SPAN = timedelta(days=366)
CSV_FIELDS = (
    "created_at", "kind", "provider", "model", "user_id", "in_tok", "out_tok", "cost_usd",
    "request_id", "id",
)  # fmt: skip


class Totals(BaseModel):
    in_tok: int
    out_tok: int
    cost_usd: Decimal
    calls: int
    unpriced_calls: int  # cost unknown (model not in the price registry): not in cost_usd


class Bucket(Totals):
    key: str | None  # day "YYYY-MM-DD" | kind | model | user id (None = background jobs)
    label: str | None = None  # user email for group_by=user


class UsageOut(BaseModel):
    start: datetime
    end: datetime
    group_by: GroupBy
    scope: Literal["org", "self"]
    totals: Totals
    series: list[Bucket]


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    start = start or month_start(end)
    if start.tzinfo is None or end.tzinfo is None:
        raise AppError(422, "invalid_range", "from and to need a timezone (e.g. ...Z).")
    if not start < end or end - start > MAX_SPAN:
        raise AppError(422, "invalid_range", "from must be before to, at most 366 days apart.")
    return start, end


def _filtered[*Ts](stmt: Select[*Ts], t: Tenant, start: datetime, end: datetime) -> Select[*Ts]:
    stmt = stmt.where(
        UsageEvent.org_id == t.org_id, UsageEvent.created_at >= start, UsageEvent.created_at < end
    )
    return stmt if _sees_org(t) else stmt.where(UsageEvent.user_id == t.user_id)


def _sees_org(t: Tenant) -> bool:
    return t.role in ("owner", "admin")


def _group_key(group_by: GroupBy) -> SQLColumnExpression[str]:
    if group_by == "day":
        day = func.date_trunc("day", func.timezone("UTC", UsageEvent.created_at))
        return func.to_char(day, "YYYY-MM-DD")
    if group_by == "user":
        return cast(UsageEvent.user_id, Text)
    return UsageEvent.kind if group_by == "kind" else UsageEvent.model


AGGREGATES = (
    func.coalesce(func.sum(UsageEvent.in_tok), 0),
    func.coalesce(func.sum(UsageEvent.out_tok), 0),
    func.coalesce(func.sum(UsageEvent.cost_usd), 0),
    func.count(),
    func.count().filter(UsageEvent.cost_usd.is_(None)),
)


def _totals(row: tuple[object, ...]) -> dict[str, object]:
    return dict(zip(("in_tok", "out_tok", "cost_usd", "calls", "unpriced_calls"), row, strict=True))


@router.get("/usage", dependencies=[need("admin")])
async def usage(
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    group_by: GroupBy = "day",
    t: Tenant = Depends(get_tenant),
) -> UsageOut:
    start, end = _range(start, end)
    total = (await t.session.execute(_filtered(select(*AGGREGATES), t, start, end))).one()
    key = _group_key(group_by)
    rows = (
        await t.session.execute(
            _filtered(select(key, *AGGREGATES), t, start, end).group_by(key).order_by(key)
        )
    ).all()
    labels: dict[str, str] = {}
    if group_by == "user":
        ids = [uuid.UUID(str(r[0])) for r in rows if r[0] is not None]
        found = await t.session.execute(select(User.id, User.email).where(User.id.in_(ids)))
        labels = {str(i): e for i, e in found}
    series = [
        Bucket.model_validate({"key": k, "label": labels.get(str(k)), **_totals(tuple(rest))})
        for k, *rest in rows
    ]
    return UsageOut(
        start=start, end=end, group_by=group_by, scope="org" if _sees_org(t) else "self",
        totals=Totals.model_validate(_totals(tuple(total))), series=series,
    )  # fmt: skip


async def _export(t: Tenant, start: datetime, end: datetime) -> AsyncIterator[str]:
    yield csv_text([list(CSV_FIELDS)])
    cols = [getattr(UsageEvent, f) for f in CSV_FIELDS]
    stmt = _filtered(select(*cols), t, start, end).order_by(UsageEvent.created_at)
    async with SessionLocal() as s:  # own session: the request's closes before streaming ends
        await use_tenant(s, t.org_id, t.user_id)
        result = await s.stream(stmt.execution_options(yield_per=1000))
        async for part in result.partitions():
            yield csv_text([list(r) for r in part])


@router.get("/usage/export", dependencies=[need("admin")], response_class=StreamingResponse)
async def export_usage(
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    t: Tenant = Depends(get_tenant),
) -> StreamingResponse:
    start, end = _range(start, end)
    return StreamingResponse(
        _export(t, start, end),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
    )
