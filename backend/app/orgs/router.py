import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, EmailStr, Field, JsonValue
from sqlalchemy import ColumnElement, select, text, update

from app.audit.service import emit, record
from app.auth.models import User
from app.core.db import use_tenant
from app.core.errors import AppError, not_found
from app.core.tenancy import Tenant, get_tenant, need, require_role
from app.orgs.models import Membership, Org, OrgInvite, Role
from app.usage.budget import BudgetAction, budget_of, month_spend

router = APIRouter(prefix="/orgs", tags=["orgs"])
invites_router = APIRouter(prefix="/invites", tags=["orgs"])
INVITE_TTL = timedelta(days=7)
ADMINS = require_role("owner", "admin")


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    role: Role
    created_at: datetime


class MemberIn(BaseModel):
    email: EmailStr
    role: Role


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: Role


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    created_at: datetime
    expires_at: datetime


@router.get("", dependencies=[need("admin")])
async def list_orgs(t: Tenant = Depends(get_tenant)) -> list[OrgOut]:
    rows = await t.session.execute(
        select(Org, Membership.role)
        .join(Membership, Membership.org_id == Org.id)
        .where(Membership.user_id == t.user_id)
        .order_by(Org.created_at)
    )
    return [OrgOut(id=o.id, name=o.name, role=r, created_at=o.created_at) for o, r in rows]


@router.post("", status_code=201, dependencies=[need("admin")])
async def create_org(body: OrgIn, t: Tenant = Depends(get_tenant)) -> OrgOut:
    org = Org(id=uuid.uuid4(), name=body.name, settings={})
    await use_tenant(t.session, org.id, t.user_id)
    t.session.add(org)
    await t.session.flush()  # parent first: no relationship() to order the inserts
    t.session.add(Membership(org_id=org.id, user_id=t.user_id, role="owner"))
    await t.session.commit()
    await t.session.refresh(org)
    return OrgOut(id=org.id, name=org.name, role="owner", created_at=org.created_at)


def _same_org(org_id: uuid.UUID, t: Tenant) -> None:
    if org_id != t.org_id:  # another org's id must look the same as a nonexistent one
        raise not_found("Organization")


def _pending(now: datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        OrgInvite.accepted_at.is_(None),
        OrgInvite.declined_at.is_(None),
        OrgInvite.revoked_at.is_(None),
        OrgInvite.expires_at > now,
    )


def _invite_out(i: OrgInvite) -> InviteOut:
    return InviteOut(
        id=i.id, email=i.email, role=i.role, created_at=i.created_at, expires_at=i.expires_at
    )


@router.post("/{org_id}/members", status_code=201, dependencies=[need("admin")])
async def invite_member(
    org_id: uuid.UUID, body: MemberIn, t: Tenant = Depends(ADMINS)
) -> InviteOut:
    """Creates a pending invite; the person joins only when they accept it, signed in with this
    email. Same answer whether or not an account exists (no email oracle). A newer invite to the
    same email replaces the pending one."""
    _same_org(org_id, t)
    if body.role == "owner" and t.role != "owner":
        raise AppError(403, "forbidden", "Only an owner can add another owner.")
    email = body.email.lower()
    # users RLS shows only this org's members here, so this reveals nothing the list doesn't.
    member = await t.session.scalar(
        select(Membership.user_id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id, User.email == email)
    )
    if member is not None:
        raise AppError(409, "already_member", "This user is already a member.")
    now = datetime.now(UTC)
    await t.session.execute(
        update(OrgInvite).where(OrgInvite.email == email, *_pending(now)).values(revoked_at=now)
    )
    invite = OrgInvite(
        id=uuid.uuid4(), org_id=org_id, email=email, role=body.role, invited_by=t.user_id,
        created_at=now, expires_at=now + INVITE_TTL,
    )  # fmt: skip
    t.session.add(invite)
    await emit(t, "invite.create", invite, {"role": body.role})
    await t.session.commit()
    return _invite_out(invite)


@router.get("/{org_id}/invites", dependencies=[need("admin")])
async def list_invites(org_id: uuid.UUID, t: Tenant = Depends(ADMINS)) -> list[InviteOut]:
    """Pending, unexpired invites of this org."""
    _same_org(org_id, t)
    rows = await t.session.scalars(
        select(OrgInvite).where(*_pending(datetime.now(UTC))).order_by(OrgInvite.created_at)
    )
    return [_invite_out(i) for i in rows]


@router.delete("/{org_id}/invites/{invite_id}", status_code=204, dependencies=[need("admin")])
async def revoke_invite(
    org_id: uuid.UUID, invite_id: uuid.UUID, t: Tenant = Depends(ADMINS)
) -> Response:
    _same_org(org_id, t)
    now = datetime.now(UTC)
    invite = await t.session.scalar(
        select(OrgInvite).where(OrgInvite.id == invite_id, *_pending(now)).with_for_update()
    )
    if invite is None:
        raise not_found("Invite")
    if invite.role == "owner" and t.role != "owner":
        raise AppError(403, "forbidden", "Only an owner can revoke an owner invite.")
    invite.revoked_at = now
    await emit(t, "invite.revoke", invite, {"role": invite.role})
    await t.session.commit()
    return Response(status_code=204)


# Every role reads the member list (role changes stay admin-only); API keys need `admin`.
@router.get("/{org_id}/members", dependencies=[need("admin")])
async def list_members(org_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> list[MemberOut]:
    _same_org(org_id, t)
    rows = await t.session.execute(
        select(Membership.user_id, User.email, Membership.role)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id)
        .order_by(Membership.created_at)
    )
    return [MemberOut(user_id=u, email=e, role=r) for u, e, r in rows]


class RoleIn(BaseModel):
    role: Role


async def _target(t: Tenant, user_id: uuid.UUID, new_role: Role | None) -> Membership:
    """The member to change, after the owner rules: only an owner grants or takes away owner,
    and the org always keeps one. Owner rows are locked so two demotions cannot race past it."""
    owners = list(
        await t.session.scalars(
            select(Membership.user_id)
            .where(Membership.org_id == t.org_id, Membership.role == "owner")
            .with_for_update()
        )
    )
    m = await t.session.get(Membership, (t.org_id, user_id), populate_existing=True)
    if m is None:
        raise not_found("Member")
    if (m.role == "owner" or new_role == "owner") and t.role != "owner":
        raise AppError(403, "forbidden", "Only an owner can grant or remove the owner role.")
    if m.role == "owner" and new_role != "owner" and owners == [user_id]:
        raise AppError(409, "last_owner", "An organization needs an owner. Add another first.")
    return m


@router.patch("/{org_id}/members/{user_id}", dependencies=[need("admin")])
async def change_role(
    org_id: uuid.UUID, user_id: uuid.UUID, body: RoleIn, t: Tenant = Depends(ADMINS)
) -> MemberOut:
    _same_org(org_id, t)
    m = await _target(t, user_id, body.role)
    old, m.role = m.role, body.role
    user = await t.session.get_one(User, user_id)
    if old != body.role:
        await emit(t, "member.role_change", user, {"from": old, "to": body.role})
    await t.session.commit()
    return MemberOut(user_id=user_id, email=user.email, role=body.role)


@router.delete("/{org_id}/members/{user_id}", status_code=204, dependencies=[need("admin")])
async def remove_member(
    org_id: uuid.UUID, user_id: uuid.UUID, t: Tenant = Depends(ADMINS)
) -> Response:
    """Access ends at once for API keys (each request re-reads the role) and at the next refresh
    for signed-in sessions (revoked here; the current access token lives out its TTL)."""
    _same_org(org_id, t)
    m = await _target(t, user_id, None)
    user = await t.session.get_one(User, user_id)  # before the delete: RLS hides non-members
    await t.session.delete(m)
    n = await t.session.scalar(text("SELECT revoke_member_sessions(:u)"), {"u": user_id})
    await emit(t, "member.remove", user, {"role": m.role, "sessions_revoked": n})
    await t.session.commit()
    return Response(status_code=204)


class SettingsOut(BaseModel):
    monthly_budget_usd: Decimal | None = Field(description="null = unlimited")
    budget_action: BudgetAction
    month_spend_usd: Decimal = Field(description="Priced usage so far this UTC month")


class SettingsIn(BaseModel):
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, le=10_000_000, decimal_places=2)
    budget_action: BudgetAction | None = None


async def _settings_out(t: Tenant, settings: dict[str, JsonValue]) -> SettingsOut:
    limit, action = budget_of(settings)
    spent = await month_spend(t.session, t.org_id)
    return SettingsOut(monthly_budget_usd=limit, budget_action=action, month_spend_usd=spent)


@router.get("/{org_id}/settings", dependencies=[need("admin")])
async def read_settings(org_id: uuid.UUID, t: Tenant = Depends(ADMINS)) -> SettingsOut:
    _same_org(org_id, t)
    org = await t.session.get_one(Org, org_id)
    return await _settings_out(t, org.settings)


@router.patch("/{org_id}/settings", dependencies=[need("admin")])
async def update_settings(
    org_id: uuid.UUID, body: SettingsIn, t: Tenant = Depends(ADMINS)
) -> SettingsOut:
    """Only the fields sent change; send monthly_budget_usd: null for unlimited."""
    _same_org(org_id, t)
    org = await t.session.get_one(Org, org_id, with_for_update=True)
    changes: dict[str, JsonValue] = {}
    if "monthly_budget_usd" in body.model_fields_set:
        b = body.monthly_budget_usd
        changes["monthly_budget_usd"] = None if b is None else str(b)
    if body.budget_action is not None:
        changes["budget_action"] = body.budget_action
    if changes:
        org.settings = {**org.settings, **changes}  # new dict: JSONB changes are not tracked
        await emit(t, "org.settings_update", org, {"fields": list[JsonValue](sorted(changes))})
    await t.session.commit()
    return await _settings_out(t, org.settings)


class MyInviteOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    org_name: str
    role: Role
    created_at: datetime
    expires_at: datetime


class AcceptOut(BaseModel):
    org_id: uuid.UUID
    role: Role


def _signed_in(t: Tenant) -> None:
    if t.api_key_id is not None:
        raise AppError(403, "forbidden", "Sign in as the user to manage their invites.")


# The invitee is not in the inviting org yet, so these go through owner-run functions (0010)
# that only ever match invites to the signed-in user's own email.
@invites_router.get("", dependencies=[need(None)])
async def my_invites(t: Tenant = Depends(get_tenant)) -> list[MyInviteOut]:
    """Pending invites for the signed-in user's email, from any org."""
    _signed_in(t)
    rows = await t.session.execute(text("SELECT * FROM my_invites()"))
    return [MyInviteOut.model_validate(r, from_attributes=True) for r in rows]


async def _respond(t: Tenant, invite_id: uuid.UUID, accept: bool) -> AcceptOut:
    _signed_in(t)
    row = (
        await t.session.execute(
            text("SELECT * FROM respond_invite(:i, :a)"), {"i": invite_id, "a": accept}
        )
    ).first()
    if row is None:  # someone else's, expired, revoked, answered or unknown all look the same
        raise not_found("Invite")
    await use_tenant(t.session, row.org_id, t.user_id)  # the audit row belongs to that org
    record(
        t.session, row.org_id, "invite.accept" if accept else "invite.decline",
        user_id=t.user_id, ip=t.ip, target_type="org_invites", target_id=invite_id,
        meta={"role": row.role},
    )  # fmt: skip
    await t.session.commit()
    return AcceptOut(org_id=row.org_id, role=row.role)


@invites_router.post("/{invite_id}/accept", dependencies=[need(None)])
async def accept_invite(invite_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> AcceptOut:
    """Joins the org with the invited role. Switch into it with POST /auth/refresh {org_id}."""
    return await _respond(t, invite_id, True)


@invites_router.post("/{invite_id}/decline", status_code=204, dependencies=[need(None)])
async def decline_invite(invite_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> Response:
    await _respond(t, invite_id, False)
    return Response(status_code=204)
