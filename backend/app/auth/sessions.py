"""Refresh-token sessions: rotation, a reuse grace window, theft detection, revocation."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record
from app.auth.models import AuthSession
from app.core.config import get_settings
from app.core.db import use_tenant
from app.core.errors import AppError
from app.core.security import TokenClaims, TokenPair, issue_tokens, keyed_hash
from app.orgs.models import Membership

log = logging.getLogger("app.auth")


class SessionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    # Expired or revoked sessions rows are deleted this many days later (scheduler tick).
    session_retention_days: int = Field(default=30, gt=0)


@lru_cache
def get_session_settings() -> SessionSettings:
    return SessionSettings()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _revoked() -> AppError:
    return AppError(401, "session_revoked", "Your session has ended. Sign in again.")


def pair(row: AuthSession) -> TokenPair:
    return issue_tokens(row.user_id, row.org_id, row.id, row.created_at)


def start(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    request: Request,
    parent: AuthSession | None = None,
) -> AuthSession:
    """New sessions row (a new family on login, the successor of `parent` on refresh)."""
    now = datetime.now(UTC)
    ip, ua = _ip(request), request.headers.get("user-agent", "")
    row = AuthSession(
        id=uuid.uuid4(), org_id=org_id, user_id=user_id,
        family_id=parent.family_id if parent else uuid.uuid4(),
        parent_id=parent.id if parent else None, created_at=now,
        expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
        ip_hash=keyed_hash(f"ip:{ip}") if ip else None,
        user_agent_hash=keyed_hash(f"ua:{ua}") if ua else None,
    )  # fmt: skip
    db.add(row)
    return row


async def _row(db: AsyncSession, claims: TokenClaims) -> AuthSession:
    """The token's sessions row, locked: parallel refreshes of one token run one at a time."""
    try:
        sid = uuid.UUID(hex=claims.jti)
    except ValueError as exc:  # pre-0008 tokens carry a random jti: sign in again
        raise _revoked() from exc
    await use_tenant(db, None, claims.sub)
    row = await db.get(AuthSession, sid, with_for_update=True, populate_existing=True)
    if row is None or row.user_id != claims.sub:
        raise _revoked()
    return row


async def revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(
        update(AuthSession)
        .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def rotate(
    db: AsyncSession, claims: TokenClaims, org_id: uuid.UUID, request: Request
) -> TokenPair:
    row = await _row(db, claims)
    now = datetime.now(UTC)
    if row.revoked_at is not None or row.expires_at <= now:
        raise _revoked()
    if row.replaced_by is not None:
        return await _reused(db, row, now, request)
    member = await db.scalar(
        select(Membership.role).where(
            Membership.org_id == org_id, Membership.user_id == row.user_id
        )
    )
    if member is None:
        raise AppError(403, "forbidden", "You are not a member of that organization.")
    new = start(db, row.user_id, org_id, request, parent=row)
    await db.flush()  # the successor exists before replaced_by points at it
    row.replaced_by = new.id
    await db.commit()
    return pair(new)


async def _reused(db: AsyncSession, row: AuthSession, now: datetime, request: Request) -> TokenPair:
    """An already-rotated token. Inside the grace window it is a parallel refresh (same pair
    again, idempotent); after it, two parties hold the token, so the whole family is revoked."""
    nxt = await db.get(AuthSession, row.replaced_by)
    grace = timedelta(seconds=get_settings().refresh_reuse_grace_seconds)
    if nxt is not None and nxt.revoked_at is None and now - nxt.created_at <= grace:
        return pair(nxt)
    await revoke_family(db, row.family_id)
    await use_tenant(db, row.org_id, row.user_id)  # audit rows are org-scoped
    record(
        db, row.org_id, "session.reuse_detected", user_id=row.user_id, ip=_ip(request),
        target_type="sessions", target_id=row.id, meta={"family_id": str(row.family_id)},
    )  # fmt: skip
    await db.commit()
    log.warning(
        "refresh token reuse: session family revoked",
        extra={"user_id": str(row.user_id), "family_id": str(row.family_id)},
    )
    raise _revoked()


async def logout(db: AsyncSession, claims: TokenClaims) -> None:
    """Ends this login (its whole family). Already revoked or unknown: nothing to do."""
    try:
        row = await _row(db, claims)
    except AppError:
        return
    await revoke_family(db, row.family_id)
    await db.commit()


async def revoke_all(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Every live session of the user, in every org. Returns how many logins ended."""
    families = await db.scalars(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
        .returning(AuthSession.family_id)
    )
    return len(set(families))


async def prune(db: AsyncSession) -> int:
    """Deletes sessions rows dead for SESSION_RETENTION_DAYS (owner-run prune_sessions, 0012:
    per-user RLS hides them and rag_app has no DELETE). Returns how many rows went."""
    keep = timedelta(days=get_session_settings().session_retention_days)
    n = await db.scalar(text("SELECT prune_sessions(:keep)"), {"keep": keep})
    await db.commit()
    return int(n or 0)
