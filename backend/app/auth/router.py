import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import emit, record
from app.auth import sessions
from app.auth.models import User
from app.core.db import get_session, use_tenant
from app.core.errors import AppError
from app.core.ratelimit import by_ip
from app.core.security import TokenPair, decode_token, hash_password, verify_password
from app.core.tenancy import Tenant, get_tenant, need
from app.orgs.models import Membership, Org, Role

router = APIRouter(tags=["auth"])
log = logging.getLogger("app.auth")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str
    org_id: uuid.UUID | None = Field(
        default=None, description="Switch to this org (must be a member)."
    )


class Ref(BaseModel):
    id: uuid.UUID
    name: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str


class MeOut(BaseModel):
    user: UserOut
    org: Ref
    role: Role


@router.post("/auth/register", status_code=201, dependencies=[by_ip("auth")])
async def register(
    body: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    email = body.email.lower()
    user = User(id=uuid.uuid4(), email=email, pw_hash=hash_password(body.password))
    org = Org(id=uuid.uuid4(), name=f"{email.split('@')[0]}'s workspace", settings={})
    await use_tenant(session, org.id, user.id)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # users.email is the only unique key a fresh insert can hit
        raise AppError(409, "email_taken", "An account with this email already exists.") from exc
    # No relationship() mappings, so the unit of work won't order by FK: flush parents first.
    session.add(org)
    await session.flush()
    session.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    await session.flush()
    record(session, org.id, "auth.register", user_id=user.id, ip=_ip(request))
    row = sessions.start(session, user.id, org.id, request)
    await session.commit()
    return sessions.pair(row)


@router.post("/auth/login", dependencies=[by_ip("auth")])
async def login(
    body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    # users RLS hides every row before a user context exists: owner-run lookup (0010).
    user = (
        await session.execute(
            text("SELECT * FROM auth_user_by_email(:e)"), {"e": body.email.lower()}
        )
    ).first()
    if not verify_password(body.password, user.pw_hash if user else None) or user is None:
        # No org is known for a failed login, so it goes to logs, not a tenant's audit table.
        # The user id (never the email) only when the account exists.
        log.warning("login failed", extra={"user_id": str(user.id) if user else None})
        raise AppError(401, "invalid_credentials", "Email or password is incorrect.")
    await use_tenant(session, None, user.id)
    org_id = await session.scalar(
        select(Membership.org_id)
        .where(Membership.user_id == user.id)
        .order_by(Membership.created_at)
        .limit(1)
    )
    if org_id is None:
        raise AppError(403, "no_org", "Your account has no organization. Ask an admin to add you.")
    await use_tenant(session, org_id, user.id)
    record(session, org_id, "auth.login", user_id=user.id, ip=_ip(request))
    row = sessions.start(session, user.id, org_id, request)
    await session.commit()
    return sessions.pair(row)


@router.post("/auth/refresh")
async def refresh(
    body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    """Rotates the refresh token. The same token sent again within the grace window (parallel
    refresh) returns the same new pair; later reuse revokes the whole login (401)."""
    claims = decode_token(body.refresh_token, "refresh")
    return await sessions.rotate(session, claims, body.org_id or claims.org, request)


class LogoutIn(BaseModel):
    refresh_token: str


@router.post("/auth/logout", status_code=204)
async def logout(body: LogoutIn, session: AsyncSession = Depends(get_session)) -> Response:
    """Ends this login. Its access token stays valid until it expires (TTL, 15 min default)."""
    await sessions.logout(session, decode_token(body.refresh_token, "refresh"))
    return Response(status_code=204)


class LogoutAllOut(BaseModel):
    revoked: int = Field(description="Logins ended (each device/browser is one).")


@router.post("/auth/logout-all", dependencies=[need(None)])
async def logout_all(t: Tenant = Depends(get_tenant)) -> LogoutAllOut:
    """Signs the user out everywhere, in every org. Access tokens already issued expire on
    their own (TTL); no refresh succeeds after this."""
    if t.api_key_id is not None:
        raise AppError(403, "forbidden", "Sign in as the user to end their sessions.")
    n = await sessions.revoke_all(t.session, t.user_id)
    await emit(t, "auth.logout_all", meta={"revoked": n})
    await t.session.commit()
    return LogoutAllOut(revoked=n)


@router.get("/me", dependencies=[need(None)])
async def me(t: Tenant = Depends(get_tenant)) -> MeOut:
    user = await t.session.get_one(User, t.user_id)
    org = await t.session.get_one(Org, t.org_id)
    return MeOut(
        user=UserOut(id=user.id, email=user.email), org=Ref(id=org.id, name=org.name), role=t.role
    )
