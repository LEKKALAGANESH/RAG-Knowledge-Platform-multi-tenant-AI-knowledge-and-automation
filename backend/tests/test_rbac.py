"""Role matrix over EVERY route (POLICY is the source of truth) + member role change/removal."""

import re
import uuid

import httpx
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text

from tests.conftest import Account, register  # isort: skip
from app.core.db import SessionLocal, use_tenant
from app.main import app
from tests.test_usage import join_as

ALL = frozenset({"owner", "admin", "editor", "viewer"})
EDIT = frozenset({"owner", "admin", "editor"})
ADMIN = frozenset({"owner", "admin"})
PUBLIC = frozenset({"public"})

# Who may call each route. A role outside the set gets 403 `forbidden`; a role inside never does.
POLICY: dict[str, frozenset[str]] = {
    "POST /v1/auth/register": PUBLIC,
    "POST /v1/auth/login": PUBLIC,
    "POST /v1/auth/refresh": PUBLIC,
    "POST /v1/auth/logout": PUBLIC,
    "POST /v1/auth/logout-all": ALL,
    "GET /v1/me": ALL,
    "GET /v1/orgs": ALL,
    "POST /v1/orgs": ALL,
    "POST /v1/orgs/{org_id}/members": ADMIN,
    "GET /v1/orgs/{org_id}/members": ALL,
    "PATCH /v1/orgs/{org_id}/members/{user_id}": ADMIN,
    "DELETE /v1/orgs/{org_id}/members/{user_id}": ADMIN,
    "GET /v1/orgs/{org_id}/invites": ADMIN,
    "DELETE /v1/orgs/{org_id}/invites/{invite_id}": ADMIN,
    "GET /v1/invites": ALL,  # the signed-in user's own invites, any org (S10)
    "POST /v1/invites/{invite_id}/accept": ALL,
    "POST /v1/invites/{invite_id}/decline": ALL,
    "GET /v1/orgs/{org_id}/settings": ADMIN,
    "PATCH /v1/orgs/{org_id}/settings": ADMIN,
    "GET /v1/kbs": ALL,
    "POST /v1/kbs": EDIT,
    "GET /v1/kbs/{kb_id}": ALL,
    "PATCH /v1/kbs/{kb_id}": EDIT,
    "DELETE /v1/kbs/{kb_id}": EDIT,
    "GET /v1/kbs/{kb_id}/documents": ALL,
    "GET /v1/documents/{doc_id}": ALL,
    "GET /v1/documents/{doc_id}/chunks": ALL,
    "DELETE /v1/documents/{doc_id}": EDIT,
    "POST /v1/kbs/{kb_id}/documents": EDIT,
    "POST /v1/documents/{doc_id}/reprocess": EDIT,
    "POST /v1/search": ALL,
    "POST /v1/kbs/{kb_id}/reindex": EDIT,
    "POST /v1/answer": ALL,
    "GET /v1/conversations": ALL,
    "GET /v1/conversations/{conversation_id}": ALL,
    "GET /v1/connectors": ALL,
    "POST /v1/kbs/{kb_id}/sources": EDIT,
    "POST /v1/kbs/{kb_id}/sources/oauth/start": EDIT,
    "POST /v1/sources/{source_id}/oauth/reconnect": EDIT,
    "POST /v1/connectors/oauth/finish": EDIT,  # + same user, org and browser as the start (S2)
    "POST /v1/connectors/rest/openapi": EDIT,
    "GET /v1/kbs/{kb_id}/sources": ALL,
    "GET /v1/sources/{source_id}": ALL,
    "PATCH /v1/sources/{source_id}": EDIT,
    "DELETE /v1/sources/{source_id}": EDIT,
    "POST /v1/sources/{source_id}/test": EDIT,
    "POST /v1/sources/{source_id}/sync": EDIT,
    "GET /v1/sources/{source_id}/runs": ALL,
    "POST /v1/agent/runs": ALL,
    "GET /v1/agent/runs": ALL,
    "GET /v1/agent/runs/{run_id}": ALL,
    "GET /v1/agent/runs/{run_id}/events": ALL,
    "POST /v1/agent/runs/{run_id}/cancel": ALL,
    "POST /v1/approvals/{approval_id}": ALL,  # own approvals; admins decide any (route logic)
    "GET /v1/reports/{report_id}": ALL,
    "POST /v1/api-keys": ADMIN,
    "GET /v1/api-keys": ADMIN,
    "DELETE /v1/api-keys/{key_id}": ADMIN,
    "GET /v1/audit": ADMIN,
    "GET /v1/audit/export": ADMIN,
    "GET /v1/usage": ALL,  # owner/admin: whole org; others: their own usage only
    "GET /v1/usage/export": ALL,
    "GET /healthz": PUBLIC,
    "GET /readyz": PUBLIC,
}


def _routes() -> set[str]:
    return {
        f"{m} {ctx.path}"
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.original_route, APIRoute) and ctx.path and ctx.methods
        for m in ctx.methods
    }


def test_policy_table_covers_every_route() -> None:
    routes = _routes()
    assert routes - POLICY.keys() == set(), "route missing from POLICY"
    assert POLICY.keys() - routes == set(), "POLICY lists a route that no longer exists"


async def _team(client: httpx.AsyncClient) -> dict[str, Account]:
    owner = await register(client)
    team = {"owner": owner}
    for role in ("admin", "editor", "viewer"):
        team[role] = await join_as(client, owner, role)
    return team


async def test_role_matrix(client: httpx.AsyncClient) -> None:
    team = await _team(client)
    fake, checked = str(uuid.uuid4()), 0
    for route, allowed in sorted(POLICY.items()):
        if allowed == PUBLIC:
            continue
        method, path = route.split(" ", 1)
        path = re.sub(r"\{\w+\}", fake, path.replace("{org_id}", str(team["owner"].org_id)))
        for role, acct in team.items():
            r = await client.request(method, path, headers=acct.headers)
            denied = r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
            assert denied == (role not in allowed), (route, role, r.status_code, r.text[:200])
            checked += 1
    assert checked == 4 * sum(1 for v in POLICY.values() if v != PUBLIC)


async def _patch(
    client: httpx.AsyncClient, by: Account, user: Account, role: str
) -> httpx.Response:
    return await client.patch(
        f"/v1/orgs/{by.org_id}/members/{user.user_id}", json={"role": role}, headers=by.headers
    )


async def _delete(client: httpx.AsyncClient, by: Account, user: Account) -> httpx.Response:
    return await client.delete(f"/v1/orgs/{by.org_id}/members/{user.user_id}", headers=by.headers)


async def _audit(a: Account, action: str) -> list[dict[str, object]]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        rows = await s.execute(
            text("SELECT target_id, meta FROM audit_log WHERE action = :a"), {"a": action}
        )
        return [dict(r._mapping) for r in rows]


async def test_role_change_rules_and_audit(client: httpx.AsyncClient) -> None:
    t = await _team(client)
    owner, admin, editor, viewer = t["owner"], t["admin"], t["editor"], t["viewer"]
    r = await _patch(client, admin, viewer, "editor")
    assert r.status_code == 200 and r.json()["role"] == "editor"
    assert (await _patch(client, admin, viewer, "owner")).status_code == 403  # only owners grant
    assert (await _patch(client, admin, owner, "viewer")).status_code == 403  # or take away
    assert (await _patch(client, editor, viewer, "viewer")).status_code == 403
    last = await _patch(client, owner, owner, "admin")
    assert last.status_code == 409 and last.json()["error"]["code"] == "last_owner"
    assert (await _delete(client, owner, owner)).status_code == 409

    assert (await _patch(client, owner, admin, "owner")).status_code == 200
    assert (await _patch(client, owner, owner, "admin")).status_code == 200  # another owner exists
    me = await client.get("/v1/me", headers=owner.headers)
    assert me.json()["role"] == "admin"  # takes effect on the live token
    changes = await _audit(owner, "member.role_change")
    assert {"from": "viewer", "to": "editor"} in [c["meta"] for c in changes]
    assert len(changes) == 3

    other = await register(client)
    assert (await _patch(client, other, editor, "viewer")).status_code == 404  # not a member
    r = await client.patch(
        f"/v1/orgs/{owner.org_id}/members/{editor.user_id}", json={"role": "viewer"},
        headers=other.headers,
    )  # fmt: skip
    assert r.status_code == 404  # another org looks nonexistent
    assert (await _patch(client, admin, other, "viewer")).status_code == 404  # not a member


async def test_removed_member_loses_keys_and_sessions(client: httpx.AsyncClient) -> None:
    t = await _team(client)
    owner, editor = t["owner"], t["editor"]
    # the editor's own API key (admin creates keys only for themselves, so promote briefly)
    assert (await _patch(client, owner, editor, "admin")).status_code == 200
    key = (
        await client.post(
            "/v1/api-keys", json={"name": "k", "scopes": ["kb:read"]}, headers=editor.headers
        )
    ).json()["key"]
    assert (await _patch(client, owner, editor, "editor")).status_code == 200
    kh = {"Authorization": f"Bearer {key}"}
    assert (await client.get("/v1/kbs", headers=kh)).status_code == 200

    assert (await _delete(client, owner, editor)).status_code == 204
    assert (await client.get("/v1/kbs", headers=kh)).status_code == 403
    r = await client.post(
        "/v1/auth/refresh", json={"refresh_token": editor.tokens["refresh_token"]}
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_revoked"
    (row,) = await _audit(owner, "member.remove")
    assert row["target_id"] == editor.user_id and row["meta"] == {
        "role": "editor", "sessions_revoked": 1,
    }  # fmt: skip
    assert (await _delete(client, owner, editor)).status_code == 404


async def test_members_list(client: httpx.AsyncClient) -> None:
    t = await _team(client)
    for reader in ("admin", "viewer"):  # read-only for every role
        r = await client.get(f"/v1/orgs/{t['owner'].org_id}/members", headers=t[reader].headers)
        assert r.status_code == 200
        assert {m["email"]: m["role"] for m in r.json()} == {a.email: k for k, a in t.items()}
