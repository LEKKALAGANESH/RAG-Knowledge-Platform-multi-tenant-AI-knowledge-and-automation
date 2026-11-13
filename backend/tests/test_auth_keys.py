"""API keys: shown once, stored hashed, scoped, revocable, org-bound (AC1), and every route
declares the scope a key needs."""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text

from app.core.db import SessionLocal, use_tenant
from app.core.security import keyed_hash, split_api_key
from app.core.tenancy import SCOPES, Need
from app.main import app
from tests.conftest import Account, register

PUBLIC = {
    "/v1/auth/register", "/v1/auth/login", "/v1/auth/refresh", "/v1/auth/logout", "/healthz",
    "/readyz",
}  # fmt: skip


async def make_key(
    client: httpx.AsyncClient, a: Account, scopes: list[str], **extra: Any
) -> dict[str, Any]:
    r = await client.post(
        "/v1/api-keys", json={"name": "ci", "scopes": scopes, **extra}, headers=a.headers
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _key_row(a: Account, key_id: str) -> dict[str, Any]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        row = (await s.execute(text("SELECT * FROM api_keys WHERE id = :id"), {"id": key_id})).one()
    return dict(row._mapping)


async def test_key_is_shown_once_and_only_its_hash_is_stored(client: httpx.AsyncClient) -> None:
    a = await register(client)
    created = await make_key(client, a, ["kb:read"])
    key = created["key"]
    assert key.startswith(f"rag_{created['prefix']}_")
    prefix, secret = split_api_key(key) or ("", "")
    assert len(secret) >= 43  # token_urlsafe(32): 256 bits

    row = await _key_row(a, created["id"])
    assert row["hash"] == keyed_hash(secret) and row["prefix"] == prefix
    assert not any(secret in str(v) for v in row.values())

    listed = (await client.get("/v1/api-keys", headers=a.headers)).json()
    assert [k["id"] for k in listed] == [created["id"]]
    assert "key" not in listed[0] and "hash" not in listed[0] and secret not in str(listed)


async def test_key_authenticates_both_headers_and_scopes_are_enforced(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    key = (await make_key(client, a, ["kb:read", "search"]))["key"]
    for h in (bearer(key), {"X-API-Key": key}):
        assert (await client.get("/v1/kbs", headers=h)).status_code == 200
    assert (
        await client.post("/v1/search", json={"query": "anything"}, headers=bearer(key))
    ).status_code == 200
    me = (await client.get("/v1/me", headers=bearer(key))).json()
    assert me["user"]["id"] == str(a.user_id) and me["role"] == "owner"

    for method, path, body in (
        ("POST", "/v1/kbs", {"name": "x"}),
        ("POST", "/v1/answer", {"question": "q"}),
        ("GET", "/v1/api-keys", None),
        ("GET", "/v1/audit", None),
        ("POST", "/v1/agent/runs", {"goal": "g"}),
    ):
        r = await client.request(method, path, json=body, headers=bearer(key))
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "insufficient_scope"


async def test_key_cannot_mint_wider_keys_and_viewer_cannot_mint(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    admin_key = (await make_key(client, a, ["admin"]))["key"]
    r = await client.post(
        "/v1/api-keys", json={"name": "x", "scopes": ["kb:write"]}, headers=bearer(admin_key)
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    r = await client.post(
        "/v1/api-keys", json={"name": "x", "scopes": ["admin"]}, headers=bearer(admin_key)
    )
    assert r.status_code == 201

    v = await register(client)
    r = await client.post(
        f"/v1/orgs/{a.org_id}/members", json={"email": v.email, "role": "viewer"}, headers=a.headers
    )
    assert r.status_code == 201
    ok = await client.post(f"/v1/invites/{r.json()['id']}/accept", headers=v.headers)
    assert ok.status_code == 200
    login = await client.post("/v1/auth/refresh", json={"refresh_token": v.tokens["refresh_token"],
                                                         "org_id": str(a.org_id)})  # fmt: skip
    vh = bearer(login.json()["access_token"])
    r = await client.post("/v1/api-keys", json={"name": "x", "scopes": ["kb:read"]}, headers=vh)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


async def test_unknown_scope_and_past_expiry_are_422(client: httpx.AsyncClient) -> None:
    a = await register(client)
    for body in (
        {"name": "x", "scopes": ["root"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["kb:read"], "expires_at": "2000-01-01T00:00:00Z"},
    ):
        r = await client.post("/v1/api-keys", json=body, headers=a.headers)
        assert r.status_code == 422, body


async def test_revoked_expired_and_malformed_keys_are_401(client: httpx.AsyncClient) -> None:
    a = await register(client)
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    revoked, expired = (
        await make_key(client, a, ["kb:read"]),
        await make_key(client, a, ["kb:read"], expires_at=soon),
    )
    assert (await client.get("/v1/kbs", headers=bearer(revoked["key"]))).status_code == 200
    r = await client.delete(f"/v1/api-keys/{revoked['id']}", headers=a.headers)
    assert r.status_code == 204
    assert (
        await client.delete(f"/v1/api-keys/{revoked['id']}", headers=a.headers)
    ).status_code == 204
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(
            text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": expired["id"]},
        )
        await s.commit()

    prefix = revoked["prefix"]
    for key in (
        revoked["key"],
        expired["key"],
        f"rag_{prefix}_{'x' * 43}",  # right prefix, wrong secret
        "rag_short_x",
        "rag_",
    ):
        for h in (bearer(key), {"X-API-Key": key}):
            r = await client.get("/v1/kbs", headers=h)
            assert r.status_code == 401, key
            assert r.json()["error"]["code"] == "invalid_api_key"


async def test_key_cannot_reach_another_org(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    kb_b = (await client.post("/v1/kbs", json={"name": "B"}, headers=b.headers)).json()
    key_b = (await make_key(client, b, ["kb:read", "admin"]))["id"]
    key_a = (await make_key(client, a, ["kb:read", "admin"]))["key"]
    h = bearer(key_a)
    assert (await client.get(f"/v1/kbs/{kb_b['id']}", headers=h)).status_code == 404
    assert (await client.get("/v1/kbs", headers=h)).json()["items"] == []
    assert (await client.delete(f"/v1/api-keys/{key_b}", headers=h)).status_code == 404
    assert key_b not in str((await client.get("/v1/api-keys", headers=h)).json())


async def test_key_loses_access_with_its_creators_membership(client: httpx.AsyncClient) -> None:
    a = await register(client)
    key = (await make_key(client, a, ["kb:read"]))["key"]
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": a.user_id})
        await s.commit()
    assert (await client.get("/v1/kbs", headers=bearer(key))).status_code == 403


async def test_last_used_is_throttled(client: httpx.AsyncClient) -> None:
    a = await register(client)
    created = await make_key(client, a, ["kb:read"])
    assert (await _key_row(a, created["id"]))["last_used_at"] is None
    await client.get("/v1/kbs", headers=bearer(created["key"]))
    first = (await _key_row(a, created["id"]))["last_used_at"]
    await client.get("/v1/kbs", headers=bearer(created["key"]))
    assert first is not None and (await _key_row(a, created["id"]))["last_used_at"] == first


def _needs(route: APIRoute) -> list[Need]:
    found, stack = [], [route.dependant]
    while stack:
        d = stack.pop()
        found += [d.call] if isinstance(d.call, Need) else []
        stack += d.dependencies
    return found


def _routes() -> list[tuple[str, str, list[Need]]]:
    """(full path, method, scope declarations) of every API route, through included routers."""
    out = []
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.original_route, APIRoute) and ctx.path and ctx.methods:
            for method in ctx.methods:
                out.append((ctx.path, method, _needs(ctx.original_route)))
    return out


def test_every_non_public_route_declares_a_scope() -> None:
    routes = _routes()
    assert len(routes) > 40
    assert [p for p, _, needs in routes if p not in PUBLIC and not needs] == []
    assert all(not needs for p, _, needs in routes if p in PUBLIC)
    declared = {n.scope for _, _, needs in routes for n in needs}
    assert declared - {None} == set(SCOPES)  # every scope guards something; no typos


async def test_a_key_with_every_scope_but_one_is_refused_exactly_there(
    client: httpx.AsyncClient,
) -> None:
    """Route table x scopes: each scoped route rejects a key missing only its scope."""
    a = await register(client)
    fake, checked = uuid.uuid4(), 0
    for scope in SCOPES:
        key = (await make_key(client, a, [s for s in SCOPES if s != scope]))["key"]
        for path, method, needs in _routes():
            if scope not in {n.scope for n in needs}:
                continue
            path = re.sub(r"\{(\w+)\}", str(fake), path.replace("{org_id}", str(a.org_id)))
            r = await client.request(method, path, headers=bearer(key))
            assert r.status_code == 403, (scope, method, path, r.status_code)
            assert r.json()["error"]["code"] == "insufficient_scope"
            checked += 1
    assert checked == sum(1 for _, _, needs in _routes() if any(n.scope for n in needs)) >= 40


async def test_keys_never_reach_logs(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    a = await register(client)
    with caplog.at_level(logging.DEBUG):
        created = await make_key(client, a, ["kb:read"])
        await client.get("/v1/kbs", headers=bearer(created["key"]))
        await client.get("/v1/kbs", headers={"X-API-Key": created["key"] + "x"})
        await client.delete(f"/v1/api-keys/{created['id']}", headers=a.headers)
    # split_api_key, not rpartition("_"): the secret itself may contain "_" (a 1-char tail
    # like "E" then matched the word "INFO" and failed the test at random).
    _, secret = split_api_key(created["key"]) or ("", "")
    assert len(secret) >= 43 and caplog.records and secret not in caplog.text
