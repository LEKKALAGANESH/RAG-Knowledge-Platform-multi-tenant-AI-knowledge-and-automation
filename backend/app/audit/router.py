import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import Select, select, tuple_

from app.audit.models import AuditLog
from app.core.csv_export import csv_text
from app.core.db import SessionLocal, use_tenant
from app.core.errors import AppError
from app.core.tenancy import Tenant, need, require_role

router = APIRouter(tags=["audit"])
ADMINS = require_role("owner", "admin")
EXPORT_BATCH = 1000
CSV_FIELDS = (
    "created_at", "action", "actor_user_id", "actor_email", "actor_api_key_id", "target_type",
    "target_id", "request_id", "ip_hash", "meta", "id",
)  # fmt: skip


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    action: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    actor_api_key_id: uuid.UUID | None
    target_type: str | None
    target_id: uuid.UUID | None
    request_id: str | None
    ip_hash: str | None
    meta: dict[str, JsonValue]
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: str | None


def _cursor(row: AuditLog) -> str:
    return base64.urlsafe_b64encode(f"{row.created_at.isoformat()}|{row.id}".encode()).decode()


def _query(action: str | None, cursor: str | None) -> Select[AuditLog]:
    """Newest first, keyset on (created_at, id). RLS limits rows to the caller's org."""
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if cursor:
        try:
            at, _, raw_id = base64.urlsafe_b64decode(cursor).decode().partition("|")
            key = (datetime.fromisoformat(at), uuid.UUID(raw_id))
        except ValueError as exc:
            raise AppError(422, "invalid_cursor", "The pagination cursor is invalid.") from exc
        stmt = stmt.where(tuple_(AuditLog.created_at, AuditLog.id) < key)
    return stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


@router.get("/audit", dependencies=[need("admin")])
async def list_audit(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    action: str | None = Query(default=None, max_length=64),
    t: Tenant = Depends(ADMINS),
) -> AuditPage:
    rows = list((await t.session.scalars(_query(action, cursor).limit(limit + 1))).all())
    nxt = _cursor(rows[limit - 1]) if len(rows) > limit else None
    return AuditPage(items=[AuditOut.model_validate(r) for r in rows[:limit]], next_cursor=nxt)


def _csv_row(row: AuditLog) -> list[object]:
    d = AuditOut.model_validate(row).model_dump(mode="json")
    d["meta"] = json.dumps(d["meta"])
    return [d[f] for f in CSV_FIELDS]


async def _export(org: uuid.UUID, user: uuid.UUID, action: str | None) -> AsyncIterator[str]:
    yield csv_text([list(CSV_FIELDS)])
    cursor: str | None = None
    async with SessionLocal() as s:  # own session: the request's closes before streaming ends
        await use_tenant(s, org, user)
        while True:
            rows = list((await s.scalars(_query(action, cursor).limit(EXPORT_BATCH))).all())
            if not rows:
                return
            yield csv_text([_csv_row(r) for r in rows])
            cursor = _cursor(rows[-1])


@router.get("/audit/export", dependencies=[need("admin")], response_class=StreamingResponse)
async def export_audit(
    action: str | None = Query(default=None, max_length=64), t: Tenant = Depends(ADMINS)
) -> StreamingResponse:
    return StreamingResponse(
        _export(t.org_id, t.user_id, action),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )
