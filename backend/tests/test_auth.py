import httpx
from sqlalchemy import delete

from app.core.db import SessionLocal, use_tenant
from app.orgs.models import Membership
from tests.conftest import Account, register


async def test_register_login_refresh_me(client: httpx.AsyncClient) -> None:
    acct = await register(client)
    me = (await client.get("/v1/me", headers=acct.headers)).json()
    assert me["user"]["email"] == acct.email
    assert me["role"] == "owner"
    assert me["org"]["name"].endswith("'s workspace")

    login = await client.post(
        "/v1/auth/login", json={"email": acct.email.upper(), "password": acct.password}
    )
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"

    refreshed = await client.post(
        "/v1/auth/refresh", json={"refresh_token": acct.tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    h = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await client.get("/v1/me", headers=h)).status_code == 200


async def test_auth_failures(client: httpx.AsyncClient) -> None:
    acct = await register(client)
    bad = await client.post(
        "/v1/auth/login", json={"email": acct.email, "password": "wrong-password"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"
    unknown = await client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
    )
    assert unknown.status_code == 401

    dup = await client.post(
        "/v1/auth/register", json={"email": acct.email, "password": "another-pass"}
    )
    assert dup.status_code == 409

    # an access token is not a refresh token, and vice versa
    r = await client.post("/v1/auth/refresh", json={"refresh_token": acct.tokens["access_token"]})
    assert r.status_code == 401
    h = {"Authorization": f"Bearer {acct.tokens['refresh_token']}"}
    assert (await client.get("/v1/me", headers=h)).status_code == 401
    assert (
        await client.get("/v1/me", headers={"Authorization": "Bearer garbage"})
    ).status_code == 401
    assert (await client.get("/v1/me")).json()["error"]["code"] == "not_authenticated"


async def test_validation_error_shape(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/auth/register",
        json={"email": "bad", "password": "x"},
        headers={"X-Request-ID": "abc-123"},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["request_id"] == "abc-123" == r.headers["x-request-id"]
    assert "email" in err["message"]


async def _add(client: httpx.AsyncClient, by: Account, email: str, role: str) -> httpx.Response:
    return await client.post(
        f"/v1/orgs/{by.org_id}/members", json={"email": email, "role": role}, headers=by.headers
    )


async def accept(client: httpx.AsyncClient, who: Account, invite: httpx.Response) -> None:
    """The invitee accepts an invite (S10: inviting never adds anyone by itself)."""
    assert invite.status_code == 201, invite.text
    r = await client.post(f"/v1/invites/{invite.json()['id']}/accept", headers=who.headers)
    assert r.status_code == 200, r.text


async def test_orgs_and_members(client: httpx.AsyncClient) -> None:
    owner, other, third = await register(client), await register(client), await register(client)

    created = await client.post("/v1/orgs", json={"name": "Acme"}, headers=owner.headers)
    assert created.status_code == 201 and created.json()["role"] == "owner"
    orgs = (await client.get("/v1/orgs", headers=owner.headers)).json()
    assert {o["name"] for o in orgs} >= {"Acme"} and len(orgs) == 2

    await accept(client, other, await _add(client, owner, other.email, "admin"))
    assert (await _add(client, owner, other.email, "viewer")).status_code == 409
    assert (await _add(client, owner, "ghost@example.com", "viewer")).status_code == 201

    # other switches into owner's personal org via refresh, as admin
    sw = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": other.tokens["refresh_token"], "org_id": str(owner.org_id)},
    )
    assert sw.status_code == 200
    admin = Account(other.email, other.password, other.user_id, owner.org_id, sw.json())
    assert (await client.get("/v1/me", headers=admin.headers)).json()["role"] == "admin"
    assert (await _add(client, admin, third.email, "owner")).status_code == 403
    await accept(client, third, await _add(client, admin, third.email, "viewer"))

    # third is a viewer: cannot add members, cannot create KBs
    sw3 = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": third.tokens["refresh_token"], "org_id": str(owner.org_id)},
    )
    viewer = Account(third.email, third.password, third.user_id, owner.org_id, sw3.json())
    assert (await _add(client, viewer, owner.email, "viewer")).status_code == 403
    assert (
        await client.post("/v1/kbs", json={"name": "x"}, headers=viewer.headers)
    ).status_code == 403
    assert (await client.get("/v1/kbs", headers=viewer.headers)).status_code == 200


async def test_cannot_switch_or_add_into_foreign_org(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    r = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": b.tokens["refresh_token"], "org_id": str(a.org_id)},
    )
    assert r.status_code == 403
    r = await client.post(
        f"/v1/orgs/{a.org_id}/members", json={"email": b.email, "role": "owner"}, headers=b.headers
    )
    assert r.status_code == 404


async def test_revoked_membership_rejects_live_token(client: httpx.AsyncClient) -> None:
    a = await register(client)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(delete(Membership).where(Membership.user_id == a.user_id))
        await s.commit()
    r = await client.get("/v1/me", headers=a.headers)
    assert r.status_code == 403
    login = await client.post("/v1/auth/login", json={"email": a.email, "password": a.password})
    assert login.status_code == 403 and login.json()["error"]["code"] == "no_org"
