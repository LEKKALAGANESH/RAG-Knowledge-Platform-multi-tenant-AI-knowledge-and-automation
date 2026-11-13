import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.audit.service import emit
from app.auth.models import ApiKey
from app.core.errors import AppError, not_found
from app.core.security import new_api_key
from app.core.tenancy import Scope, Tenant, need, require_role

router = APIRouter(tags=["api-keys"])
ADMINS = require_role("owner", "admin")


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    user_id: uuid.UUID
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Shown once. Only a hash is stored; it cannot be shown again.")


@router.post("/api-keys", status_code=201, dependencies=[need("admin")])
async def create_api_key(body: ApiKeyIn, t: Tenant = Depends(ADMINS)) -> ApiKeyCreated:
    scopes = sorted(set(body.scopes))
    # A key may only mint keys narrower than itself, never escalate.
    if t.scopes is not None and not set(scopes) <= t.scopes:
        raise AppError(403, "insufficient_scope", "A key cannot grant scopes it does not have.")
    if body.expires_at is not None and (
        body.expires_at.tzinfo is None or body.expires_at <= datetime.now(UTC)
    ):
        raise AppError(422, "invalid_expiry", "expires_at must be a future time with a timezone.")
    plaintext, prefix, digest = new_api_key()
    key = ApiKey(
        id=uuid.uuid4(), org_id=t.org_id, user_id=t.user_id, name=body.name, prefix=prefix,
        hash=digest, scopes=scopes, expires_at=body.expires_at,
    )  # fmt: skip
    t.session.add(key)
    await emit(t, "api_key.create", key, {"scopes": list(scopes)})
    await t.session.commit()
    await t.session.refresh(key)
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=plaintext)


@router.get("/api-keys", dependencies=[need("admin")])
async def list_api_keys(t: Tenant = Depends(ADMINS)) -> list[ApiKeyOut]:
    rows = await t.session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()).limit(500))
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.delete("/api-keys/{key_id}", status_code=204, dependencies=[need("admin")])
async def revoke_api_key(key_id: uuid.UUID, t: Tenant = Depends(ADMINS)) -> Response:
    """Immediate: every request re-checks revoked_at. Revoking twice is a no-op."""
    key = await t.session.get(ApiKey, key_id)  # RLS hides other orgs' keys -> 404
    if key is None:
        raise not_found("API key")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await emit(t, "api_key.revoke", key)
        await t.session.commit()
    return Response(status_code=204)
