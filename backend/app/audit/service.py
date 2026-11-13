import logging
import uuid

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditLog
from app.auth.models import User
from app.core.errors import request_id_var
from app.core.security import keyed_hash
from app.core.tenancy import Tenant

log = logging.getLogger("app.audit")


def record(
    session: AsyncSession,
    org_id: uuid.UUID,
    action: str,
    *,
    user_id: uuid.UUID | None,
    ip: str,
    api_key_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    meta: dict[str, JsonValue] | None = None,
) -> None:
    """Adds an audit row to the caller's transaction, so it commits (or not) with the action.
    meta holds ids and field names only: never secrets, emails, or document text. The actor's
    email is snapshotted into actor_email (read in the same INSERT under the caller's RLS, which
    always shows its own user row) so the trail stays readable after the member is removed."""
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            org_id=org_id,
            actor_user_id=user_id,
            actor_email=select(User.email).where(User.id == user_id).scalar_subquery()
            if user_id
            else None,
            actor_api_key_id=api_key_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            request_id=request_id_var.get(),
            ip_hash=keyed_hash(f"ip:{ip}") if ip else None,
            meta=meta or {},
        )
    )


async def emit(
    t: Tenant,
    action: str,
    target: object = None,
    meta: dict[str, JsonValue] | None = None,
) -> None:
    """record() for a Tenant request. `target` is an ORM row; a new row is flushed for its id."""
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    if target is not None:
        if getattr(target, "id", None) is None:
            await t.session.flush()
        target_type = getattr(target, "__tablename__", None)
        target_id = getattr(target, "id", None)
    record(
        t.session, t.org_id, action, user_id=t.user_id, ip=t.ip, api_key_id=t.api_key_id,
        target_type=target_type, target_id=target_id, meta=meta,
    )  # fmt: skip
