import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from fastapi import Depends, Request, params
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey
from app.core.db import SessionLocal, use_tenant
from app.core.errors import AppError
from app.core.security import API_KEY_PREFIX, api_key_matches, decode_token, split_api_key
from app.orgs.models import Membership, Role

Scope = Literal[
    "kb:read",
    "kb:write",
    "documents:write",
    "search",
    "answer",
    "agent:run",
    "sources:read",
    "sources:write",
    "admin",
]
SCOPES: tuple[Scope, ...] = get_args(Scope)
LAST_USED_EVERY = timedelta(minutes=5)  # last_used_at is a hint, not a per-request write

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Tenant:
    session: AsyncSession
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: Role
    api_key_id: uuid.UUID | None = None
    scopes: frozenset[str] | None = None  # None = signed-in user: role-based access only
    ip: str = ""


def _invalid_key() -> AppError:
    return AppError(401, "invalid_api_key", "This API key is invalid, expired, or revoked.")


async def _resolve_api_key(raw: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, frozenset[str]]:
    """(key id, org id, user id, scopes) of a live key, else 401."""
    parts = split_api_key(raw)
    if parts is None:
        raise _invalid_key()
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        row = (await s.execute(text("SELECT * FROM api_key_lookup(:p)"), {"p": parts[0]})).first()
        if (
            row is None
            or not api_key_matches(parts[1], row.hash)
            or row.revoked_at is not None
            or (row.expires_at is not None and row.expires_at <= now)
        ):
            raise _invalid_key()
        if row.last_used_at is None or row.last_used_at < now - LAST_USED_EVERY:
            await use_tenant(s, row.org_id, row.user_id)
            await s.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
            await s.commit()
    return row.id, row.org_id, row.user_id, frozenset(row.scopes)


async def get_tenant(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    header_key: str | None = Depends(_api_key_header),
) -> AsyncIterator[Tenant]:
    """RLS-scoped session for the caller's org: a JWT access token, or an API key
    (`Authorization: Bearer rag_...` or `X-API-Key`). Endpoints must commit their own writes."""
    token = header_key or (creds.credentials if creds else None)
    if not token:
        raise AppError(401, "not_authenticated", "Sign in to continue.")
    key_id: uuid.UUID | None = None
    scopes: frozenset[str] | None = None
    if token.startswith(API_KEY_PREFIX):
        key_id, org_id, user_id, scopes = await _resolve_api_key(token)
    else:
        claims = decode_token(token, "access")
        org_id, user_id = claims.org, claims.sub
    async with SessionLocal() as session:
        await use_tenant(session, org_id, user_id)
        # An API key acts with its creator's CURRENT role, further capped by its scopes.
        role = await session.scalar(
            select(Membership.role).where(
                Membership.org_id == org_id, Membership.user_id == user_id
            )
        )
        if role is None:  # membership revoked after the token/key was issued
            raise AppError(403, "forbidden", "You no longer have access to this organization.")
        ip = request.client.host if request.client else ""
        yield Tenant(session, user_id, org_id, role, key_id, scopes, ip)


def require_role(*allowed: Role) -> Callable[..., Awaitable[Tenant]]:
    async def dep(tenant: Tenant = Depends(get_tenant)) -> Tenant:
        if tenant.role not in allowed:
            raise AppError(403, "forbidden", "Your role does not allow this action.")
        return tenant

    return dep


class Need:
    """Route scope declaration. None = any authenticated caller. Signed-in users pass (their
    role is checked by the route); an API key must carry the scope."""

    def __init__(self, scope: Scope | None) -> None:
        self.scope = scope

    async def __call__(self, t: Tenant = Depends(get_tenant)) -> None:
        if self.scope is not None and t.scopes is not None and self.scope not in t.scopes:
            raise AppError(
                403, "insufficient_scope", f"This API key lacks the '{self.scope}' scope."
            )


def need(scope: Scope | None) -> params.Depends:
    return params.Depends(Need(scope))


EDITORS: tuple[Role, ...] = ("owner", "admin", "editor")
