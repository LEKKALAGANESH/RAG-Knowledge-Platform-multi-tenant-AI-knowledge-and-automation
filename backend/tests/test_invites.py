"""S10: adding a member creates an invite the invitee accepts in-app; S9: users RLS."""

import time
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import Account, register  # isort: skip
from app.core.db import SessionLocal, use_tenant
from tests.test_auth_keys import bearer, make_key


async def _invite(client: httpx.AsyncClient, by: Account, email: str, role: str) -> httpx.Response:
    return await client.post(
        f"/v1/orgs/{by.org_id}/members", json={"email": email, "role": role}, headers=by.headers
    )


async def _switch(client: httpx.AsyncClient, who: Account, org: uuid.UUID) -> httpx.Response:
    return await client.post(
        "/v1/auth/refresh", json={"refresh_token": who.tokens["refresh_token"], "org_id": str(org)}
    )


async def _answer(client: httpx.AsyncClient, who: Account, inv: str, verb: str = "accept") -> int:
    return (await client.post(f"/v1/invites/{inv}/{verb}", headers=who.headers)).status_code


async def _members(client: httpx.AsyncClient, owner: Account) -> set[str]:
    r = await client.get(f"/v1/orgs/{owner.org_id}/members", headers=owner.headers)
    return {m["email"] for m in r.json()}


async def _actions(owner: Account) -> list[str]:
    async with SessionLocal() as s:
        await use_tenant(s, owner.org_id, owner.user_id)
        rows = await s.scalars(
            text("SELECT action FROM audit_log WHERE action LIKE 'invite.%' ORDER BY created_at")
        )
        return list(rows)


async def test_pre_registered_account_gains_nothing_without_accepting(
    client: httpx.AsyncClient,
) -> None:
    owner, attacker = await register(client), await register(client)
    r = await _invite(client, owner, attacker.email, "admin")
    assert r.status_code == 201
    # Old code added the account at once: the switch below returned 200 with an admin token.
    assert (await _switch(client, attacker, owner.org_id)).status_code == 403
    assert attacker.email not in await _members(client, owner)


async def test_invite_response_is_identical_for_existing_and_unknown_email(
    client: httpx.AsyncClient,
) -> None:
    owner, known = await register(client), await register(client)
    unknown = f"nobody{uuid.uuid4().hex[:8]}@example.com"
    t0 = time.perf_counter()
    a = await _invite(client, owner, known.email, "editor")
    t1 = time.perf_counter()
    b = await _invite(client, owner, unknown, "editor")
    t2 = time.perf_counter()
    assert a.status_code == b.status_code == 201  # old code: 201 vs 404 user_not_found
    ja, jb = a.json(), b.json()
    assert ja.keys() == jb.keys() == {"id", "email", "role", "created_at", "expires_at"}
    assert (ja["email"], jb["email"]) == (known.email, unknown)
    assert ja["role"] == jb["role"] == "editor"
    assert a.headers.keys() == b.headers.keys()
    assert abs((t1 - t0) - (t2 - t1)) < 0.5  # same queries either way; only a coarse bound
    pending = (await client.get(f"/v1/orgs/{owner.org_id}/invites", headers=owner.headers)).json()
    assert {i["email"] for i in pending} == {known.email, unknown}


async def test_accept_creates_membership_with_the_invited_role(client: httpx.AsyncClient) -> None:
    owner, invitee = await register(client), await register(client)
    inv = (await _invite(client, owner, invitee.email.upper(), "editor")).json()

    mine = (await client.get("/v1/invites", headers=invitee.headers)).json()
    assert [(i["id"], i["org_id"], i["role"]) for i in mine] == [
        (inv["id"], str(owner.org_id), "editor")
    ]
    assert mine[0]["org_name"].endswith("'s workspace")
    assert (await client.get("/v1/invites", headers=owner.headers)).json() == []

    r = await client.post(f"/v1/invites/{inv['id']}/accept", headers=invitee.headers)
    assert r.status_code == 200 and r.json() == {"org_id": str(owner.org_id), "role": "editor"}
    sw = await _switch(client, invitee, owner.org_id)
    assert sw.status_code == 200
    me = await client.get("/v1/me", headers=bearer(sw.json()["access_token"]))
    assert me.json()["role"] == "editor"
    assert invitee.email in await _members(client, owner)
    # Spent: gone from both lists, a second accept is a 404.
    assert (await client.get("/v1/invites", headers=invitee.headers)).json() == []
    r = await client.get(f"/v1/orgs/{owner.org_id}/invites", headers=owner.headers)
    assert r.json() == []
    again = await client.post(f"/v1/invites/{inv['id']}/accept", headers=invitee.headers)
    assert again.status_code == 404
    assert (await _invite(client, owner, invitee.email, "viewer")).status_code == 409
    assert await _actions(owner) == ["invite.create", "invite.accept"]


async def test_only_the_invitee_can_answer(client: httpx.AsyncClient) -> None:
    owner, invitee, other = await register(client), await register(client), await register(client)
    inv = (await _invite(client, owner, invitee.email, "viewer")).json()
    assert (await client.get("/v1/invites", headers=other.headers)).json() == []
    for verb in ("accept", "decline"):
        r = await client.post(f"/v1/invites/{inv['id']}/{verb}", headers=other.headers)
        assert r.status_code == 404
    # The owner (another email) cannot accept on the invitee's behalf either.
    r = await client.post(f"/v1/invites/{inv['id']}/accept", headers=owner.headers)
    assert r.status_code == 404
    # Nor can the invitee through an API key.
    key = (await make_key(client, invitee, ["admin"]))["key"]
    r = await client.post(f"/v1/invites/{inv['id']}/accept", headers=bearer(key))
    assert r.status_code == 403
    assert (await _switch(client, other, owner.org_id)).status_code == 403


async def test_decline_expired_and_revoked_cannot_be_accepted(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    a, b, c = await register(client), await register(client), await register(client)
    ia = (await _invite(client, owner, a.email, "viewer")).json()
    ib = (await _invite(client, owner, b.email, "viewer")).json()
    ic = (await _invite(client, owner, c.email, "viewer")).json()

    r = await client.post(f"/v1/invites/{ia['id']}/decline", headers=a.headers)
    assert r.status_code == 204
    assert await _answer(client, a, ia["id"]) == 404

    async with SessionLocal() as s:
        await use_tenant(s, owner.org_id, owner.user_id)
        await s.execute(
            text("UPDATE org_invites SET expires_at = now() - interval '1 second' WHERE id = :i"),
            {"i": ib["id"]},
        )
        await s.commit()
    assert (await client.get("/v1/invites", headers=b.headers)).json() == []
    assert await _answer(client, b, ib["id"]) == 404

    d = await client.delete(f"/v1/orgs/{owner.org_id}/invites/{ic['id']}", headers=owner.headers)
    assert d.status_code == 204
    d = await client.delete(f"/v1/orgs/{owner.org_id}/invites/{ic['id']}", headers=owner.headers)
    assert d.status_code == 404
    assert await _answer(client, c, ic["id"]) == 404

    for who in (a, b, c):
        assert (await _switch(client, who, owner.org_id)).status_code == 403
    assert await _actions(owner) == [
        "invite.create", "invite.create", "invite.create", "invite.decline", "invite.revoke"
    ]  # fmt: skip


async def test_reinvite_replaces_pending_and_admin_limits(client: httpx.AsyncClient) -> None:
    owner, invitee, adm = await register(client), await register(client), await register(client)
    first = (await _invite(client, owner, invitee.email, "viewer")).json()
    second = (await _invite(client, owner, invitee.email, "editor")).json()
    mine = (await client.get("/v1/invites", headers=invitee.headers)).json()
    assert [i["id"] for i in mine] == [second["id"]]
    r = await client.post(f"/v1/invites/{first['id']}/accept", headers=invitee.headers)
    assert r.status_code == 404

    owner_inv = (await _invite(client, owner, "boss@example.com", "owner")).json()
    inv = (await _invite(client, owner, adm.email, "admin")).json()
    await client.post(f"/v1/invites/{inv['id']}/accept", headers=adm.headers)
    admin = Account(adm.email, adm.password, adm.user_id, owner.org_id,
                    (await _switch(client, adm, owner.org_id)).json())  # fmt: skip
    assert (await _invite(client, admin, "x@example.com", "owner")).status_code == 403
    path = f"/v1/orgs/{owner.org_id}/invites/{owner_inv['id']}"
    r = await client.delete(path, headers=admin.headers)
    assert r.status_code == 403
    # Another org's admin cannot see or revoke this org's invites.
    stranger = await register(client)
    r = await client.get(f"/v1/orgs/{owner.org_id}/invites", headers=stranger.headers)
    assert r.status_code == 404
    r = await client.delete(
        f"/v1/orgs/{stranger.org_id}/invites/{second['id']}", headers=stranger.headers
    )
    assert r.status_code == 404


# --- S9: users RLS -------------------------------------------------------------------------


async def _emails(as_org: uuid.UUID | None, as_user: uuid.UUID | None) -> set[str]:
    async with SessionLocal() as s:
        await use_tenant(s, as_org, as_user)
        return set(await s.scalars(text("SELECT email FROM users")))


async def test_users_rows_are_scoped_to_self_and_current_org(client: httpx.AsyncClient) -> None:
    owner, member, outsider = await register(client), await register(client), await register(client)
    inv = (await _invite(client, owner, member.email, "viewer")).json()
    await client.post(f"/v1/invites/{inv['id']}/accept", headers=member.headers)

    assert await _emails(None, None) == set()  # old code: every user in the database
    assert await _emails(owner.org_id, owner.user_id) == {owner.email, member.email}
    assert await _emails(outsider.org_id, outsider.user_id) == {outsider.email}
    assert await _emails(None, member.user_id) == {member.email}
    # An org id alone (forged or not) never shows other orgs' users.
    assert outsider.email not in await _emails(owner.org_id, member.user_id)


async def test_app_role_cannot_read_pw_hash_or_insert_other_users(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("SELECT pw_hash FROM users"))
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(
                text("INSERT INTO users (id, email, pw_hash) VALUES (:i, :e, 'x')"),
                {"i": uuid.uuid4(), "e": f"x{uuid.uuid4().hex[:8]}@example.com"},
            )
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("UPDATE users SET email = email"))
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": a.user_id})


async def test_definer_functions_are_not_public() -> None:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT proname, prosecdef, has_function_privilege('public', oid, 'EXECUTE') "
                    "FROM pg_proc WHERE proname IN "
                    "('auth_user_by_email', 'my_invites', 'respond_invite')"
                )
            )
        ).all()
    assert {r[0] for r in rows} == {"auth_user_by_email", "my_invites", "respond_invite"}
    assert all(r[1] and not r[2] for r in rows), rows
